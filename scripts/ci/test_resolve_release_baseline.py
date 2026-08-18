"""主干镜像发布基线解析的回归测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "resolve_release_baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("resolve_release_baseline", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selects_latest_successful_image_release_and_skips_current_run():
    module = _load_module()
    runs = [
        {"head_sha": "c" * 40, "conclusion": "success"},
        {"head_sha": "b" * 40, "conclusion": "success"},
        {"head_sha": "a" * 40, "conclusion": "success"},
    ]
    baseline, source = module.select_baseline(
        runs,
        {"b" * 40},
        current_sha="c" * 40,
        fallback_sha="d" * 40,
    )
    assert baseline == "b" * 40
    assert source == "last_successful_image_release_and_promotion"


def test_falls_back_to_event_before_when_no_image_release_exists():
    module = _load_module()
    baseline, source = module.select_baseline(
        [{"head_sha": "a" * 40, "conclusion": "success"}],
        set(),
        current_sha="b" * 40,
        fallback_sha="c" * 40,
    )
    assert baseline == "c" * 40
    assert source == "event_before_fallback"


def test_release_requires_build_and_promotion_success():
    module = _load_module()
    assert module.is_successful_release([
        {"name": "构建并推送镜像（admin-ui）", "conclusion": "success"},
        {"name": "晋级非生产环境", "conclusion": "success"},
    ])
    assert not module.is_successful_release([
        {"name": "构建并推送镜像（admin-ui）", "conclusion": "success"},
        {"name": "晋级非生产环境", "conclusion": "failure"},
    ])


def test_service_release_requires_its_own_build_and_promotion():
    module = _load_module()
    jobs = [
        {"name": "构建并推送镜像（admin-ui）", "conclusion": "success"},
        {"name": "晋级非生产环境", "conclusion": "success"},
    ]
    assert module.successful_service_releases(jobs) == {"admin-ui"}


def test_hci_sim_requires_immutable_gitops_verification():
    module = _load_module()
    jobs = [
        {"name": "构建并推送镜像（hci-sim）", "conclusion": "success"},
        {"name": "晋级非生产环境", "conclusion": "success"},
    ]
    assert module.successful_service_releases(jobs) == set()
    jobs.append({"name": "校验 hci-sim immutable GitOps digest", "conclusion": "success"})
    assert module.successful_service_releases(jobs) == {"hci-sim"}
