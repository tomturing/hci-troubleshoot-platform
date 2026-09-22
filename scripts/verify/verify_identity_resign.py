#!/usr/bin/env python3
"""校验网关身份重签（B1，SRC-2026-5358）的渲染契约。

背景：internal 模式下 customer-ui 的 Nginx 会为 /api 注入内部令牌与管理员
操作者身份，使匿名请求经该代理即获得管理员权限。B1 删除注入并把 /api 收回到
api-gateway，身份改由网关依据服务端签发的身份 Cookie 重签。

本脚本防止未来 chart / 前端改动**静默恢复**注入链（此类回退在功能上完全
"正常"，只有安全属性被悄悄丢掉，极难在评审中发现）：

1. customer-ui Deployment 不得再出现 CUSTOMER_API_* 注入 env；
2. customer 前端 nginx.conf 不得再注入 Authorization / X-Tenant-ID / X-Actor-ID；
3. 主 ingress 的 /api 必须直达 api-gateway（非 customer-ui）；
4. admin-ui Ingress 的公网封禁开关默认关闭；开启后必须按**连字符格式**
   引用独立的 admin-guard 中间件（斜杠格式会让 Traefik 静默回退，封禁失效）。

用法：
    uv run python scripts/verify/verify_identity_resign.py
    uv run python scripts/verify/verify_identity_resign.py --require-helm  # CI
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

# 唯一调用链：本次校验执行的可追踪标识，输出到全部日志行
TRACE_ID = uuid.uuid4().hex

INJECTION_HEADERS = ("proxy_set_header Authorization", "proxy_set_header X-Tenant-ID", "proxy_set_header X-Actor-ID")
INJECTION_ENV_PREFIXES = ("CUSTOMER_API_",)


def log(level: str, message: str) -> None:
    """带调用链的结构化日志行。"""
    print(f"[{TRACE_ID}] {level}  {message}")


def helm_render(root: Path, release: str, extra_sets: list[str]) -> str:
    """执行 helm template 并返回渲染产物文本。"""

    chart = root / "deploy/helm/hci-platform"
    command = [
        "helm",
        "template",
        release,
        str(chart),
        "--set",
        "diagnosisService.enabled=true",
        "--set",
        "diagnosisService.identityMode=internal",
        "--set",
        "diagnosisService.internalIdentity.tenantId=verify-tenant",
        "--set",
        "diagnosisService.internalIdentity.adminActorId=verify-admin-ui",
        "--set",
        "diagnosisService.existingCredentialsSecret=identity-resign-credentials",
        "--set",
        "diagnosisService.allowedOrigins=http://localhost:3001",
        "--set",
        "diagnosisService.directUploadBaseUrl=/",
        "--set",
        "global.publicUrl=https://resign.example.com:4443",
        *extra_sets,
    ]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"helm template 失败：{result.stderr.strip()}")
    return result.stdout


def parse_documents(rendered: str) -> list[dict[str, Any]]:
    """解析渲染产物中的全部 YAML 文档。"""
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def find_by_name(documents: list[dict[str, Any]], kind: str, name_suffix: str) -> dict[str, Any] | None:
    for doc in documents:
        if doc.get("kind") == kind and str(doc.get("metadata", {}).get("name", "")).endswith(name_suffix):
            return doc
    return None


def verify(root: Path, require_helm: bool = False) -> list[str]:
    """执行全部断言，返回错误列表（空 = 通过）。"""

    errors: list[str] = []

    # ── 静态检查 1：customer 前端不得再注入身份头 ─────────────────────
    customer_nginx = root / "frontend/customer/nginx.conf"
    if not customer_nginx.exists():
        errors.append(f"未找到 {customer_nginx}，无法校验注入是否已删除")
    else:
        leaked = [item for item in INJECTION_HEADERS if item in customer_nginx.read_text(encoding="utf-8")]
        if leaked:
            errors.append(f"customer nginx.conf 仍存在身份注入指令（B1 必须删除）：{leaked}")
        else:
            log("OK", "customer nginx.conf 已无身份注入指令")

    if not shutil.which("helm"):
        if require_helm:
            errors.append("CI 要求 Helm 渲染校验，但未安装 helm")
            return errors
        log("INFO", "未安装 helm，跳过渲染校验")
        return errors

    rendered = helm_render(root, "resign-check", [])
    documents = parse_documents(rendered)

    # ── 场景 1：customer-ui 不得注入身份 env ─────────────────────────
    customer_deploy = find_by_name(documents, "Deployment", "customer-ui")
    if customer_deploy is None:
        errors.append("未渲染 customer-ui Deployment，无法校验注入 env")
    else:
        containers = customer_deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        leaked_env = [
            env.get("name")
            for container in containers
            for env in (container.get("env") or [])
            if str(env.get("name", "")).startswith(INJECTION_ENV_PREFIXES)
        ]
        if leaked_env:
            errors.append(f"customer-ui Deployment 仍注入服务端身份 env：{leaked_env}")
        else:
            log("OK", "customer-ui Deployment 已无身份注入 env")

    # ── 场景 2：/api 必须直达网关 ────────────────────────────────────
    main_ingress = find_by_name(documents, "Ingress", "hci-ingress")
    if main_ingress is None:
        errors.append("未渲染主 ingress，无法校验 /api 转发目标")
    else:
        api_backend = None
        for rule in main_ingress.get("spec", {}).get("rules", []):
            for path_item in rule.get("http", {}).get("paths", []):
                if str(path_item.get("path")) == "/api":
                    api_backend = path_item.get("backend", {}).get("service", {}).get("name")
                    break
            if api_backend:
                break
        if api_backend is None:
            errors.append("主 ingress 缺少 /api 路径（客户 API 链路断裂）")
        elif api_backend != "api-gateway":
            errors.append(f"主 ingress 的 /api 仍指向 {api_backend}，身份注入链可能复现（应为 api-gateway）")
        else:
            log("OK", "主 ingress /api → api-gateway（不再经 customer-ui 注入）")

    # ── 场景 3：管理入口封禁开关默认关闭 ──────────────────────────────
    rendered_admin = helm_render(root, "resign-admin", ["--set", "adminUI.ingress.enabled=true"])
    admin_ingress = find_by_name(parse_documents(rendered_admin), "Ingress", "admin-ui-ingress")
    if admin_ingress is None:
        errors.append("未渲染 admin-ui-ingress，无法校验管理入口封禁开关")
    else:
        annotation_key = "traefik.ingress.kubernetes.io/router.middlewares"
        default_annotation = (admin_ingress.get("metadata", {}).get("annotations", {}) or {}).get(annotation_key)
        if default_annotation:
            errors.append(f"管理入口封禁默认应关闭，但已绑定中间件：{default_annotation!r}")
        else:
            log("OK", "管理入口封禁默认关闭（不改变既有访问方式）")

    # ── 场景 4：开启后按连字符格式引用中间件 ──────────────────────────
    rendered_guard = helm_render(
        root,
        "resign-admin-guard",
        ["--set", "adminUI.ingress.enabled=true", "--set", "adminUI.ingress.internalGuard.enabled=true"],
    )
    admin_guard = find_by_name(parse_documents(rendered_guard), "Ingress", "admin-ui-ingress")
    if admin_guard is not None:
        annotation = (admin_guard.get("metadata", {}).get("annotations", {}) or {}).get(
            "traefik.ingress.kubernetes.io/router.middlewares", ""
        )
        if not annotation.endswith("-admin-guard@kubernetescrd"):
            errors.append(f"管理入口未按连字符格式绑定 admin-guard 中间件：annotation={annotation!r}")
        elif "/" in annotation.split("@")[0]:
            errors.append(f"管理入口中间件引用误用斜杠格式（Traefik 会静默回退）：{annotation!r}")
        else:
            log("OK", f"管理入口开启后已绑定中间件：{annotation}")

    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="校验网关身份重签（B1）的渲染契约")
    parser.add_argument("--require-helm", action="store_true", help="CI 模式：未安装 helm 视为失败")
    args = parser.parse_args()
    log("INFO", f"开始校验 B1 身份重签渲染契约 trace_id={TRACE_ID}")
    failures = verify(Path(__file__).resolve().parents[2], require_helm=args.require_helm)
    for failure in failures:
        log("ERROR", failure)
    if failures:
        log("FAIL", f"校验失败：{len(failures)} 项")
        sys.exit(1)
    log("PASS", "B1 身份重签渲染契约全部通过")
