"""resolve_image_build_plan 的回归测试。

核心防护目标：共享依赖 backend/shared/ 的任意变更必须触发全部后端服务
（含 kbService）重建并推进 tag。历史上因 shared 变更只推进了部分后端服务，
导致 agent-service 与 kb-service 的 signals 指纹分叉，引发 KBD 23821 诊断
tool_contract_revision stale 故障。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "resolve_image_build_plan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("resolve_image_build_plan", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _select(paths, *, force_all=False):
    module = _load_module()
    selected, _ = module.select_services(paths, force_all=force_all)
    return selected


def test_shared_change_rebuilds_all_backend_services():
    """backend/shared/ 下任意改动必须包含全部后端服务（含 kb-service）。"""
    selected = _select(["backend/shared/schemas/signal_generation.py"])
    backend_keys = {"api-gateway", "case-service", "conversation-service",
                    "agent-service", "eval-service", "scheduler-service", "kb-service"}
    assert backend_keys <= selected, f"shared 变更漏推后端服务: {selected}"


def test_kb_service_catalog_change_triggers_kb_service_rebuild():
    """kb-service 自身代码改动必须触发 kb-service 重建。"""
    selected = _select(["backend/kb-service/app/routes/resolution_catalogs.py"])
    assert "kb-service" in selected


def test_force_all_selects_everything():
    selected = _select([], force_all=True)
    assert "kb-service" in selected
    assert "agent-service" in selected
    assert "terminal-bridge" in selected


def test_push_uses_successful_release_baseline(monkeypatch):
    """主干空 diff push 也必须从最近成功镜像发布继续计算。"""
    module = _load_module()
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_RELEASE_BASE_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_EVENT_BEFORE", "b" * 40)
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)

    captured = {}

    class Result:
        stdout = "frontend/admin/src/router/index.ts\n"

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.changed_files() == ["frontend/admin/src/router/index.ts"]
    assert captured["command"][3] == "a" * 40


def test_manual_dispatch_uses_force_all_without_event_before(monkeypatch):
    """手动发布没有 event.before 时仍应进入全量构建路径。"""
    module = _load_module()
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("GITHUB_EVENT_BEFORE", raising=False)
    assert module.changed_files() == []


def test_reconciliation_rebuilds_only_service_with_unreleased_change(monkeypatch):
    """admin-ui 成功发布不能掩盖 api-gateway 的历史未发布变更。"""
    module = _load_module()
    current = "c" * 40
    baselines = {service: "b" * 40 for service in module.SERVICE_NAMES}
    baselines["api-gateway"] = "a" * 40

    def fake_diff(base, head):
        assert head == current
        if base == "a" * 40:
            return ["backend/api-gateway/app/routes/simulations.py"]
        return []

    monkeypatch.setattr(module, "changed_files_between", fake_diff)
    assert module.reconciliation_services(json.dumps(baselines), current) == {"api-gateway"}


def test_reconciliation_fails_closed_for_missing_service_baseline(monkeypatch):
    """从未确认发布过的服务不得被全局成功基线静默跳过。"""
    module = _load_module()
    current = "c" * 40
    baselines = {service: "b" * 40 for service in module.SERVICE_NAMES if service != "hci-sim"}
    monkeypatch.setattr(module, "changed_files_between", lambda _base, _head: [])
    assert module.reconciliation_services(json.dumps(baselines), current) == {"hci-sim"}
