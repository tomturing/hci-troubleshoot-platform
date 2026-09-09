#!/usr/bin/env python3
"""校验 Compose、.env.example 与 Helm 的配置交付契约。"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
COMPOSE_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
HELM_VALUE_REFERENCE = re.compile(r"\.Values\.([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)")
HELM_ENV_NAME = re.compile(r"^\s*-\s+name:\s+([A-Z][A-Z0-9_]*)\s*$", re.MULTILINE)
HELM_CONFIG_KEY = re.compile(r"^\s{2}([A-Z][A-Z0-9_]*):", re.MULTILINE)


def parse_env(path: Path) -> tuple[set[str], list[str]]:
    names: set[str] = set()
    errors: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = ENV_LINE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name in names:
            errors.append(f"{path}:{number}: 重复声明 {name}")
        names.add(name)
    return names, errors


def value_exists(values: dict[str, Any], path: str) -> bool:
    current: Any = values
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def rendered_runtime_names(rendered: str) -> set[str]:
    """从 Helm 实际渲染的对象读取 ConfigMap/Secret 和容器环境变量名。

    不能用文本正则替代 YAML 解析：ConfigMap 的缩进会随对象而变，且契约项可能
    经由 ``template_env`` 改名（例如 ``API_GATEWAY_PORT`` -> ``SERVICE_PORT``）。
    """

    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            environment = value.get("env")
            if isinstance(environment, list):
                for item in environment:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        names.add(item["name"])
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for document in yaml.safe_load_all(rendered):
        if not isinstance(document, dict):
            continue
        kind = document.get("kind")
        if kind in {"ConfigMap", "Secret"}:
            for section in ("data", "stringData"):
                data = document.get(section)
                if isinstance(data, dict):
                    names.update(str(key) for key in data)
        walk(document)
    return names


def run_helm_render(root: Path, contract: dict[str, Any], errors: list[str], *, required: bool) -> None:
    """有 Helm 时额外做渲染检查；无 Helm 的开发机仍完成静态契约校验。"""
    helm = shutil.which("helm")
    if not helm:
        if required:
            errors.append("当前校验要求 Helm 渲染，但未安装 helm")
            return
        print("INFO  未安装 helm，跳过渲染检查（已完成 values/template 静态检查）")
        return
    chart = root / "deploy/helm/hci-platform"
    with tempfile.TemporaryDirectory() as temporary_directory:
        rendered = Path(temporary_directory) / "rendered.yaml"
        command = [
            helm,
            "template",
            "config-contract",
            str(chart),
            "--set",
            "diagnosisService.enabled=true",
            "--set",
            "diagnosisService.existingCredentialsSecret=config-contract-credentials",
            "--set",
            "diagnosisService.allowedOrigins=http://localhost:3001",
            "--set",
            "diagnosisService.directUploadBaseUrl=/",
            "--set",
            "langfuse.enabled=true",
            "--set",
            "langfuse.secretKey=config-contract-secret",
            "--set",
            "langfuse.publicKey=config-contract-public",
        ]
        result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
        if result.returncode:
            errors.append("Helm 渲染失败: " + result.stderr.strip())
            return
        rendered.write_text(result.stdout, encoding="utf-8")
        runtime_names = rendered_runtime_names(result.stdout)
        expected: set[str] = set()
        for name, specification in (contract.get("helm_equivalents") or {}).items():
            # 例如 Grafana、暖池配置按 values 条件启用；静态模板仍会校验其存在，
            # 但默认渲染不应该误报为缺失。
            if specification.get("render_optional", False):
                continue
            expected.update(specification.get("template_env") or [name])
        missing = expected - runtime_names
        if missing:
            errors.append("Helm 渲染结果缺少契约运行时变量: " + ", ".join(sorted(missing)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default="deploy/config/config-contract.yaml")
    parser.add_argument("--skip-render", action="store_true", help="跳过可选 Helm render 校验")
    parser.add_argument("--require-render", action="store_true", help="要求安装 Helm 并完成渲染校验（CI 使用）")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    contract_path = root / args.contract
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    sources = contract.get("sources") or {}
    env_path = root / sources["env_example"]
    compose_path = root / sources["compose"]
    values_path = root / sources["helm_values"]
    templates_path = root / sources["helm_templates"]
    errors: list[str] = []

    env_names, env_errors = parse_env(env_path)
    errors.extend(env_errors)
    compose_names = set(COMPOSE_REFERENCE.findall(compose_path.read_text(encoding="utf-8")))
    non_compose = set(contract.get("non_compose") or [])
    missing_from_env = compose_names - env_names
    if missing_from_env:
        errors.append("Compose 引用但 .env.example 未声明: " + ", ".join(sorted(missing_from_env)))
    invalid_non_compose = non_compose & compose_names
    if invalid_non_compose:
        errors.append("non_compose 却被 Compose 使用: " + ", ".join(sorted(invalid_non_compose)))
    unclassified = env_names - compose_names - non_compose
    if unclassified:
        errors.append(".env.example 变量既非 Compose 使用也未登记为 non_compose: " + ", ".join(sorted(unclassified)))
    unknown_non_compose = non_compose - env_names
    if unknown_non_compose:
        errors.append("契约 non_compose 未在 .env.example 声明: " + ", ".join(sorted(unknown_non_compose)))

    values = yaml.safe_load(values_path.read_text(encoding="utf-8")) or {}
    template_text = "\n".join(path.read_text(encoding="utf-8") for path in templates_path.rglob("*.yaml"))
    for reference in sorted(set(HELM_VALUE_REFERENCE.findall(template_text))):
        if not value_exists(values, reference):
            errors.append(f"Helm 模板引用不存在的 values 路径: {reference}")
    helm_env_names = set(HELM_ENV_NAME.findall(template_text)) | set(HELM_CONFIG_KEY.findall(template_text))
    equivalents = contract.get("helm_equivalents") or {}
    helm_only = set(contract.get("helm_only_runtime") or [])
    overlap = set(equivalents) & helm_only
    if overlap:
        errors.append("变量不能同时登记为 Helm 等价配置和 Helm 内部变量: " + ", ".join(sorted(overlap)))
    missing_equivalents = env_names - set(equivalents)
    if missing_equivalents:
        errors.append(".env.example 的每项配置必须登记 Helm values 等价映射: " + ", ".join(sorted(missing_equivalents)))
    missing_compose_equivalents = compose_names - set(equivalents)
    if missing_compose_equivalents:
        errors.append("Compose 变量必须登记 Helm values 等价映射: " + ", ".join(sorted(missing_compose_equivalents)))
    allowed_helm_names = set(equivalents) | helm_only
    unknown_helm_names = helm_env_names - allowed_helm_names
    if unknown_helm_names:
        errors.append("Helm 模板运行时变量未登记到配置契约: " + ", ".join(sorted(unknown_helm_names)))
    for name, specification in equivalents.items():
        for value_path in specification.get("value_paths") or []:
            if not value_exists(values, value_path):
                errors.append(f"{name} 的 Helm values 路径不存在: {value_path}")
        template_names = set(specification.get("template_env") or [name])
        missing_template_names = template_names - helm_env_names
        if missing_template_names:
            errors.append(
                f"{name} 已登记 Helm 等价配置，但模板未注入运行时变量: "
                + ", ".join(sorted(missing_template_names))
            )

    if args.skip_render and args.require_render:
        parser.error("--skip-render 与 --require-render 不能同时使用")
    if not args.skip_render:
        run_helm_render(root, contract, errors, required=args.require_render)
    if errors:
        print("配置契约校验失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    aliases = sum(
        1
        for name, specification in equivalents.items()
        if set(specification.get("template_env") or [name]) != {name}
    )
    print(
        "配置契约校验通过："
        f"业务配置={len(env_names)}（Compose 交付={len(compose_names)}，非 Compose={len(non_compose)}，"
        f"Helm 等价映射={len(equivalents)}，别名/派生映射={aliases}）；"
        f"Helm 内部运行时变量={len(helm_only)}；"
        f"Helm values 路径引用={len(set(HELM_VALUE_REFERENCE.findall(template_text)))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
