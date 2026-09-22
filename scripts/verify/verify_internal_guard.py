#!/usr/bin/env python3
"""校验 internal 管理面公网封禁（SRC-2026-5358）的 Helm 渲染契约。

背景：internal 模式下主 ingress 将 /api 送 customer-ui 注入服务端身份，
匿名可穿透访问 /api/internal 管理面。修复采用独立 guard Ingress
（Host+PathPrefix(/api/internal)，规则更长优先命中）+ Traefik ipAllowList
Middleware 仅放行内网源。本脚本防止未来 chart 改动静默移除该护栏：

1. internal 模式默认渲染：Middleware 与 guard Ingress 必须存在且配置正确；
2. host 一致性：guard 与主 ingress 必须落在同一 host（否则规则不命中）；
3. 不误伤：主 ingress 的 /api 路径必须仍然存在（客户功能不受影响）；
4. 关闭开关：internalGuard.enabled=false 时不渲染任何 guard 资源。

用法：
    uv run python scripts/verify/verify_internal_guard.py                # 本地无 helm 也可跑（跳过）
    uv run python scripts/verify/verify_internal_guard.py --require-helm # CI 强制要求 helm
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


def log(level: str, message: str) -> None:
    """带调用链的结构化日志行。"""
    print(f"[{TRACE_ID}] {level}  {message}")


def helm_render(root: Path, release: str, extra_sets: list[str]) -> str:
    """执行 helm template 并返回渲染产物文本。

    渲染参数与 verify_config_contract.py 保持一致：internal 模式必填的
    签名私钥通过 existingCredentialsSecret 指向占位 secret 绕过。
    """
    chart = root / "deploy/helm/hci-platform"
    command = [
        "helm",
        "template",
        release,
        str(chart),
        "--set",
        "diagnosisService.enabled=true",
        "--set",
        "diagnosisService.existingCredentialsSecret=config-contract-credentials",
        "--set",
        "diagnosisService.allowedOrigins=http://localhost:3001",
        "--set",
        "diagnosisService.directUploadBaseUrl=/",
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
    helm = shutil.which("helm")
    if not helm:
        if require_helm:
            errors.append("CI 要求 Helm 渲染校验，但未安装 helm")
            return errors
        log("INFO", "未安装 helm，跳过渲染校验（仅静态检查已无意义，直接通过）")
        return errors

    base_sets = [
        "--set",
        "diagnosisService.enabled=true",
        "--set",
        "diagnosisService.identityMode=internal",
        "--set",
        "global.publicUrl=https://guard.example.com:4443",
    ]

    # ── 场景 1：internal 模式默认开启，guard 资源必须渲染 ──────────────
    rendered = helm_render(root, "guard-check", base_sets)
    documents = parse_documents(rendered)

    middleware = find_by_name(documents, "Middleware", "-internal-guard")
    if middleware is None:
        errors.append("internal 模式下未渲染 internal-guard Middleware（公网封禁护栏缺失）")
    else:
        source_range = (middleware.get("spec", {}).get("ipAllowList", {}) or {}).get("sourceRange") or []
        if not source_range:
            errors.append("internal-guard Middleware 缺少 ipAllowList.sourceRange，公网将全量放行")
        else:
            log("OK", f"Middleware sourceRange={source_range}")

    guard_ingress = find_by_name(documents, "Ingress", "-internal-guard")
    main_ingress = find_by_name(documents, "Ingress", "hci-ingress")
    if guard_ingress is None:
        errors.append("internal 模式下未渲染 internal-guard Ingress（/api/internal 无专用规则）")
    else:
        paths = guard_ingress.get("spec", {}).get("rules", [{}])[0].get("http", {}).get("paths", [])
        guard_path = next((p for p in paths if str(p.get("path")) == "/api/internal"), None)
        if guard_path is None:
            errors.append("guard Ingress 未包含 /api/internal 路径")
        elif guard_path.get("backend", {}).get("service", {}).get("name") != "customer-ui":
            errors.append("guard Ingress backend 应指向 customer-ui（与主链路一致）")
        else:
            log("OK", "guard Ingress /api/internal → customer-ui 配置正确")

        annotation = (guard_ingress.get("metadata", {}).get("annotations", {}) or {}).get(
            "traefik.ingress.kubernetes.io/router.middlewares", ""
        )
        # Ingress annotation 引用格式必须为「<namespace>-<name>@kubernetescrd」
        # （连字符拼接）。斜杠格式仅适用于 IngressRoute spec，误用会导致
        # traefik 报 "middleware does not exist" 且路由静默回退到主规则。
        if not annotation.endswith("-internal-guard@kubernetescrd"):
            errors.append(f"guard Ingress 未按连字符格式绑定中间件：annotation={annotation!r}")
        elif "/" in annotation.split("@")[0]:
            errors.append(f"guard Ingress 中间件引用误用斜杠格式（应为 ns-name@kubernetescrd）：{annotation!r}")
        else:
            log("OK", f"guard Ingress 已按连字符格式绑定中间件 {annotation}")

    # ── 场景 2：host 一致性（guard 与主 ingress 落在同一 host）────────
    if guard_ingress is not None and main_ingress is not None:

        def rule_host(doc: dict[str, Any]) -> str:
            return str(doc.get("spec", {}).get("rules", [{}])[0].get("host") or "")

        if rule_host(guard_ingress) != rule_host(main_ingress):
            errors.append(
                f"guard host={rule_host(guard_ingress)!r} 与主 ingress host={rule_host(main_ingress)!r} "
                "不一致，Traefik 将不会命中 guard 规则"
            )
        else:
            log("OK", f"guard 与主 ingress host 一致：{rule_host(guard_ingress)!r}")

    # ── 场景 3：不误伤（主 ingress 的 /api 仍在，客户功能不受影响）────
    if main_ingress is None:
        errors.append("internal 模式下主 ingress hci-ingress 消失（guard 改动破坏了客户链路）")
    else:
        for rule in main_ingress.get("spec", {}).get("rules", []):
            for path_item in rule.get("http", {}).get("paths", []):
                if str(path_item.get("path")) == "/api":
                    log("OK", "主 ingress 的 /api 路径仍在，客户流量不受影响")
                    break
            else:
                continue
            break
        else:
            errors.append("主 ingress 丢失 /api 路径，客户功能将被 guard 改动破坏")

    # ── 场景 4：关闭开关生效（enabled=false 不渲染任何 guard 资源）────
    rendered_off = helm_render(root, "guard-check-off", [*base_sets, "--set", "ingress.internalGuard.enabled=false"])
    documents_off = parse_documents(rendered_off)
    leftovers = [
        doc.get("metadata", {}).get("name")
        for doc in documents_off
        if str(doc.get("metadata", {}).get("name", "")).endswith("-internal-guard")
    ]
    if leftovers:
        errors.append(f"internalGuard.enabled=false 时仍渲染了 guard 资源：{leftovers}")
    else:
        log("OK", "internalGuard.enabled=false 时 guard 资源正确消失")

    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="校验 internal 管理面公网封禁的 Helm 渲染契约")
    parser.add_argument("--require-helm", action="store_true", help="CI 模式：未安装 helm 视为失败")
    args = parser.parse_args()
    log("INFO", f"开始校验 internal guard 渲染契约 trace_id={TRACE_ID}")
    failures = verify(Path(__file__).resolve().parents[2], require_helm=args.require_helm)
    for failure in failures:
        log("ERROR", failure)
    if failures:
        log("FAIL", f"校验失败：{len(failures)} 项")
        sys.exit(1)
    log("PASS", "internal guard 渲染契约全部通过")
