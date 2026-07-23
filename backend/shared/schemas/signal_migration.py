"""关键信号扁平 v1 → 嵌套 v2（数组级 schema_version）迁移逻辑。

来源：RFC《关键信号数据模型分层重构》§4.1 / §4.2 / §7。
本模块提供**纯函数**，可被迁移脚本、单元测试、干跑工具复用，不依赖数据库连接。

形态对照
--------
v1（当前线上）：`signals_json` 是一段**扁平 list**[{signal_category, keyword, acquirer,
  acquirer_args, matcher, produces, requires, expected, match_mode, risk, ...}]
v2（目标）    ：`signals_json = { schema_version: 2, signals: [ {id, acquire, match,
  orchestrate, provenance, review}, ... ] }`

迁移规则（详见 RFC §4.2 映射表）
--------------------------------
- acquirer        → acquire.tool
- acquirer_args.* → acquire.args.*（public/common 字段沿用，status 由 is_failed 派生）
- qfk acquirer_args.keyword → acquire.args.resource_keyword（改名消歧，§4.4.4）
- matcher.*       → match.{type,pattern,mode,expected}（仅 backend；frontend 可缺）
- 顶层 expected / match_mode（qkv 死副本）→ 不迁移（RFC §2.7.1）
- produces/requires/phase/action/source/target/container → orchestrate.*
- signal_category → provenance.category
- extraction_method → provenance.method；risk / confidence / needs_review / source_section → provenance.*
- require_human_confirm → review.require_human_confirm（唯一运行时门禁，§2.7.2）

幂等：输入已是 v2 对象（含 schema_version）或已是 v2 单条（含 acquire 段）则原样返回。
无损：未被识别的 v1 顶层字段统一收进 `_v1_legacy`，绝不静默丢弃，便于人工复核。
"""

from __future__ import annotations

import copy
from typing import Any

# v1 顶层字段中，被本迁移显式归位的键（其余未知的收进 _v1_legacy）
_HANDLED_V1_TOP = {
    "id",
    "signal_category",
    "keyword",
    "acquirer",
    "acquirer_args",
    "matcher",
    "produces",
    "requires",
    "phase",
    "action",
    "source",
    "source_section",
    "target",
    "container",
    "timeout",
    "expected",
    "match_mode",
    "risk",
    "extraction_method",
    "confidence",
    "needs_review",
    "require_human_confirm",
    "is_failed",
    "description",
}

# 后端（QFK）工具：其 acquirer_args.keyword 是"资源/主题"选择器，需改名 resource_keyword
_BACKEND_TOOLS = {
    "qfk_log",
    "qfk_service",
    "qfk_system",
    "qfk_vm",
    "qfk_network",
    "qfk_storage",
    "qfk_hardware",
    "qfk_platform",
}

SIGNAL_SCHEMA_VERSION = 2


def _migrate_args(acquirer: str, acquirer_args: dict[str, Any]) -> dict[str, Any]:
    """扁平 acquirer_args → 嵌套 acquire.args（含 status→is_failed 派生与 qfk 改名）。"""
    args = copy.deepcopy(acquirer_args or {})

    # status → is_failed 派生（RFC §4.2：删 status）
    status = args.pop("status", None)
    if status == "failed" and not args.get("is_failed"):
        args["is_failed"] = True

    # qfk 的 keyword 是"资源/主题"选择器，改名消歧（RFC §4.4.4）
    if acquirer in _BACKEND_TOOLS and "keyword" in args:
        args["resource_keyword"] = args.pop("keyword")

    return args


