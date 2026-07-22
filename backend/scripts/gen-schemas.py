#!/usr/bin/env python3
"""从 `ACQUIRER_ARGS_SCHEMA` 单一来源导出 JSON Schema 契约文件（RFC §6.1）。

输出（默认 backend/shared/schemas/signals/）：
  - signal.v2.schema.json                  整体 {schema_version, signals:[...]}
  - acquirer_args/common_args.schema.json  公共参数（timeout），供各 tool `$ref`
  - acquirer_args/{tool}.schema.json       每个 acquirer 的 args 契约（11 个）

不变量：
  - 单一来源：所有字段定义来自 `backend/shared/schemas/acquirer_args.py` 的
    `ACQUIRER_ARGS_SCHEMA` / `COMMON_ARGS`，本脚本不做任何业务判断。
  - 公共字段 `timeout` 用 `$ref` 指向 `common_args.schema.json#/properties/timeout`，
    杜绝在各 tool 中重复声明（§6.1）。
  - 幂等：固定构造顺序 + `ensure_ascii=False` + `indent=2`，重复运行输出字节一致。
  - `$id` 使用绝对 URI（`https://hci-troubleshoot/schemas/signals/...`），
    使 registry / check-jsonschema 无需依赖相对解析。

使用：
  python backend/scripts/gen-schemas.py                 # 写出到默认目录
  python backend/scripts/gen-schemas.py --out DIR       # 指定输出目录
  make gen-schemas                                      # 等价
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent                 # backend/scripts
SCHEMA_MODULE = HERE.parent / "shared" / "schemas" / "acquirer_args.py"
DEFAULT_OUT = HERE.parent / "shared" / "schemas" / "signals"

DRAFT = "http://json-schema.org/draft-07/schema#"
BASE = "https://hci-troubleshoot/schemas/signals"


def _load_registry_module():
    """动态加载 acquirer_args.py（避免 backend 非 pip 包时的导入问题）。"""
    spec = importlib.util.spec_from_file_location("acquirer_args", SCHEMA_MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_common_args(mod: object) -> dict:
    timeout = deepcopy(mod.COMMON_ARGS["timeout"])  # type: ignore[attr-defined]
    return {
        "$schema": DRAFT,
        "$id": f"{BASE}/acquirer_args/common_args.schema.json",
        "title": "Common acquire args (shared across all acquirers)",
        "type": "object",
        "additionalProperties": False,
        "properties": {"timeout": timeout},
    }


def build_tool_schema(mod: object, tool: str, schema: dict) -> dict:
    s = deepcopy(schema)
    # timeout 改为引用 common_args，消除重复定义（§6.1）
    if "timeout" in s.get("properties", {}):
        s["properties"]["timeout"] = {
            "$ref": f"{BASE}/acquirer_args/common_args.schema.json#/properties/timeout"
        }
    s["$schema"] = DRAFT
    s["$id"] = f"{BASE}/acquirer_args/{tool}.schema.json"
    s["title"] = f"acquire.args for {tool}"
    return s


def build_signal_v2(mod: object, tools: list[str]) -> dict:
    # acquire.args 按 tool 值选择对应 tool schema（draft-07 if/then 跨层级可达）
    acquire_allof = []
    for tool in tools:
        acquire_allof.append(
            {
                "if": {"properties": {"tool": {"const": tool}}, "required": ["tool"]},
                "then": {
                    "properties": {
                        "args": {"$ref": f"{BASE}/acquirer_args/{tool}.schema.json"}
                    }
                },
            }
        )

    return {
        "$schema": DRAFT,
        "$id": f"{BASE}/signal.v2.schema.json",
        "title": "KBD signals_json (v2 nested signal model)",
        "type": "object",
        "required": ["schema_version", "signals"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 2},
            "signals": {"type": "array", "items": {"$ref": "#/definitions/signal"}},
        },
        "definitions": {
            "signal": {
                "type": "object",
                "required": ["acquire"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "acquire": {"$ref": "#/definitions/acquire"},
                    "match": {"$ref": "#/definitions/match"},
                    "orchestrate": {"$ref": "#/definitions/orchestrate"},
                    "provenance": {"$ref": "#/definitions/provenance"},
                    "review": {"$ref": "#/definitions/review"},
                    # 迁移无损兼容：v1 未知字段收进 _v1_legacy（正常写路径不会出现）
                    "_v1_legacy": {"type": "object"},
                },
            },
            "acquire": {
                "type": "object",
                "required": ["tool", "args"],
                "additionalProperties": False,
                "properties": {
                    "tool": {"type": "string", "enum": tools},
                    "args": {"type": "object"},
                },
                "allOf": acquire_allof,
            },
            "match": {
                "type": ["object", "null"],
                "required": ["type", "pattern", "mode", "expected"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"},
                    "pattern": {"type": "string"},
                    "mode": {"type": "string"},
                    "expected": {"type": "boolean"},
                },
            },
            "orchestrate": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "phase": {"type": "string"},
                    "action": {"type": "string"},
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "container": {"type": "string"},
                    "produces": {
                        "type": "array",
                        "description": "该信号向变量池产出的变量（v1 produces: [{name, type?, path?}]）",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                                "path": {"type": "string"},
                            },
                        },
                    },
                    "requires": {"type": "array", "items": {"type": "string"}},
                },
            },
            "provenance": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {"type": "string", "enum": ["frontend", "backend"]},
                    "method": {"type": "string"},
                    "source_section": {"type": "string"},
                    "confidence": {"type": ["number", "integer"]},
                    "risk": {"type": ["integer", "number"]},
                    "needs_review": {"type": "boolean"},
                },
            },
            "review": {
                "type": "object",
                "required": ["require_human_confirm"],
                "additionalProperties": False,
                "properties": {
                    "require_human_confirm": {"type": "boolean"},
                },
            },
        },
    }


def build_all() -> dict[str, dict]:
    """返回 {相对路径: schema dict} 的内存表示（供写出或漂移比对复用）。"""
    mod = _load_registry_module()
    tools = mod.SUPPORTED_TOOLS  # type: ignore[attr-defined]
    docs: dict[str, dict] = {}
    docs["signal.v2.schema.json"] = build_signal_v2(mod, tools)
    docs["acquirer_args/common_args.schema.json"] = build_common_args(mod)
    for tool, schema in mod.ACQUIRER_ARGS_SCHEMA.items():  # type: ignore[attr-defined]
        docs[f"acquirer_args/{tool}.schema.json"] = build_tool_schema(mod, tool, schema)
    return docs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="导出信号 v2 JSON Schema 契约文件")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    args = p.parse_args(argv)

    docs = build_all()
    for rel, doc in docs.items():
        path = args.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"generated {len(docs)} schema files into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
