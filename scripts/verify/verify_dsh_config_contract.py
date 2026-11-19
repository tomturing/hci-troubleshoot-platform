#!/usr/bin/env python3
"""校验 DSH Compose 输入、Helm values 与容器运行时变量的语义映射。"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

COMPOSE_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
ENV_LINE = re.compile(r"^(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$")


def value_at(values: Any, path: str) -> tuple[bool, Any]:
    current = values
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[match.group(1)] = value
    return result


def merge_values(base: Any, overlay: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    merged = dict(base)
    for key, value in overlay.items():
        merged[key] = merge_values(merged[key], value) if key in merged else value
    return merged


def normalized(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ""
    return str(value).strip()


def chart_files(root: Path, relative: str) -> tuple[dict[str, Any], str]:
    chart = root / relative
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8")) or {}
    templates = "\n".join(path.read_text(encoding="utf-8") for path in (chart / "templates").rglob("*.yaml"))
    return values, templates


def compare_values(mapping: dict[str, Any], compose_value: str, helm_values: dict[str, Any]) -> bool:
    actual: list[Any] = []
    for path in mapping.get("helm_values") or []:
        exists, value = value_at(helm_values, path)
        if not exists:
            return False
        actual.append(value)

    comparison = mapping.get("comparison", "identity")
    if comparison == "space_list":
        return actual == [compose_value.split()]
    if comparison == "url_with_ingress_path":
        if len(actual) != 2:
            return False
        public_url, ingress_path = (normalized(item) for item in actual)
        expected = f"{public_url.rstrip('/')}/{ingress_path.strip('/')}/"
        return compose_value.rstrip("/") + "/" == expected
    if comparison == "host_from_url":
        return len(actual) == 1 and compose_value == (urlsplit(normalized(actual[0])).hostname or "")
    return (
        len(actual) == 1
        and normalized(actual[0]) == compose_value
        or all(normalized(value) == compose_value for value in actual)
    )


def run_helm_render(root: Path, errors: list[str]) -> None:
    helm = shutil.which("helm")
    if not helm:
        errors.append("当前校验要求 Helm 渲染，但未安装 helm")
        return
    for chart_name in ("deploy/helm/hci-platform-dsh", "deploy/helm/hci-platform"):
        result = subprocess.run(
            [helm, "template", "dsh-contract", str(root / chart_name)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            errors.append(f"{chart_name} Helm 渲染失败: {result.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="测试或外部仓库根目录；默认使用当前平台仓库")
    parser.add_argument("--contract", default="deploy/config/dsh-config-contract.yaml")
    parser.add_argument("--compose-env", help="环境仓或本地 .env；提供后执行实际值比对")
    parser.add_argument("--dsh-values", help="环境仓 dsh-values.yaml；提供后执行 DSH 值比对")
    parser.add_argument("--platform-values", help="环境仓主平台 values.yaml；提供后执行路由值比对")
    parser.add_argument("--require-render", action="store_true", help="要求 Helm 渲染两个相关 Chart")
    parser.add_argument("--skip-render", action="store_true", help="跳过 Helm 渲染")
    args = parser.parse_args()
    if args.require_render and args.skip_render:
        parser.error("--skip-render 与 --require-render 不能同时使用")

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    contract_path = Path(args.contract)
    if not contract_path.is_absolute():
        contract_path = root / contract_path
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    sources = contract.get("sources") or {}
    compose_text = (root / sources["compose"]).read_text(encoding="utf-8")
    env_names = set(parse_env_file(root / sources["env_example"]))
    compose_refs = set(COMPOSE_REFERENCE.findall(compose_text))
    mappings = contract.get("mappings") or []
    errors: list[str] = []
    mapped_names: set[str] = set()

    for mapping in mappings:
        compose_name = mapping.get("compose_env")
        if not isinstance(compose_name, str):
            errors.append("DSH 映射缺少 compose_env")
            continue
        mapped_names.add(compose_name)
        if compose_name not in compose_refs:
            errors.append(f"{compose_name} 未被 Compose 引用")
        if compose_name not in env_names:
            errors.append(f"{compose_name} 未在 .env.example 声明")

        chart_path = mapping.get("helm_chart")
        if not isinstance(chart_path, str):
            errors.append(f"{compose_name} 缺少 helm_chart")
            continue
        try:
            chart_values, template_text = chart_files(root, chart_path)
        except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
            errors.append(f"{compose_name} 的 Helm Chart 无法读取: {exc}")
            continue
        for value_path in mapping.get("helm_values") or []:
            exists, _ = value_at(chart_values, value_path)
            if not exists and not (
                mapping.get("dynamic_values") and value_at(chart_values, value_path.rsplit(".", 1)[0])[0]
            ):
                errors.append(f"{compose_name} 的 Helm values 路径不存在: {chart_path}:{value_path}")
        name_path = mapping.get("helm_name_path")
        if name_path and not value_at(chart_values, name_path)[0]:
            errors.append(f"{compose_name} 的 Helm 动态环境变量路径不存在: {chart_path}:{name_path}")
        selector = mapping.get("template_selector")
        if selector and selector not in template_text:
            errors.append(f"{compose_name} 的 Helm 模板未包含动态注入选择器: {selector}")
        for rendered_env in mapping.get("rendered_env") or []:
            if rendered_env not in template_text and not selector:
                errors.append(f"{compose_name} 的 Helm 模板未注入运行时变量: {rendered_env}")

    offline_refs = {name for name in compose_refs if name.startswith("DSH_OFFLINE_")}
    missing = offline_refs - mapped_names
    if missing:
        errors.append("DSH_OFFLINE_* Compose 变量未纳入映射契约: " + ", ".join(sorted(missing)))
    extra = mapped_names - compose_refs
    if extra:
        errors.append("DSH 映射声明了未被 Compose 使用的变量: " + ", ".join(sorted(extra)))

    if args.compose_env:
        compose_values = parse_env_file(Path(args.compose_env))
        values_files = {
            "deploy/helm/hci-platform-dsh": args.dsh_values,
            "deploy/helm/hci-platform": args.platform_values,
        }
        chart_cache: dict[str, dict[str, Any]] = {}
        for mapping in mappings:
            name = mapping["compose_env"]
            if name not in compose_values:
                errors.append(f"实际环境文件缺少 DSH 配置: {name}")
                continue
            chart_path = mapping["helm_chart"]
            if chart_path not in chart_cache:
                base, _ = chart_files(root, chart_path)
                overlay_path = values_files.get(chart_path)
                overlay = yaml.safe_load(Path(overlay_path).read_text(encoding="utf-8")) if overlay_path else {}
                chart_cache[chart_path] = merge_values(base, overlay or {})
            if not compare_values(mapping, compose_values[name], chart_cache[chart_path]):
                errors.append(f"DSH 实际值不一致: {name} ↔ {mapping['helm_chart']}")
            name_path = mapping.get("helm_name_path")
            if name_path:
                exists, actual_name = value_at(chart_cache[chart_path], name_path)
                expected_name = (mapping.get("rendered_env") or [""])[0]
                if not exists or actual_name != expected_name:
                    errors.append(f"DSH 动态凭据环境变量名不一致: {name} ↔ {name_path}")

    if args.require_render and not args.skip_render:
        run_helm_render(root, errors)
    if errors:
        print("DSH 配置契约校验失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    mode = "实际值比对" if args.compose_env else "静态映射"
    print(f"DSH 配置契约校验通过（{mode}，映射={len(mappings)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