def _migrate_one_signal(sig: dict[str, Any]) -> dict[str, Any]:
    """单条 v1 扁平信号 → v2 嵌套信号（幂等：已含 acquire 段则原样返回）。"""
    if not isinstance(sig, dict):
        return sig
    if "acquire" in sig:  # 已是 v2 段结构
        return copy.deepcopy(sig)

    out: dict[str, Any] = {}
    if "id" in sig:
        out["id"] = sig["id"]

    acquirer = sig.get("acquirer", "")
    args = _migrate_args(acquirer, sig.get("acquirer_args", {}))
    # v1 顶层 keyword / description 是 QKV 采集关键词与信号说明的权威来源，
    # 须并入 acquire.args（而非收进 _v1_legacy），否则 v2 契约校验会因缺 keyword 而 422。
    if "keyword" in sig:
        args.setdefault("keyword", sig["keyword"])
    if "description" in sig:
        args.setdefault("description", sig["description"])
    out["acquire"] = {
        "tool": acquirer,
        "args": args,
    }

    # match 段：仅 backend 且存在 matcher 时构建
    matcher = sig.get("matcher") or {}
    if acquirer in _BACKEND_TOOLS and matcher:
        out["match"] = {
            "type": matcher.get("type"),
            "pattern": matcher.get("pattern"),
            "mode": matcher.get("mode", "or"),
            "expected": bool(matcher.get("expected", True)),
        }
    # frontend（qkv）不构建 match 段（无匹配语义，RFC §2.7.1）

    # orchestrate 段（执行路径）
    orchestrate: dict[str, Any] = {}
    for k in ("phase", "action", "source", "target", "container", "produces", "requires"):
        if k in sig:
            orchestrate[k] = sig[k]
    if orchestrate:
        out["orchestrate"] = orchestrate

    # provenance 段（来源与质量，不进执行路径）
    provenance: dict[str, Any] = {}
    if "signal_category" in sig:
        provenance["category"] = sig["signal_category"]
    if "extraction_method" in sig:
        provenance["method"] = sig["extraction_method"]  # 溯源戳，不进运行时分支
    if "source_section" in sig:
        provenance["source_section"] = sig["source_section"]
    if "confidence" in sig:
        provenance["confidence"] = sig["confidence"]
    if "risk" in sig:
        provenance["risk"] = sig["risk"]  # 质量标注，不参与门禁
    if "needs_review" in sig:
        provenance["needs_review"] = sig["needs_review"]
    if provenance:
        out["provenance"] = provenance

    # review 段（审核/门禁）
    out["review"] = {"require_human_confirm": bool(sig.get("require_human_confirm", False))}

    # 无损：未识别的 v1 顶层字段收进 _v1_legacy 供人工复核
    legacy = {k: v for k, v in sig.items() if k not in _HANDLED_V1_TOP}
    if legacy:
        out["_v1_legacy"] = legacy

    return out


def migrate_signal_document(raw: Any) -> dict[str, Any]:
    """将整段 `signals_json` 迁移为 v2 数组级对象，幂等且无损。

    Args:
        raw: 数据库原始 signals_json。可能形态：
             - 已是 v2 对象 {schema_version, signals:[...]} → 原样返回
             - 扁平 list（v1）→ 包装为 v2 对象
             - 单条 dict（异常） → 包装为 {schema_version:2, signals:[migrated]}

    Returns:
        {schema_version: 2, signals: [...]}
    """
    if isinstance(raw, dict) and "schema_version" in raw and "signals" in raw:
        return copy.deepcopy(raw)  # 已是 v2

    if isinstance(raw, dict) and "acquire" in raw:  # 单条已迁移
        return {"schema_version": SIGNAL_SCHEMA_VERSION, "signals": [copy.deepcopy(raw)]}

    signals = raw if isinstance(raw, list) else [raw]
    migrated = [_migrate_one_signal(s) for s in signals]
    return {"schema_version": SIGNAL_SCHEMA_VERSION, "signals": migrated}


