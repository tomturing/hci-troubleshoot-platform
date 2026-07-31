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

# acquirer_args 会导入同属 backend/shared 的日志源 Catalog；动态加载前确保 backend
# 位于模块搜索路径，避免生成器与服务运行时出现不同的导入行为。
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

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
            "rejected_candidates": {
                "type": "array",
                "items": {"$ref": "#/definitions/rejectedCandidate"},
            },
            "verification_contract": {"$ref": "#/definitions/verificationContract"},
            "generation_metadata": {"$ref": "#/definitions/generationMetadata"},
            "publish_validation": {"$ref": "#/definitions/publishValidation"},
        },
        "definitions": {
            "rejectedCandidate": {
                "type": "object",
                "required": ["candidate", "reason"],
                "additionalProperties": False,
                "properties": {
                    # 保留模型原始候选供审核；候选之所以在这里，正是因为它不一定
                    # 符合 signal definition，因此不能用 signal schema 约束。
                    "candidate": {},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
            "signal": {
                "type": "object",
                "required": ["acquire"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "role": {"type": "string", "enum": ["must", "should", "exclude", "context"]},
                    "acquire": {"$ref": "#/definitions/acquire"},
                    "match": {"$ref": "#/definitions/match"},
                    "orchestrate": {"$ref": "#/definitions/orchestrate"},
                    "provenance": {"$ref": "#/definitions/provenance"},
                    "review": {"$ref": "#/definitions/review"},
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
            # 匹配模式只消费同一份声明式 ValueExtract 的结果。JSON 路径属于取值层，
            # 不是单独的 Matcher；保存时拒绝无 extract 的旧全文判定。
            "match": {
                "type": ["object", "null"],
                "required": ["type", "expected"],
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "keyword", "regex", "state", "threshold", "delta", "trend", "exists"
                        ],
                    },
                    "pattern": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                        ]
                    },
                    "mode": {"type": "string", "enum": ["or", "and", "not"]},
                    "expected": {"type": "boolean"},
                    "value": {"type": ["number", "integer"]},
                    "operator": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "=", "!="]},
                    "aggregation": {
                        "type": "string",
                        "enum": [
                            "first_number", "last_number", "line_count", "duration_seconds", "max", "min", "sum"
                        ],
                        "default": "first_number",
                    },
                    # 与 produces[].extract 引用同一份 valueExtract；取值层与
                    # Predicate 正交，不执行自由 grep/awk/jq。
                    "extract": {"$ref": "#/definitions/valueExtract"},
                    "minimum_samples": {"type": "integer", "minimum": 2, "maximum": 10000},
                    "direction": {"type": "string", "enum": ["increasing", "decreasing", "stable"]},
                },
                "allOf": [
                    {"required": ["extract"]},
                    {
                        "if": {"properties": {"type": {"enum": ["keyword", "regex", "state"]}}},
                        "then": {"required": ["pattern"]},
                    },
                    {
                        "if": {"properties": {"type": {"const": "threshold"}}},
                        "then": {"required": ["value", "operator"]},
                    },
                    {"if": {"properties": {"type": {"const": "delta"}}}, "then": {"required": ["value", "operator"]}},
                    {"if": {"properties": {"type": {"const": "trend"}}}, "then": {"required": ["direction"]}},
                ],
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
                        "description": "该信号向变量池产出的变量；统一使用声明式 extract",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "string", "integer", "number", "boolean", "array",
                                        "object", "array<object>",
                                    ],
                                },
                                "path": {"type": "string"},
                                "extract": {"$ref": "#/definitions/valueExtract"},
                            },
                            "not": {"required": ["path", "extract"]},
                        },
                    },
                    "requires": {"type": "array", "items": {"type": "string"}},
                },
            },
            "rowRange": {
                "type": "object",
                "required": ["start", "end"],
                "additionalProperties": False,
                "properties": {
                    "start": {"type": "integer", "minimum": 1},
                    "end": {"type": "integer", "minimum": 1},
                },
            },
            "rowSelector": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["mode"],
                        "additionalProperties": False,
                        "properties": {"mode": {"const": "all"}},
                    },
                    {
                        "type": "object",
                        "required": ["mode"],
                        "additionalProperties": False,
                        "properties": {
                            "mode": {"const": "keywords"},
                            "include": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "exclude": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "include_mode": {"type": "string", "enum": ["all", "any"], "default": "all"},
                            "case_sensitive": {"type": "boolean", "default": True},
                        },
                    },
                    {
                        "type": "object",
                        "required": ["mode", "basis"],
                        "additionalProperties": False,
                        "properties": {
                            "mode": {"const": "indices"},
                            "basis": {"type": "string", "enum": ["physical", "non_empty", "data"]},
                            "indices": {
                                "type": "array",
                                "items": {"type": "integer", "minimum": 1},
                                "uniqueItems": True,
                            },
                            "ranges": {
                                "type": "array",
                                "items": {"$ref": "#/definitions/rowRange"},
                            },
                        },
                        "anyOf": [
                            {"required": ["indices"], "properties": {"indices": {"minItems": 1}}},
                            {"required": ["ranges"], "properties": {"ranges": {"minItems": 1}}},
                        ],
                    },
                ],
            },
            "tableHeader": {
                "type": "object",
                "required": ["mode", "required"],
                "additionalProperties": False,
                "properties": {
                    "mode": {"const": "contains"},
                    "required": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "case_sensitive": {"type": "boolean", "default": False},
                },
            },
            "textColumnSelector": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["by", "index"],
                        "additionalProperties": False,
                        "properties": {
                            "by": {"const": "index"},
                            "index": {"type": "integer", "minimum": 1},
                        },
                    },
                    {
                        "type": "object",
                        "required": ["by", "name"],
                        "additionalProperties": False,
                        "properties": {
                            "by": {"const": "header"},
                            "name": {"type": "string", "minLength": 1},
                            "aliases": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "uniqueItems": True,
                            },
                        },
                    },
                ],
            },
            "textColumn": {
                "type": "object",
                "required": ["key", "selector"],
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
                    "selector": {"$ref": "#/definitions/textColumnSelector"},
                    "value_mode": {
                        "type": "string",
                        "enum": ["string", "integer", "number", "boolean"],
                        "default": "string",
                    },
                },
            },
            "textExtract": {
                "type": "object",
                "description": "QFK 非 JSON 输出的受控行筛选与列提取规则；不接受 shell/grep/awk 脚本",
                "additionalProperties": False,
                "required": ["type", "rows"],
                "properties": {
                    "type": {"const": "text"},
                    "delimiter": {
                        "anyOf": [
                            {"const": "whitespace"},
                            {"type": "string", "minLength": 1, "maxLength": 1},
                        ],
                        "default": "whitespace",
                    },
                    "cardinality": {
                        "type": "string",
                        "enum": ["exactly_one", "first", "last", "all"],
                        "default": "exactly_one",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["stdout", "stderr"],
                        "default": "stdout",
                    },
                    "value_mode": {
                        "type": "string",
                        "enum": ["string", "integer", "number", "boolean", "array"],
                        "description": "提取后的确定性类型；number 支持 54%→54，不剥离容量/时长单位",
                    },
                    "parser": {"type": "string", "enum": ["whitespace_table", "delimited_table"]},
                    "header": {"$ref": "#/definitions/tableHeader"},
                    "rows": {"$ref": "#/definitions/rowSelector"},
                    "columns": {
                        "type": "array",
                        "items": {"$ref": "#/definitions/textColumn"},
                        "minItems": 1,
                    },
                    "value_key": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
                },
                "allOf": [
                    {
                        "if": {"required": ["columns"]},
                        "then": {"required": ["parser", "rows"]},
                    },
                    {
                        "if": {
                            "properties": {"parser": {"const": "delimited_table"}},
                            "required": ["parser"],
                        },
                        "then": {"required": ["delimiter"]},
                    },
                ],
            },
            "jsonExtract": {
                "type": "object",
                "description": "QFK JSON 输出的受控点号/数组下标取值；不接受 jq 表达式",
                "required": ["type", "path"],
                "additionalProperties": False,
                "properties": {
                    "type": {"const": "json"},
                    "source": {"type": "string", "enum": ["stdout", "stderr"], "default": "stdout"},
                    "path": {"type": "string"},
                    "cardinality": {
                        "type": "string",
                        "enum": ["exactly_one", "first", "last", "all"],
                        "default": "exactly_one",
                    },
                    "value_mode": {
                        "type": "string",
                        "enum": ["string", "integer", "number", "boolean", "array", "object", "array<object>"],
                    },
                },
            },
            "valueExtract": {
                "oneOf": [
                    {"$ref": "#/definitions/textExtract"},
                    {"$ref": "#/definitions/jsonExtract"},
                ]
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
                    "evidence": {"type": "string"},
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
            "review": {
                "type": "object",
                "required": ["require_human_confirm"],
                "additionalProperties": False,
                "properties": {
                    "require_human_confirm": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
            },
            "verificationContract": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "const": 1},
                    "case_id": {"type": "string"},
                    "scope": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "products": {"type": "array", "items": {"type": "string"}},
                            "versions": {"type": "array", "items": {"type": "string"}},
                            "components": {"type": "array", "items": {"type": "string"}},
                            "topology_constraints": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "variables": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "required": ["type"],
                            "additionalProperties": False,
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["string", "integer", "number", "boolean", "array"],
                                },
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "evidence_policy": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["must"],
                        "properties": {
                            "must": {"type": "array", "items": {"type": "string"}},
                            "should": {"type": "array", "items": {"type": "string"}},
                            "exclude": {"type": "array", "items": {"type": "string"}},
                            "context": {"type": "array", "items": {"type": "string"}},
                            "minimum_should": {"type": "integer", "minimum": 0},
                            "on_missing_must": {"type": "string", "const": "inconclusive"},
                        },
                    },
                },
            },
            "publishValidation": {
                "type": "object",
                "required": ["schema_version", "status", "tool_contract_revision", "validator"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "const": 1},
                    "status": {"type": "string", "const": "passed"},
                    "tool_contract_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "validator": {"type": "string", "const": "expert_publish_gate"},
                },
            },
            "generationMetadata": {
                "type": "object",
                "required": [
                    "schema_version",
                    "status",
                    "source_fingerprint",
                    "prompt_revision",
                    "model_id",
                    "tool_contract_revision",
                    "generation_fingerprint",
                ],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "const": 1},
                    "status": {"type": "string", "enum": ["current", "stale", "manual_reviewed"]},
                    "source_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "prompt_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "model_id": {"type": "string", "minLength": 1},
                    "tool_contract_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "generation_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
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
