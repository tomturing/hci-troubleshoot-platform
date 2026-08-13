#!/usr/bin/env python3
"""根据 Git 变更生成 main 发布所需的最小镜像构建与部署计划。

该脚本只解决“哪些镜像必须重新构建”的问题，不决定 PR 质量门禁。它被
``ci.yml`` 的变更探测 job 调用，输出 GitHub Actions job output：

* ``matrix``：build-and-push 使用的动态 matrix；
* ``has_images``：本次是否需要构建业务镜像；
* ``deploy_services``：需要写入环境仓库 values 的服务 key；
* ``has_deploy_services``：是否需要更新业务服务 tag；
* ``db_migrate``：是否需要构建并发布 db-migrate 镜像。

安全优先级：共享依赖、前端共享依赖及无法归类的 Docker 构建输入均向上
扩散到全部相关镜像；仅文档、Helm values、测试和 CI 配置变更不会产生镜像。
手动触发没有可靠 diff，故意执行全量构建，作为发布复核逃生通道。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable

BACKEND_SERVICES = (
    ("api-gateway", "backend/api-gateway/Dockerfile", "apiGateway"),
    ("case-service", "backend/case-service/Dockerfile", "caseService"),
    ("conversation-service", "backend/conversation-service/Dockerfile", "conversationService"),
    ("agent-service", "backend/agent-service/Dockerfile", "agentService"),
    ("eval-service", "backend/eval-service/Dockerfile", "evalService"),
    ("diagnosis-service", "backend/diagnosis-service/Dockerfile", "diagnosisService"),
    ("scheduler-service", "backend/scheduler-service/Dockerfile", "schedulerService"),
    ("kb-service", "backend/kb-service/Dockerfile", "kbService"),
)
FRONTEND_SERVICES = (
    ("customer-ui", "frontend/customer/Dockerfile", "customerUI"),
    ("admin-ui", "frontend/admin/Dockerfile", "adminUI"),
)
OTHER_SERVICES = (
    ("terminal-bridge", "terminal_bridge/Dockerfile", "terminalBridge"),
    # hci-sim 由独立 chart 消费，不同步到 hci-platform-env。
    ("hci-sim", "hci_sim/Dockerfile", None),
)
ALL_SERVICES = BACKEND_SERVICES + FRONTEND_SERVICES + OTHER_SERVICES

DB_MIGRATE_PATHS = (
    "database/desired_schema.sql",
    "database/atlas-migrations/",
    "database/desired_extras.sql",
    "database/data-migrations/",
    "Dockerfile.migrations",
    "scripts/db-migrate.sh",
    "scripts/migration-runner.sh",
)


def is_under(path: str, prefix: str) -> bool:
    return path == prefix.rstrip("/") or path.startswith(prefix)


def changed_files() -> list[str]:
    """返回当前事件的完整变更范围；手动触发返回空列表，由调用方转为全量。"""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "workflow_dispatch":
        return []

    if event_name == "pull_request":
        base = os.environ.get("GITHUB_BASE_SHA", "")
        head = os.environ.get("GITHUB_HEAD_SHA", "")
    else:
        base = os.environ.get("GITHUB_EVENT_BEFORE", "")
        head = os.environ.get("GITHUB_SHA", "")

    if not base or not head or base == "0" * 40:
        raise ValueError("事件缺少可比较的 base/head SHA；请使用 workflow_dispatch 执行全量发布复核")

    result = subprocess.run(
        ["git", "diff", "--name-only", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def add_service(selected: set[str], service: str) -> None:
    selected.add(service)


def select_services(paths: Iterable[str], *, force_all: bool) -> tuple[set[str], bool]:
    """按 Dockerfile COPY 边界计算服务闭包，并返回 (services, db_migrate)。"""
    selected: set[str] = set()
    db_migrate = force_all

    if force_all:
        return {service for service, _, _ in ALL_SERVICES}, True

    for path in paths:
        if any(is_under(path, watched) for watched in DB_MIGRATE_PATHS):
            db_migrate = True

        # Dockerfile 明确 COPY backend/shared 到每个后端镜像；根 pyproject/uv.lock
        # 只服务仓库级测试，不会进入这些镜像，不能把它们错误放大为全量发布。
        if path == ".dockerignore":
            selected.update(service for service, _, _ in ALL_SERVICES)
            continue
        if is_under(path, "backend/shared/"):
            selected.update(service for service, _, _ in BACKEND_SERVICES)
            continue

        # pnpm 工作区/共享包会被两个 UI Dockerfile COPY。
        if (
            is_under(path, "frontend/shared/")
            or path in {
                "frontend/package.json",
                "frontend/pnpm-lock.yaml",
                "frontend/pnpm-workspace.yaml",
                "frontend/.npmrc",
            }
        ):
            selected.update(service for service, _, _ in FRONTEND_SERVICES)
            continue

        for service, dockerfile, _ in BACKEND_SERVICES:
            directory = dockerfile.removesuffix("Dockerfile")
            if is_under(path, directory) or path == dockerfile:
                add_service(selected, service)
                break
        else:
            for service, dockerfile, _ in FRONTEND_SERVICES:
                directory = dockerfile.removesuffix("Dockerfile")
                if is_under(path, directory) or path == dockerfile:
                    add_service(selected, service)
                    break
            else:
                if is_under(path, "terminal_bridge/"):
                    add_service(selected, "terminal-bridge")
                elif is_under(path, "hci_sim/"):
                    add_service(selected, "hci-sim")

    return selected, db_migrate


def emit(name: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    force_all = event_name == "workflow_dispatch"
    try:
        paths = changed_files()
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"无法生成最小构建计划：{error}", file=sys.stderr)
        return 1

    selected, db_migrate = select_services(paths, force_all=force_all)
    by_name = {service: (dockerfile, deploy_key) for service, dockerfile, deploy_key in ALL_SERVICES}
    ordered = [service for service, _, _ in ALL_SERVICES if service in selected]
    matrix = [
        {"service": service, "context": ".", "dockerfile": by_name[service][0]}
        for service in ordered
    ]
    deploy_services = [by_name[service][1] for service in ordered if by_name[service][1]]

    print("发布构建计划：")
    print(f"  event: {event_name}")
    print(f"  changed files: {len(paths)}")
    print(f"  images: {', '.join(ordered) or '无'}")
    print(f"  deploy services: {', '.join(deploy_services) or '无'}")
    print(f"  db-migrate: {'是' if db_migrate else '否'}")

    emit("matrix", json.dumps(matrix, separators=(",", ":")))
    emit("has_images", "true" if matrix else "false")
    emit("deploy_services", ",".join(deploy_services))
    emit("has_deploy_services", "true" if deploy_services else "false")
    emit("db_migrate", "true" if db_migrate else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