def normalize_qfk_keyword_alias(sig: dict[str, Any]) -> None:
    """写边界 / 读边界别名归一（v1 历史字段消歧，RFC §4.4.4）：

    将 QFK 信号的 ``acquire.args.keyword``（v1 历史别名，资源/主题选择器）原地归并为
    ``resource_keyword``（v2 契约字段）。QKV 信号的 ``keyword`` 是采集关键词权威字段，
    保持不变。

    幂等：无 ``keyword`` 则跳过；``resource_keyword`` 已存在（含非空）则保留旧值、丢弃
    ``keyword``，避免覆盖。

    调用点：
    - 写边界 ``update_kbd_entry``（保存前）：无论数据来自前端回写还是存量「半残 v2」
      （PR 修复前抽取、args 带 ``keyword``），均归一为契约字段，避免 §6.1
      ``additionalProperties:false`` 校验 422（KBD 详情页「保存失败，请重试」根因）。
    - 读边界 ``_signals_for_response``（GET 出口）：使前端编辑框（绑定 ``resource_keyword``）
      正确显示历史 ``keyword`` 值，消除显示错位且不丢数据。
    """
    if not isinstance(sig, dict):
        return
    acquire = sig.get("acquire") or {}
    if acquire.get("tool") not in _BACKEND_TOOLS:
        return
    args = acquire.get("args") or {}
    if "keyword" in args:
        args["resource_keyword"] = args.get("resource_keyword") or args.pop("keyword")


def unwrap_signals(raw: Any) -> list[dict[str, Any]]:
    """统一解包：无论 signals_json 是 v2 对象 {schema_version, signals} 还是扁平 list，
    都返回信号 dict 列表。供所有读取方（agent/admin/前端）在边界处一次性归一。"""
    if isinstance(raw, dict) and isinstance(raw.get("signals"), list):
        return raw["signals"]
    if isinstance(raw, list):
        return raw
    return []


def to_legacy_signal(sig: dict[str, Any]) -> dict[str, Any]:
    """v2 嵌套信号 → v1 扁平信号（reverse of _migrate_one_signal）。

    用于在读取边界把 v2 还原为既有代码（kbd_differential / 模型）期望的扁平形状，
    使下游零改动。已为扁平的信号原样返回（幂等）。所有字段语义与 §4.2 映射表严格互逆。
    """
    if not isinstance(sig, dict) or "acquire" not in sig:
        return copy.deepcopy(sig) if isinstance(sig, dict) else sig

    acquire = sig.get("acquire", {})
    args = copy.deepcopy(acquire.get("args", {}) or {})
    tool = acquire.get("tool", "")

    # qfk：resource_keyword → keyword（逆迁移改名）
    if tool in _BACKEND_TOOLS and "resource_keyword" in args:
        args["keyword"] = args.pop("resource_keyword")

    legacy: dict[str, Any] = {
        "id": sig.get("id"),
        "signal_category": "backend" if tool.startswith("qfk") else "frontend",
        "acquirer": tool,
        "acquirer_args": args,
    }

    matcher = sig.get("match")
    if matcher:
        legacy["matcher"] = {
            "type": matcher.get("type"),
            "pattern": matcher.get("pattern"),
            "mode": matcher.get("mode", "or"),
            "expected": bool(matcher.get("expected", True)),
        }

    orchestrate = sig.get("orchestrate") or {}
    for k in ("phase", "action", "source", "target", "container", "produces", "requires"):
        if k in orchestrate:
            legacy[k] = orchestrate[k]

    provenance = sig.get("provenance") or {}
    if "category" in provenance:
        legacy["signal_category"] = provenance["category"]
    if "method" in provenance:
        legacy["extraction_method"] = provenance["method"]
    if "source_section" in provenance:
        legacy["source_section"] = provenance["source_section"]
    if "confidence" in provenance:
        legacy["confidence"] = provenance["confidence"]
    if "risk" in provenance:
        legacy["risk"] = provenance["risk"]
    if "needs_review" in provenance:
        legacy["needs_review"] = provenance["needs_review"]

    review = sig.get("review") or {}
    if "require_human_confirm" in review:
        legacy["require_human_confirm"] = review["require_human_confirm"]

    # 无损：迁移时收进 _v1_legacy 的字段还原回去
    if isinstance(sig.get("_v1_legacy"), dict):
        legacy.update(sig["_v1_legacy"])

    return legacy
