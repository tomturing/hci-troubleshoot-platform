"""加载 §6.1 导出的 JSON Schema 契约，并用 jsonschema 校验 signals_json（保存时强制）。

来源：RFC《关键信号数据模型分层重构》§6.1。
契约文件由 `backend/scripts/gen-schemas.py` 导出到本目录 `signals/`。
依赖：jsonschema(>=4.21) + referencing（运行时依赖，见根 pyproject.toml）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

_SIGNALS_DIR = Path(__file__).resolve().parent / "signals"


def _build_registry() -> Registry:
    """把所有 *.schema.json 装入 referencing Registry（以各自 $id 为键）。"""
    resources: list[tuple[str, Resource]] = []
    for p in sorted(_SIGNALS_DIR.rglob("*.schema.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        uri = data.get("$id")
        if not uri:
            # 兜底：以相对路径作为 $id（正常生成文件都带绝对 $id）
            uri = p.relative_to(_SIGNALS_DIR).as_posix()
        resources.append((uri, Resource.from_contents(data, default_specification=DRAFT7)))
    return Registry().with_resources(resources)


_REGISTRY = _build_registry()
_SIGNAL_V2_SCHEMA = json.loads((_SIGNALS_DIR / "signal.v2.schema.json").read_text(encoding="utf-8"))


def validate_signals_json(raw: Any) -> None:
    """校验整段 `signals_json`（v2 对象）符合 `signal.v2.schema.json`。

    同时经 if/then 逐条校验 `acquire.args`（按 `tool` 选 `acquirer_args/{tool}.schema.json`）。
    `additionalProperties:false` 会拒绝幽灵字段与顶层 `keyword` 等回归。
    失败时抛 `jsonschema.ValidationError`（调用方应转为 422）。
    """
    Draft7Validator(_SIGNAL_V2_SCHEMA, registry=_REGISTRY).validate(raw)
