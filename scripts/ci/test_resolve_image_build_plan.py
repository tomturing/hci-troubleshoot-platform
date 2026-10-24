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
    backend_keys = {
        "auth-service",
        "api-gateway",
        "case-service",
        "conversation-service",
        "agent-service",
        "scheduler-service",
        "kb-service",
    }
    assert backend_keys <= selected, f"shared 变更漏推后端服务: {selected}"


def test_mirror_setup_script_change_rebuilds_all_backend_services():
    """共享镜像源脚本（deploy/docker/base/setup-mirror.sh）被全部后端 Dockerfile COPY，必须全量重建。"""
    selected = _select(["deploy/docker/base/setup-mirror.sh"])
    backend_keys = {
        "auth-service",
        "api-gateway",
        "case-service",
        "conversation-service",
        "agent-service",
        "scheduler-service",
        "kb-service",
    }
    assert backend_keys <= selected, f"镜像源脚本变更漏推后端服务: {selected}"


def test_kb_service_catalog_change_triggers_kb_service_rebuild():
    """kb-service 自身代码改动必须触发 kb-service 重建。"""
    selected = _select(["backend/kb-service/app/routes/resolution_catalogs.py"])
    assert "kb-service" in selected


def test_force_all_selects_everything():
    selected = _select([], force_all=True)
    assert "kb-service" in selected
    assert "agent-service" in selected
    assert "terminal-bridge" in selected


def test_push_uses_event_before_for_current_changes(monkeypatch):
    """当前 push 只看相邻提交，历史漏发交给服务级基线补偿。"""
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
    assert captured["command"][3] == "b" * 40


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
    assert module.reconciliation_services(json.dumps(baselines), current, "b" * 40) == {"api-gateway"}


def test_reconciliation_uses_fallback_for_missing_service_baseline(monkeypatch):
    """缺少服务基线时，仅比较全局基线后的实际变更，不能无条件全量重建。"""
    module = _load_module()
    current = "c" * 40
    baselines = {service: "b" * 40 for service in module.SERVICE_NAMES if service != "hci-sim"}
    calls = []

    def fake_diff(base, _head):
        calls.append(base)
        return ["hci_sim/cmd/hci-sim/main.go"] if base == "a" * 40 else []

    monkeypatch.setattr(module, "changed_files_between", fake_diff)
    assert module.reconciliation_services(json.dumps(baselines), current, "a" * 40) == {"hci-sim"}
    assert "a" * 40 in calls


def test_reconciliation_does_not_rebuild_unknown_service_for_unrelated_change(monkeypatch):
    """首轮只发布 hci-sim 后，无关 main 合并不得触发未知服务补偿。"""
    module = _load_module()
    current = "c" * 40
    baselines = {"hci-sim": "b" * 40}
    monkeypatch.setattr(
        module,
        "changed_files_between",
        lambda _base, _head: ["terminal_bridge/cmd/terminal-bridge/main.go"],
    )
    assert module.reconciliation_services(json.dumps(baselines), current, "b" * 40) == {"terminal-bridge"}


def test_reconciliation_does_not_rebuild_hci_sim_for_unrelated_change(monkeypatch):
    """已有晋级请求基线后，无关 main 合并不得再次构建 hci-sim。"""
    module = _load_module()
    current = "c" * 40
    baselines = {service: "b" * 40 for service in module.SERVICE_NAMES}
    monkeypatch.setattr(
        module,
        "changed_files_between",
        lambda _base, _head: ["docs/deploy/发布指南.md"],
    )
    assert module.reconciliation_services(json.dumps(baselines), current, "b" * 40) == set()


def test_promotion_push_does_not_rebuild_released_hci_sim(monkeypatch, tmp_path):
    """promotion 是制品消费者，不能跨过已发布源码反向触发新制品。"""
    module = _load_module()
    release_base = "a" * 40
    event_before = "b" * 40
    current = "c" * 40
    promotion_file = "deploy/gitops/argo-apps/local/hci-sim-dev.yaml"
    baselines = {service: event_before for service in module.SERVICE_NAMES}
    output = tmp_path / "outputs"

    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_RELEASE_BASE_SHA", release_base)
    monkeypatch.setenv("GITHUB_EVENT_BEFORE", event_before)
    monkeypatch.setenv("GITHUB_SHA", current)
    monkeypatch.setenv("GITHUB_SERVICE_RELEASE_BASELINES", json.dumps(baselines))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **_kwargs):
        base = command[3]
        if base == release_base:
            return Result(f"hci_sim/internal/telemetry/telemetry.go\n{promotion_file}\n")
        assert base == event_before
        return Result(f"{promotion_file}\n")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.main() == 0

    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert json.loads(outputs["matrix"]) == []
    assert outputs["has_images"] == "false"
    assert outputs["db_migrate"] == "false"


def test_dsh_change_triggers_dsh_rebuild():
    """dsh/ 下改动必须触发 dsh 镜像构建。

    否则 DSH chart 的 image.tag 永远停在初始值，Pod 会一直 ImagePullBackOff。
    """
    module = _load_module()
    selected, _ = module.select_services(["dsh/docker-entrypoint.sh"], force_all=False)
    assert "dsh" in selected
    # deploy_key 必须存在，否则 tag 不会同步到 hci-platform-env 的 dsh.image.tag，
    # 而 DSH 位于独立 chart，没有 tag 覆写就会一直拉取初始值。
    deploy_keys = {service: key for service, _, key in module.ALL_SERVICES}
    assert deploy_keys.get("dsh") == "dsh"


def test_every_registered_service_dockerfile_exists():
    """每个注册服务都必须指向真实存在的 Dockerfile，防止路径写错后静默不构建。"""
    from pathlib import Path as _Path

    module = _load_module()
    repo_root = _Path(__file__).resolve().parents[2]
    missing = [
        f"{service} -> {dockerfile}"
        for service, dockerfile, _ in module.ALL_SERVICES
        if not (repo_root / dockerfile).is_file()
    ]
    assert not missing, f"注册的 Dockerfile 不存在: {missing}"
