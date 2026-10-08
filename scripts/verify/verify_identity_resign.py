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
5. SRC-L2 防回退静态守卫：conversation-service 的客户写入路由（exec-result /
   vm-console-* / bridge-logs）改为「网关签名 X-Client-ID + 工单归属校验」后，
   生产源码不得再出现历史占位符 token 字面量，也不得复活已删除的桩鉴权函数
   （`_check_user_session` / `_check_session_or_internal`）。此项不依赖 helm，
   无条件执行，防止「桩鉴权看似正常、实则 IDOR 防护被悄悄移除」的静默回退。

6. SRC-L1 防回退守卫：管理台身份上下文收敛后，admin-ui 的 nginx 不得再以
   `proxy_set_header` 静态写死或以 `$http_*` 透传 X-Tenant-ID / X-Actor-ID /
   X-User-ID（唯一可信来源是 api-gateway 对 admin JWT 的派生），admin-ui Deployment
   亦不得再注入 `ADMIN_API_*` 身份 env。此项与 customer 侧同款：透传客户端身份头
   比静态注入更危险（把浏览器伪造能力重新引入），故 nginx 检查为无条件静态守卫，
   Deployment env 前缀守卫依赖 helm 渲染。

   注：admin nginx 仍保留 `proxy_set_header Authorization $http_authorization;`
   （透明转发浏览器登录签发的 JWT，属正当用途），故 admin 侧静态检查仅禁止
   身份上下文头（X-Tenant-ID / X-Actor-ID / X-User-ID），不含 Authorization。

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

# SRC-L1：admin nginx 保留 Authorization 透传（浏览器 JWT 正当用途），仅禁止身份上下文头注入
ADMIN_INJECTION_HEADERS = (
    "proxy_set_header X-Tenant-ID",
    "proxy_set_header X-Actor-ID",
    "proxy_set_header X-User-ID",
)
ADMIN_INJECTION_ENV_PREFIXES = ("ADMIN_API_",)

# SRC-L2 防回退：占位符 token 字面量与已删除的桩鉴权函数不得在生产源码复活
PLACEHOLDER_LITERAL = "client-session-placeholder-token"
FORBIDDEN_STUB_DEFS = ("def _check_user_session", "def _check_session_or_internal")
SRC_L2_DIRS = (
    "backend/api-gateway/app",
    "backend/conversation-service/app",
    "frontend/customer/src",
    "frontend/admin/src",
)
SRC_L2_SUFFIXES = (".py", ".vue", ".ts", ".js")


def log(level: str, message: str) -> None:
    """带调用链的结构化日志行。"""
    print(f"[{TRACE_ID}] {level}  {message}")


def verify_src_l2_no_regression(root: Path, errors: list[str]) -> None:
    """SRC-L2 静态守卫：占位符 token / 桩鉴权函数不得出现在生产源码。

    不依赖 helm，无条件执行。仅扫描 app/src 生产目录（不含 tests / docs），
    因此负向断言测试中作为字符串出现的占位符不会被误判。
    """

    placeholder_hits: list[str] = []
    stub_hits: list[str] = []
    scanned = 0
    for rel_dir in SRC_L2_DIRS:
        base = root / rel_dir
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not (path.is_file() and path.suffix in SRC_L2_SUFFIXES):
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="ignore")
            if PLACEHOLDER_LITERAL in text:
                placeholder_hits.append(str(path.relative_to(root)))
            for stub in FORBIDDEN_STUB_DEFS:
                if stub in text:
                    stub_hits.append(f"{path.relative_to(root)}: {stub}")

    if placeholder_hits:
        errors.append(f"SRC-L2 占位符 token 在生产源码复发（身份桩鉴权可能回退）：{placeholder_hits}")
    elif stub_hits:
        errors.append(f"SRC-L2 已删除的桩鉴权函数在生产源码复活：{stub_hits}")
    else:
        log("OK", f"SRC-L2 防回退：生产源码无占位符 token / 桩鉴权函数（扫描 {scanned} 个文件）")


def verify_src_l1_admin_nginx(root: Path, errors: list[str]) -> None:
    """SRC-L1 静态守卫：admin nginx 不得注入 X-Tenant-ID / X-Actor-ID 身份头。

    管理台身份上下文唯一来源为 api-gateway 对 admin JWT 的派生；nginx 静态写死或
    以 `$http_*` 透传客户端身份头都会引入伪造风险（透传比静态注入更危险）。
    不依赖 helm，无条件执行。Authorization 透传（浏览器 JWT）属正当用途，不在此禁止之列。
    """

    admin_nginx = root / "frontend/admin/nginx.conf"
    if not admin_nginx.exists():
        errors.append(f"未找到 {admin_nginx}，无法校验管理台身份注入是否已删除")
        return
    leaked = [item for item in ADMIN_INJECTION_HEADERS if item in admin_nginx.read_text(encoding="utf-8")]
    if leaked:
        errors.append(f"admin nginx.conf 仍存在身份上下文注入指令（L1 必须删除）：{leaked}")
    else:
        log("OK", "admin nginx.conf 已无 X-Tenant-ID / X-Actor-ID 注入（Authorization 透传保留）")


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


def leaked_env_names(deploy: dict[str, Any], prefixes: tuple[str, ...]) -> list[str]:
    """返回 Deployment 容器 env 中命中指定前缀的名称列表。"""
    containers = deploy.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    return [
        env.get("name")
        for container in containers
        for env in (container.get("env") or [])
        if str(env.get("name", "")).startswith(prefixes)
    ]


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

    # ── 静态检查 2：SRC-L2 占位符 token / 桩鉴权函数防回退（不依赖 helm）──
    verify_src_l2_no_regression(root, errors)

    # ── 静态检查 3：SRC-L1 admin nginx 身份上下文头注入防回退（不依赖 helm）──
    verify_src_l1_admin_nginx(root, errors)

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
        leaked_env = leaked_env_names(customer_deploy, INJECTION_ENV_PREFIXES)
        if leaked_env:
            errors.append(f"customer-ui Deployment 仍注入服务端身份 env：{leaked_env}")
        else:
            log("OK", "customer-ui Deployment 已无身份注入 env")

    # ── 场景 1b：SRC-L1 admin-ui 不得注入 ADMIN_API_* 身份 env ─────────
    admin_deploy = find_by_name(documents, "Deployment", "admin-ui")
    if admin_deploy is None:
        errors.append("未渲染 admin-ui Deployment，无法校验 L1 身份注入 env")
    else:
        leaked_admin_env = leaked_env_names(admin_deploy, ADMIN_INJECTION_ENV_PREFIXES)
        if leaked_admin_env:
            errors.append(f"admin-ui Deployment 仍注入服务端身份 env（L1 必须删除）：{leaked_admin_env}")
        else:
            log("OK", "admin-ui Deployment 已无 ADMIN_API_* 身份注入 env")

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
