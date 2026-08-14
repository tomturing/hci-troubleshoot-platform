"""resolve_image_build_plan 的回归测试。

核心防护目标：共享依赖 backend/shared/ 的任意变更必须触发全部后端服务
（含 kbService）重建并推进 tag。历史上因 shared 变更只推进了部分后端服务，
导致 agent-service 与 kb-service 的 signals 指纹分叉，引发 KBD 23821 诊断
tool_contract_revision stale 故障。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

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
