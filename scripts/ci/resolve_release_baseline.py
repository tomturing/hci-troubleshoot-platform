#!/usr/bin/env python3
"""解析主干最近一次成功镜像发布的 commit，防止空 diff 吞掉发布。

GitHub main push 可能连续到达。若上一轮 CI 在镜像构建阶段被取消，下一轮
push 即使与上一轮拥有相同 Git tree，也必须从最近一次成功镜像发布继续计算
变更范围，而不是把本轮误判为纯文档/无镜像变更。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_JOB_PREFIX = "构建并推送镜像（"
PROMOTION_JOB_NAME = "晋级非生产环境"
HCI_SIM_VERIFY_JOB_NAME = "校验 hci-sim immutable GitOps digest"
SERVICE_NAMES = (
    "api-gateway",
    "case-service",
    "conversation-service",
    "agent-service",
    "eval-service",
    "diagnosis-service",
    "scheduler-service",
    "kb-service",
    "customer-ui",
    "admin-ui",
    "terminal-bridge",
    "hci-sim",
)


def select_baseline(
    runs: Iterable[dict[str, object]],
    successful_release_heads: set[str],
    *,
    current_sha: str,
    fallback_sha: str,
) -> tuple[str, str]:
    """从成功且确实构建过业务镜像的 run 中选择最新基线。"""
    for run in runs:
        head_sha = str(run.get("head_sha", ""))
        if head_sha == current_sha or str(run.get("conclusion", "")) != "success":
            continue
        if head_sha in successful_release_heads:
            return head_sha, "last_successful_image_release_and_promotion"
    return fallback_sha, "event_before_fallback"


def is_successful_release(jobs: object) -> bool:
    """只有镜像构建和非生产晋级都成功，才算可复用发布基线。"""
    if not isinstance(jobs, list):
        return False
    has_image_build = any(
        isinstance(job, dict)
        and str(job.get("conclusion", "")) == "success"
        and str(job.get("name", "")).startswith(IMAGE_JOB_PREFIX)
        for job in jobs
    )
    has_promotion = any(
        isinstance(job, dict)
        and str(job.get("conclusion", "")) == "success"
        and str(job.get("name", "")) == "晋级非生产环境"
        for job in jobs
    )
    return has_image_build and has_promotion


def successful_service_releases(jobs: object) -> set[str]:
    """返回本次运行中确实完成发布闭环的服务。

    不能以“任意镜像成功”推断全部服务均已发布：矩阵通常只构建本次变更
    服务。hci-sim 不写入环境仓库，必须额外通过 immutable GitOps digest 校验。
    """
    if not isinstance(jobs, list):
        return set()
    names = {
        str(job.get("name", "")): str(job.get("conclusion", ""))
        for job in jobs
        if isinstance(job, dict)
    }
    promoted = names.get(PROMOTION_JOB_NAME) == "success"
    hci_sim_verified = names.get(HCI_SIM_VERIFY_JOB_NAME) == "success"
    released = set()
    for service in SERVICE_NAMES:
        if names.get(f"{IMAGE_JOB_PREFIX}{service}）") != "success":
            continue
        if service == "hci-sim":
            if hci_sim_verified:
                released.add(service)
        elif promoted:
            released.add(service)
    return released


def gh_json(endpoint: str) -> dict[str, object]:
    result = subprocess.run(
        ["gh", "api", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError(f"GitHub API 返回非对象：{endpoint}")
    return payload


def write_output(name: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    current_sha = os.environ.get("CURRENT_SHA", "")
    fallback_sha = os.environ.get("EVENT_BEFORE", "")
    if not repository or not SHA_RE.fullmatch(current_sha):
        print("缺少合法 GITHUB_REPOSITORY/CURRENT_SHA", file=sys.stderr)
        return 1
    if not SHA_RE.fullmatch(fallback_sha) or fallback_sha == "0" * 40:
        print("缺少合法 EVENT_BEFORE，无法建立发布基线", file=sys.stderr)
        return 1

    runs_payload = gh_json(
        f"repos/{repository}/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=50"
    )
    runs = runs_payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        raise ValueError("GitHub API 缺少 workflow_runs")

    successful_release_heads: set[str] = set()
    service_baselines: dict[str, str] = {}
    for run in runs:
        if not isinstance(run, dict) or str(run.get("head_sha", "")) == current_sha:
            continue
        run_id = run.get("id")
        if not run_id or str(run.get("conclusion", "")) != "success":
            continue
        jobs_payload = gh_json(f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100")
        jobs = jobs_payload.get("jobs", [])
        if is_successful_release(jobs):
            successful_release_heads.add(str(run.get("head_sha", "")))
        for service in successful_service_releases(jobs):
            service_baselines.setdefault(service, str(run.get("head_sha", "")))
        # API 返回按时间倒序；所有服务都已有基线后无需继续请求 Jobs API。
        if len(service_baselines) == len(SERVICE_NAMES):
            break

    baseline_sha, source = select_baseline(
        runs,
        successful_release_heads,
        current_sha=current_sha,
        fallback_sha=fallback_sha,
    )
    if not SHA_RE.fullmatch(baseline_sha):
        print(f"解析出的发布基线非法：{baseline_sha}", file=sys.stderr)
        return 1
    print(f"发布基线：{baseline_sha}（{source}）")
    write_output("base_sha", baseline_sha)
    write_output("source", source)
    write_output("service_baselines", json.dumps(service_baselines, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
