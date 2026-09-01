"""主干镜像发布基线解析的回归测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "resolve_release_baseline.py"
_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


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


def test_global_baseline_does_not_accept_partially_failed_run():
    """服务级基线可独立恢复，但全局发布基线仍只接受整轮成功。"""
    module = _load_module()
    failed_sha = "a" * 40
    baseline, source = module.select_baseline(
        [{"head_sha": failed_sha, "conclusion": "failure"}],
        {failed_sha},
        current_sha="b" * 40,
        fallback_sha="c" * 40,
    )
    assert baseline == "c" * 40
    assert source == "event_before_fallback"


def test_release_requires_build_and_promotion_success():
    module = _load_module()
    assert module.is_successful_release(
        [
            {"name": "构建并推送镜像（admin-ui）", "conclusion": "success"},
            {"name": "晋级非生产环境", "conclusion": "success"},
        ]
    )
    assert not module.is_successful_release(
        [
            {"name": "构建并推送镜像（admin-ui）", "conclusion": "success"},
            {"name": "晋级非生产环境", "conclusion": "failure"},
        ]
    )


def test_service_baseline_requires_its_own_build_and_promotion():
    module = _load_module()
    jobs = [
        {"name": "构建并推送镜像（admin-ui）", "conclusion": "success"},
        {"name": "晋级非生产环境", "conclusion": "success"},
    ]
    assert module.successful_service_baselines(jobs) == {"admin-ui"}


def test_hci_sim_baseline_requires_persisted_promotion_request():
    module = _load_module()
    jobs = [
        {"name": "构建并推送镜像（hci-sim）", "conclusion": "success"},
        {"name": "晋级非生产环境", "conclusion": "success"},
    ]
    assert module.successful_service_baselines(jobs) == set()
    jobs.append({"name": "hci-sim-promotion-request", "conclusion": "success"})
    assert module.successful_service_baselines(jobs) == {"hci-sim"}


def test_hci_sim_does_not_accept_obsolete_job_name():
    """展示名漂移不得再次把旧 job 误认成当前晋级协议。"""
    module = _load_module()
    jobs = [
        {"name": "构建并推送镜像（hci-sim）", "conclusion": "success"},
        {"name": "校验 hci-sim immutable GitOps digest", "conclusion": "success"},
    ]
    assert module.successful_service_baselines(jobs) == set()


def test_hci_sim_promotion_job_name_matches_workflow_contract():
    """基线解析器与 Actions job 必须共享同一个稳定机器名。"""
    module = _load_module()
    lines = _WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("  promote-hci-sim-dev:")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  ") and not lines[index].startswith("    ")
        ),
        len(lines),
    )
    job_block = lines[start:end]
    assert f"    name: {module.HCI_SIM_PROMOTION_JOB_NAME}" in job_block


def test_hci_sim_promotion_workflow_targets_dev_and_staging_applications():
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "deploy/gitops/argo-apps/local/hci-sim-dev.yaml" in workflow
    assert "deploy/gitops/argo-apps/cloud/hci-sim-staging.yaml" in workflow
    assert 'promotion_target="dev"' in workflow
    assert 'promotion_target="dev/staging"' in workflow
    assert "staging)\n                app_files=" in workflow
    assert "ENVIRONMENTS=(dev)" in workflow
    assert "staging) ENVIRONMENTS=(staging) ;;" in workflow
    assert "prod) ENVIRONMENTS=(prod) ;;" in workflow


def test_hci_sim_promotion_pr_uses_owner_pat_and_fail_closed_actor_check():
    """自动晋级 PR 必须来自仓库 owner，不能退回 bot token 或隐式信任。"""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "token: ${{ secrets.ENV_REPO_PAT }}" in workflow
    assert "GH_TOKEN: ${{ secrets.ENV_REPO_PAT }}" in workflow
    assert "automation_actor=\"$(gh api user --jq '.login')\"" in workflow
    assert 'expected_actor="${GITHUB_REPOSITORY_OWNER}"' in workflow
    assert '"${automation_actor}" != "${expected_actor}"' in workflow
    assert "--json url,author" in workflow
    assert 'gh pr close "${pr_url}"' in workflow
    assert "旧 bot-authored 候选" in workflow


def test_partial_workflow_failure_still_records_hci_sim_service_baseline(monkeypatch):
    """无关矩阵失败不能抹掉已经持久化的 hci-sim 晋级请求。"""
    module = _load_module()
    source_sha = "a" * 40
    current_sha = "b" * 40
    fallback_sha = "c" * 40
    endpoints = []

    def fake_gh_json(endpoint):
        endpoints.append(endpoint)
        if "/runs?" in endpoint:
            return {
                "workflow_runs": [
                    {"id": 123, "head_sha": source_sha, "conclusion": "failure"},
                ]
            }
        return {
            "jobs": [
                {"name": "构建并推送镜像（hci-sim）", "conclusion": "success"},
                {"name": "hci-sim-promotion-request", "conclusion": "success"},
            ]
        }

    outputs = {}
    monkeypatch.setattr(module, "gh_json", fake_gh_json)
    monkeypatch.setattr(module, "write_output", outputs.__setitem__)
    monkeypatch.setenv("GITHUB_REPOSITORY", "tomturing/hci-troubleshoot-platform")
    monkeypatch.setenv("CURRENT_SHA", current_sha)
    monkeypatch.setenv("EVENT_BEFORE", fallback_sha)

    assert module.main() == 0
    assert "status=completed" in endpoints[0]
    assert json.loads(outputs["service_baselines"])["hci-sim"] == source_sha
