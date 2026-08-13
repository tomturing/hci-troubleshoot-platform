"""Deterministic provenance fingerprints for generated KBD signal contracts.

契约门禁根治（见 docs/solution/events/2026-08-13-kbd-contract-gate-structural-semantic-fingerprint-adr.md）：
将单一的「字节哈希全等」拆为两个正交维度：

- 结构指纹（structural）：决定「旧 KBD 的 signals 在当前 schema 下还能否解析执行」。
  提取 required / type / enum / const / additionalProperties 以及递归 $ref 后的 if-then required。
- 语义指纹（semantic）：结构指纹 + description / default 等影响执行语义但结构仍合法的字段。

门禁据此区分「破坏性变更」（结构变了且旧信号校验失败 → 真阻断）与
「兼容演进」（仅新增可选属性 / 仅语义漂移 → 放行 + 可观测），避免每次 schema 微调
让 100% 存量 KBD 瞬间过期（虚拟机-015 / 23821 系统性 stale 根因）。
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_SIGNALS_DIR = Path(__file__).resolve().parent / "signals"

# 指纹算法版本：算法实现升级时自增，避免「算法变了导致所有存量快照误判 breaking」。
# 门禁在快照里记录 fp_algo_version，与当前不一致时走一次性全量重算。
FP_ALGO_VERSION = 1


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@lru_cache(maxsize=1)
def current_tool_contract_revision() -> str:
    """Hash every generated Signal/QKV/QFK schema, not only the root `$ref` file.

    保留作为「是否发生任何变动」的粗粒度探测，向后兼容旧 KBD 快照的
    ``publish_validation.tool_contract_revision`` 比对。
    """
    material = [
        {
            "path": path.relative_to(_SIGNALS_DIR).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(_SIGNALS_DIR.rglob("*.schema.json"))
    ]
    return _fingerprint(material)


# ──────────────────────────────────────────────
#  结构 / 语义指纹（递归解析 $ref）
# ──────────────────────────────────────────────

_REF_RE = re.compile(r"(?P<file>[^#]*)#/(?P<pointer>.*)")


def _load_signals_schemas() -> dict[str, dict]:
    """加载 signals 目录下所有 schema 文件，key 为相对路径（posix）。"""
    schemas: dict[str, dict] = {}
    for path in sorted(_SIGNALS_DIR.rglob("*.schema.json")):
        rel = path.relative_to(_SIGNALS_DIR).as_posix()
        schemas[rel] = json.loads(path.read_text(encoding="utf-8"))
    return schemas


def _resolve_ref(ref: str, base_path: str, schemas: dict[str, dict]) -> dict:
    """解析 JSON Schema `$ref` 为实际子 schema（支持同文件与跨文件）。"""
    m = _REF_RE.match(ref)
    if not m:
        return {}
    ref_file, pointer = m.group("file"), m.group("pointer")
    target = schemas[base_path] if not ref_file else schemas.get(ref_file)
    if target is None:
        return {}
    node: Any = target
    for token in pointer.split("/"):
        if token == "":
            continue
        node = node.get(token, {})
    return node if isinstance(node, dict) else {}


def _norm_type(t: Any) -> Any:
    """类型归一化：oneOf/anyOf/数组统一为排序后的集合字符串，便于结构比对。"""
    if isinstance(t, list):
        return "|".join(sorted(t))
    if isinstance(t, str):
        return t
    # oneOf / anyOf / allOf：递归提取其中的 type
    if isinstance(t, dict):
        for key in ("oneOf", "anyOf", "allOf"):
            if key in t and isinstance(t[key], list):
                sub = [_norm_type(item.get("type")) for item in t[key] if isinstance(item, dict)]
                return "|".join(sorted(x for x in sub if x))
        if "type" in t:
            return _norm_type(t["type"])
    return str(t)


def _structural_repr(node: Any, base_path: str, schemas: dict[str, dict]) -> Any:
    """递归提取「能否解析」相关的结构约束；遇到 $ref 展开后继续递归。"""
    if not isinstance(node, dict):
        return node

    # $ref 优先展开
    if "$ref" in node:
        resolved = _resolve_ref(node["$ref"], base_path, schemas)
        merged = {k: v for k, v in node.items() if k != "$ref"}
        merged.update(resolved)
        return _structural_repr(merged, base_path, schemas)

    result: dict[str, Any] = {}
    # 类型
    if "type" in node:
        result["type"] = _norm_type(node["type"])
    # 必填集合（强约束）
    if "required" in node:
        result["required"] = sorted(node["required"])
    # 枚举 / 常量（强约束，决定取值合法性）
    if "enum" in node:
        result["enum"] = sorted(node["enum"])
    if "const" in node:
        result["const"] = node["const"]
    # 未知字段开关（additionalProperties:False 意味着缺字段/多字段都非法）
    if "additionalProperties" in node:
        result["additionalProperties"] = node["additionalProperties"]
    # 递归属性：仅 required 属性的约束进入结构指纹。
    # 理由（对抗性审查）：旧 KBD 的 signals 是「已发布的旧实例」。新增一个可选属性
    # （如 #745 的 match.metric）不改变旧实例的合法性——旧实例不含该属性且属性非
    # required，用新 schema 校验仍通过。因此可选属性的「存在性/约束」变化不进结构
    # 指纹，避免误杀兼容演进。真 breaking（改 required 属性的 type/enum、把可选属性
    # 移入 required、additionalProperties 变 False）仍会被 required 列表或
    # additionalProperties 开关捕获；边界情形由门禁实跑校验兜底。
    if "properties" in node and isinstance(node["properties"], dict):
        required = set(node.get("required", []))
        result["properties"] = {
            k: _structural_repr(v, base_path, schemas)
            for k, v in node["properties"].items()
            if k in required
        }
    # 命名定义库（definitions / $defs）：被 $ref 引用，其任何变化都影响引用方，
    # 必须纳入指纹（否则 #745 在 definitions.match 新增 metric 会被遗漏）。
    for defs_key in ("definitions", "$defs"):
        if defs_key in node and isinstance(node[defs_key], dict):
            result[defs_key] = {
                k: _structural_repr(v, base_path, schemas)
                for k, v in node[defs_key].items()
            }
    # 子条件结构：if/then 的 required 变更同样是 breaking
    if "allOf" in node and isinstance(node["allOf"], list):
        conds = []
        for sub in node["allOf"]:
            if not isinstance(sub, dict):
                continue
            cond: dict[str, Any] = {}
            if "if" in sub:
                cond["if"] = _structural_repr(sub["if"], base_path, schemas)
            if "then" in sub:
                then_repr = _structural_repr(sub["then"], base_path, schemas)
                if isinstance(then_repr, dict) and "required" in then_repr:
                    cond["then_required"] = then_repr["required"]
            if cond:
                conds.append(cond)
        if conds:
            result["allOf"] = conds
    # 其他组合关键字
    for key in ("anyOf", "oneOf"):
        if key in node and isinstance(node[key], list):
            result[key] = [_structural_repr(item, base_path, schemas) for item in node[key]]
    return result


def _semantic_repr(node: Any, base_path: str, schemas: dict[str, dict]) -> Any:
    """结构指纹 + 影响执行语义但结构仍合法的字段（description / default）。"""
    struct = _structural_repr(node, base_path, schemas)
    if not isinstance(struct, dict):
        return struct
    result: dict[str, Any] = dict(struct)
    if "description" in node and isinstance(node["description"], str):
        result["description"] = node["description"]
    if "default" in node:
        result["default"] = node["default"]
    # 语义指纹递归全量 properties（含可选属性），不继承结构的 required 裁剪，
    # 否则可选属性的语义漂移（如新增 metric 的 description 变化）无法被观测。
    if "properties" in node and isinstance(node["properties"], dict):
        result["properties"] = {
            k: _semantic_repr(v, base_path, schemas)
            for k, v in node["properties"].items()
        }
    for defs_key in ("definitions", "$defs"):
        if defs_key in node and isinstance(node[defs_key], dict):
            result[defs_key] = {
                k: _semantic_repr(v, base_path, schemas)
                for k, v in node[defs_key].items()
            }
    for key in ("anyOf", "oneOf", "allOf"):
        if key in result and isinstance(result[key], list):
            src = node.get(key, [])
            result[key] = [
                _semantic_repr(item, base_path, schemas)
                for i, item in enumerate(result[key])
                if i < len(src)
            ]
    return result


@lru_cache(maxsize=1)
def current_structural_fingerprint() -> str:
    """聚合所有 schema 的结构指纹（递归解析 $ref，含 FP_ALGO_VERSION）。

    仅当「能否解析」相关的结构约束变化时才改变 → 用于判定真 breaking。
    """
    schemas = _load_signals_schemas()
    material = {
        "fp_algo_version": FP_ALGO_VERSION,
        "structural": {
            path: _structural_repr(doc, path, schemas)
            for path, doc in schemas.items()
        },
    }
    return _fingerprint(material)


@lru_cache(maxsize=1)
def current_semantic_fingerprint() -> str:
    """聚合所有 schema 的语义指纹（结构 + description/default）。

    兼容演进（仅语义漂移、结构不变）→ 改变此值但结构指纹不变 → 触发 soft_stale。
    """
    schemas = _load_signals_schemas()
    material = {
        "fp_algo_version": FP_ALGO_VERSION,
        "semantic": {
            path: _semantic_repr(doc, path, schemas)
            for path, doc in schemas.items()
        },
    }
    return _fingerprint(material)


def build_signal_generation_metadata(
    *,
    source: dict[str, Any],
    prompt_template: str,
    model_id: str,
) -> dict[str, Any]:
    """Build the immutable inputs needed to decide whether Signal/Contract is stale."""
    source_fingerprint = _fingerprint(source)
    prompt_revision = hashlib.sha256(prompt_template.encode()).hexdigest()
    tool_contract_revision = current_tool_contract_revision()
    generation_fingerprint = _fingerprint(
        {
            "source_fingerprint": source_fingerprint,
            "prompt_revision": prompt_revision,
            "model_id": model_id,
            "tool_contract_revision": tool_contract_revision,
        }
    )
    return {
        "schema_version": 1,
        "status": "current",
        "source_fingerprint": source_fingerprint,
        "prompt_revision": prompt_revision,
        "model_id": model_id,
        "tool_contract_revision": tool_contract_revision,
        "generation_fingerprint": generation_fingerprint,
    }


def staleness_reasons(
    stored: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[str]:
    """Return closed, machine-readable reasons; an absent legacy record is untracked."""
    if not stored:
        return ["generation_metadata_missing"]
    reasons: list[str] = []
    if stored.get("status") == "stale":
        reasons.append("explicitly_marked_stale")
    for field in (
        "source_fingerprint",
        "prompt_revision",
        "model_id",
        "tool_contract_revision",
    ):
        if stored.get(field) != current.get(field):
            reasons.append(f"{field}_changed")
    return reasons
