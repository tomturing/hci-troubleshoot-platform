"""KBD 语义入口画像的纯契约与确定性匹配辅助。

语义入口画像不是一个可执行采集器。它只描述当任务、告警、弹框三类强生产者
均确认未命中时，如何由用户已提供的故障描述进行受限候选召回。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from jsonschema import ValidationError

from shared.schemas.acquirer_args import CONDITIONAL_PRODUCERS, FRONTEND_TOOLS

SEMANTIC_ENTRY_TOOL = "qkv_case_context"
SEMANTIC_CAPABILITIES = frozenset({"executable", "guidance_only", "capability_gap"})
STRONG_PRODUCER_TOOLS = frozenset(FRONTEND_TOOLS)
MAX_CONTEXT_CHARS = 16000
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]+|[\u4e00-\u9fff]{2,}")
_ERROR_CODE_RE = re.compile(r"\b(?:[A-Z][A-Z0-9_]{1,31}-\d{2,}|(?:ERR|ERROR|E)\d{2,}|0x[0-9A-Fa-f]{4,})\b")
_QUOTED_TEXT_RE = re.compile(r"[\"“‘']([^\"”’']{2,160})[\"”’']")
_OBJECT_TERMS = (
    "虚拟机",
    "VM",
    "主机",
    "节点",
    "网络",
    "网卡",
    "存储",
    "磁盘",
    "备份",
    "容器",
    "服务",
    "镜像",
    "许可证",
)
_OPERATION_TERMS = ("创建", "启动", "关闭", "迁移", "备份", "恢复", "升级", "扩容", "删除", "登录", "连接", "挂载")


def semantic_entry_profile(raw: Any) -> dict[str, Any] | None:
    """读取 v2 signals 文档中的画像；不接受隐式或旧 metadata 旁路。"""

    if not isinstance(raw, dict):
        return None
    profile = raw.get("semantic_entry_profile")
    return profile if isinstance(profile, dict) else None


def has_case_context_signal(raw: Any) -> bool:
    signals = raw.get("signals") if isinstance(raw, dict) else None
    return any(
        isinstance(signal, dict)
        and isinstance(signal.get("acquire"), dict)
        and signal["acquire"].get("tool") == SEMANTIC_ENTRY_TOOL
        for signal in signals or []
    )


def capability_of(raw: Any) -> str | None:
    profile = semantic_entry_profile(raw)
    value = profile.get("diagnosis_capability") if profile else None
    return str(value) if value in SEMANTIC_CAPABILITIES else None


def validate_semantic_entry_profile(raw: Any) -> None:
    """校验画像和 qkv_case_context 的成对出现及安全边界。"""

    profile = semantic_entry_profile(raw)
    has_context = has_case_context_signal(raw)
    if profile is None and not has_context:
        return
    if profile is None or not has_context:
        raise ValidationError(
            "语义入口必须同时声明 semantic_entry_profile 与 qkv_case_context，禁止只保留其中一项",
            path=["semantic_entry_profile" if profile is None else "signals"],
        )
    if profile.get("schema_version") != 1:
        raise ValidationError(
            "semantic_entry_profile.schema_version 必须为 1", path=["semantic_entry_profile", "schema_version"]
        )
    capability = profile.get("diagnosis_capability")
    if capability not in SEMANTIC_CAPABILITIES:
        raise ValidationError(
            "semantic_entry_profile.diagnosis_capability 必须是 executable、guidance_only 或 capability_gap",
            path=["semantic_entry_profile", "diagnosis_capability"],
        )
    if capability == "executable" and not any(
        str((signal.get("acquire") or {}).get("tool") or "").startswith("qfk_")
        and (signal.get("orchestrate") or {}).get("phase", "diagnostic") == "diagnostic"
        for signal in raw.get("signals", [])
        if isinstance(signal, dict)
    ):
        raise ValidationError(
            "executable 画像至少需要一条诊断阶段消费者；没有验证能力请选择 guidance_only",
            path=["semantic_entry_profile", "diagnosis_capability"],
        )
    if any(
        str(ref).split(":")[-1] in {"root_cause", "solution", "recommendations"}
        for ref in profile.get("source_refs", [])
    ):
        raise ValidationError(
            "语义画像不能引用根因或修复方案作为问题侧依据", path=["semantic_entry_profile", "source_refs"]
        )
    for field in ("canonical_symptoms", "positive_anchors"):
        values = profile.get(field)
        if not isinstance(values, list) or not any(isinstance(value, str) and value.strip() for value in values):
            raise ValidationError(
                f"semantic_entry_profile.{field} 至少需要一条非空文本",
                path=["semantic_entry_profile", field],
            )
    for field in ("exclusion_anchors", "manual_evidence_request"):
        values = profile.get(field, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValidationError(
                f"semantic_entry_profile.{field} 必须是字符串数组", path=["semantic_entry_profile", field]
            )
    if capability == "guidance_only" and not any(
        str(item).strip() for item in profile.get("manual_evidence_request") or []
    ):
        raise ValidationError(
            "guidance_only 画像必须提供 manual_evidence_request，明确需要用户补充什么证据",
            path=["semantic_entry_profile", "manual_evidence_request"],
        )
    for index, example in enumerate(profile.get("routing_examples", [])):
        _, positive, negative = lexical_profile_score(profile, example["description"])
        if bool(positive and not negative) != example["expected_match"]:
            raise ValidationError(
                "画像变更后正反例不再通过，请复核症状、筛选条件及该测试输入",
                path=["semantic_entry_profile", "routing_examples", index],
            )


def profile_source_evidence(profile: dict[str, Any], source: dict[str, Any]) -> list[dict[str, str]]:
    """只为可逐字回查的画像字段建立原文引用，不推测缺失来源。"""
    evidence = []
    for field in ("canonical_symptoms", "positive_anchors", "exclusion_anchors"):
        for index, value in enumerate(profile.get(field, [])):
            for section in ("problem_description", "alert_info", "steps_text"):
                raw = str(source.get(section) or "")
                if value and value in raw:
                    evidence.append(
                        {
                            "field_path": f"{field}.{index}",
                            "source_ref": section,
                            "quote": value,
                            "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                        }
                    )
                    break
    return evidence


def validate_profile_source_evidence(document: Any, source: dict[str, Any]) -> None:
    """发布时复查已声明的引用；正文改变使引用失效，不悄悄沿用旧审核。"""
    profile = semantic_entry_profile(document) or {}
    for index, evidence in enumerate(profile.get("source_evidence", [])):
        raw = str(source.get(evidence["source_ref"]) or "")
        field, offset = evidence["field_path"].split(".")
        values = profile.get(field, [])
        if (
            hashlib.sha256(raw.encode()).hexdigest() != evidence["source_sha256"]
            or evidence["quote"] not in raw
            or int(offset) >= len(values)
            or values[int(offset)] not in evidence["quote"]
        ):
            raise ValidationError(
                "画像原文引用已过期或不能逐字回查，请重新核对并关联原文",
                path=["semantic_entry_profile", "source_evidence", index],
            )


def profile_text(profile: dict[str, Any]) -> str:
    """供向量/词法索引使用的无答案侧文本，绝不拼接根因或解决方案。"""

    pieces: list[str] = []
    for field in ("canonical_symptoms", "positive_anchors", "scope"):
        value = profile.get(field)
        if isinstance(value, list):
            pieces.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            pieces.append(value.strip())
    return "\n".join(pieces)


def normalize_case_context(raw: Any) -> dict[str, str]:
    """将客户输入规范为可审计的检索片段，不使用 LLM 推断任何字段。

    ``description`` 是保底症状片段；错误码、引号中的报错原文、对象/操作仅由
    明确文本或上游受控表单给出。它们只能用于候选排序，绝不能进入执行变量池。
    """

    source = raw if isinstance(raw, dict) else {"description": raw}
    description = str(source.get("description") or source.get("raw_text") or "").strip()[:MAX_CONTEXT_CHARS]
    if not description:
        return {}

    explicit_error = str(source.get("error_text") or "").strip()[:1000]
    quoted_errors = [
        item.strip() for item in _QUOTED_TEXT_RE.findall(description) if affirmative_anchor_hit(item, description)
    ]
    error_codes = [item for item in _ERROR_CODE_RE.findall(description) if affirmative_anchor_hit(item, description)]
    error_text = explicit_error or "；".join(dict.fromkeys([*error_codes, *quoted_errors]))[:1000]

    explicit_object = str(source.get("object_type") or "").strip()[:80]
    explicit_operation = str(source.get("operation") or "").strip()[:80]
    objects = [term for term in _OBJECT_TERMS if term.casefold() in description.casefold()]
    operations = [term for term in _OPERATION_TERMS if term in description]
    object_operation = " ".join(
        dict.fromkeys([item for item in (explicit_object, explicit_operation, *objects, *operations) if item])
    )[:240]

    segments = {"symptom": description}
    if error_text:
        segments["error_text"] = error_text
    if object_operation:
        segments["object_operation"] = object_operation
    return segments


def semantic_segment_weights(segments: dict[str, str]) -> dict[str, float]:
    """按可用片段归一化固定权重，禁止由模型或调用方自由调参。"""

    configured = {"symptom": 0.55, "error_text": 0.30, "object_operation": 0.15}
    active = {key: configured[key] for key, value in segments.items() if value and key in configured}
    total = sum(active.values())
    return {key: value / total for key, value in active.items()} if total else {}


def lexical_profile_score(profile: dict[str, Any], context: str) -> tuple[float, list[str], list[str]]:
    """确定性守门：正向锚点加分、排除锚点直接否决。

    它不是 LLM 判断，也不从文本推导 HOST/VM_ID 等执行目标；只为向量候选提供
    可解释的最低安全门槛。
    """

    normalized = context.casefold()
    positives = [str(item).strip() for item in profile.get("positive_anchors") or [] if str(item).strip()]
    exclusions = [str(item).strip() for item in profile.get("exclusion_anchors") or [] if str(item).strip()]
    matched_positive = [item for item in positives if affirmative_anchor_hit(item, context)]
    matched_exclusion = [item for item in exclusions if affirmative_anchor_hit(item, context)]
    if matched_exclusion:
        return 0.0, matched_positive, matched_exclusion
    context_tokens = set(_TOKEN_RE.findall(normalized))
    profile_tokens = set(_TOKEN_RE.findall(profile_text(profile).casefold()))
    overlap = len(context_tokens & profile_tokens) / max(1, len(profile_tokens))
    score = min(1.0, overlap + min(0.65, 0.25 * len(matched_positive)))
    return score, matched_positive, matched_exclusion


def affirmative_anchor_hit(anchor: str, context: str) -> bool:
    """只接受当前描述中的肯定命中；不把明确否定或已解决历史当作当前事实。"""
    needle = anchor.strip().casefold()
    if not needle:
        return False
    for clause in re.split(r"[，,。；;\n]", context.casefold()):
        if re.match(r"\s*(?:历史上|以前|曾经|此前已解决|之前已解决)", clause):
            continue
        for match in re.finditer(re.escape(needle), clause):
            prefix = clause[: match.start()]
            if not re.search(r"(?:未出现|未发生|没有出现|没有发生|没有|并非|不是|排除)[\s\"“‘']*$", prefix):
                return True
    return False


def semantic_context_text(raw: Any) -> str:
    """文字门禁与向量使用相同的显式输入，避免独立报错字段被遗漏。"""
    source = raw if isinstance(raw, dict) else {"description": raw}
    segments = normalize_case_context(raw)
    # 自动提取的对象词／引号内容已丢失句子极性，不能重新作为肯定事实。
    return "\n".join(filter(None, [segments.get("symptom", ""), str(source.get("error_text") or "")[:1000]]))


def profile_digest(profile: dict[str, Any]) -> str:
    """内容寻址：发布、停用、回滚后不能误用另一份画像的派生结果。"""
    return hashlib.sha256(
        json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def has_strong_producer(raw: Any) -> bool:
    signals = raw.get("signals", []) if isinstance(raw, dict) else []
    return any(
        (signal.get("acquire") or {}).get("tool") in STRONG_PRODUCER_TOOLS
        for signal in signals
        if isinstance(signal, dict)
    )


def propose_semantic_profile(source: dict[str, Any], signals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从问题原文保守生成待审核画像；不使用标题中的根因或任何修复方案。"""
    if has_strong_producer({"signals": signals}) or any(
        (item.get("acquire") or {}).get("tool") in CONDITIONAL_PRODUCERS for item in signals
    ):
        return None
    text = str(source.get("problem_description") or "")[:16000]
    symptoms = [
        line.strip()
        for line in re.split(r"[。；;\n]", text)
        if 6 <= len(line.strip()) <= 300
        and re.search(r"失败|异常|报错|无法|不显示|不支持|不足|超时|蓝屏|黑屏|error|failed", line, re.I)
        and not re.search(r"根因|解决|修复|删除|重启|导致", line)
    ]
    if not symptoms:
        return None
    # 锚点必须逐字来自问题描述；没有特征报错时保留原句，宁可召回窄也不编造。
    anchors = (
        list(
            dict.fromkeys([*_ERROR_CODE_RE.findall("；".join(symptoms)), *_QUOTED_TEXT_RE.findall("；".join(symptoms))])
        )
        or symptoms[:3]
    )
    executable = any(str((item.get("acquire") or {}).get("tool") or "").startswith("qfk_") for item in signals)
    profile = {
        "schema_version": 1,
        "diagnosis_capability": "executable" if executable else "guidance_only",
        "canonical_symptoms": symptoms[:5],
        "positive_anchors": anchors[:10],
        "exclusion_anchors": [],
        "source_refs": ["kbd:problem_description"],
        "manual_evidence_request": []
        if executable
        else ["请提供当前故障的完整报错和发生前的操作，由人工复核；平台目前没有可验证的消费者。"],
    }
    profile["source_evidence"] = profile_source_evidence(profile, source)
    profile["routing_examples"] = [
        {"description": "；".join(symptoms[:5]), "expected_match": True},
        {"description": "；".join(f"没有出现“{anchor}”" for anchor in anchors[:10]), "expected_match": False},
    ]
    return profile
