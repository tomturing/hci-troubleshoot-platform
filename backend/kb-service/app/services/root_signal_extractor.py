"""根因信号抽取核心模块 — 与关键信号完全解耦的第二条抽取轨。

定位与边界（详见 docs/solution/knowledge-base/根因信号抽取方案.md）：
- 关键信号 = 「诊断叙事输入」（问题侧，S0-S3 逐步排查的 producer/consumer 信号）；
- 根因信号 = 「结论侧定位锚点」（每篇 KBD 抽取全部可确定根因的定位信号，不限条数，
  用于快速圈定符合该根因特性的 KBD 集合）。两者存储、Prompt、门禁、方法标识完全分离：
  根因信号不进 signals_json、不受 CDD/QFK producer-consumer 发布门禁约束。
- 与「语义入口画像」的边界：语义入口用问题侧描述匹配用户已提交上下文；根因信号用
  结论侧特征匹配现场证据/报错文本，方向相反、字段独立，仅匹配原语可复用。

v2 契约（多信号 + 整篇输入 + 溯源分层）：
- 输入面：抽取 Prompt 接收整篇 KBD（占位符 kbd_content），不再只读 root_cause；
- 输出面：root_signal_json 为顶层 JSON 数组 [{signal}, ...]，单篇不限条数（门禁封顶防噪）；
- 溯源分层（trace_tier）：信号 provenance.evidence 逐字命中 root_cause 者为「主锚」（高信任）；
  仅命中整篇其余章节者为「兑底」，强制 needs_review 转人工复核；整篇都溯源不到即拒收；
- 主锚优先：只要存在 root_cause 级信号就只采用主锚，不用兑底填充（防噪声稀释，实测依据见方案 §10）。

双形态 contract：
- executable：acquire.tool 取自 ACQUIRER_CATALOG 的 qfk_* 消费判定原语（禁 qfk_var），
  args 过 validate_acquire_args，且过 kbd_signal_read_only_violation 只读安全门；
- text：无法映射采集器时降级，match.expected 为可溯源至所溯源章节原文的独特
  错误文本/谓词关键词，平台侧在已采集证据/告警文本/用户输入上做包含匹配。

Prompt 管理与关键信号完全对齐：存于 system_prompt 表（stage='KEY'，
name='kbd_extract_root_signal_v2'），经 StrictPromptLoader 每次抽取热加载，
admin-ui「KEY 关键信号分级抽取」页在线编辑即时生效。占位符契约见
ROOT_SIGNAL_PLACEHOLDERS。

LLM 调用由调用方经 llm_call 回调注入（治理与可观测性在调用侧完成：
call_with_llm_governance + Langfuse observe_llm_generation + KBD_LLM_* 指标），
本模块保持纯门禁 + 归一化逻辑，保证可单测、可离线试点。
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections.abc import Awaitable, Callable
from string import Formatter
from typing import Any

from shared.observability.logger import get_logger
from shared.schemas.acquirer_args import ACQUIRER_ARGS_SCHEMA, validate_acquire_args
from shared.schemas.kbd_signal_safety import kbd_signal_read_only_violation

from app.routes.extract_signals import ACQUIRER_CATALOG, VALID_MATCHER_TYPES
from app.services.root_signal_closure import adjudicate_root_closure

logger = get_logger("kb-service-root-signal")

# ─── 常量契约 ─────────────────────────────────────────────────────────────────
ROOT_SIGNAL_PROMPT_NAME = "kbd_extract_root_signal_v2"
ROOT_SIGNAL_SCHEMA_VERSION = 2
ROOT_SIGNAL_METHOD = "llm_root_signal_v2"
# Prompt 占位符契约（与种子 SQL 一致；StrictPromptLoader 同法校验）
# v2 变更：输入面由仅 root_cause 放开为整篇 kbd_content（req#2）；root_cause 仍单独
# 传入用于「主锚」溯源判定（不是 Prompt 占位符）。
ROOT_SIGNAL_PLACEHOLDERS = [
    "title",
    "category_id",
    "category_path",
    "kbd_content",
    "acquirer_catalog",
]
VALID_MATCH_TYPES = {"executable", "text"}
# v2 允许信号出自整篇任一章节（溯源分层由 _trace_tier 决定主锚/兜底）
VALID_SOURCE_SECTIONS = {
    "problem_description",
    "alert_info",
    "steps_text",
    "root_cause",
    "solution",
    "operational_impact",
    "recommendations",
}
# text 型仅允许确定性包含匹配，避免 regex 之外的语义漂移
TEXT_MATCH_TYPES = {"keyword", "regex"}
# 关键词最短长度：低于该长度的中文/英文 token 通用度过高，无法承担「快速定位」职责
MIN_KEYWORD_LEN = 4
# 纯数字/分隔符关键词（如 0.95、6.11）无区分度，必须搭配语境词（如 HCI 6.11.1）才可用
_WEAK_KEYWORD_RE = re.compile(r"^[\d._:%\-]+$")
# executable 型可用 matcher 类型收敛到「结论判定」语义子集
EXECUTABLE_MATCH_TYPES = VALID_MATCHER_TYPES & {"keyword", "regex", "state", "boolean", "threshold", "exists"}


def _normalize_for_trace(text: str) -> str:
    """溯源归一化：去空白、转小写，令关键词可在原文中稳定定位。"""
    return re.sub(r"\s+", "", text or "").casefold()


def _args_contract_lines(tool: str) -> str:
    """从 ACQUIRER_ARGS_SCHEMA 生成单工具 args 契约摘要（与门禁同源，消除「门禁比 Prompt 严」缝隙）。"""
    schema = ACQUIRER_ARGS_SCHEMA.get(tool) or {}
    required = set(schema.get("required") or [])
    parts: list[str] = []
    for name, spec in (schema.get("properties") or {}).items():
        if name in ("timeout", "nonzero_exit_as_negative"):
            continue
        desc = str(spec.get("description") or "").split("；")[0].split("(")[0].strip()
        if len(desc) > 36:
            desc = desc[:36] + "…"
        star = "*" if name in required else ""
        parts.append(f"{name}{star}({desc})" if desc else f"{name}{star}")
    return " | ".join(parts)


# 与 validate_acquire_args 实际语义对齐的隐含必填/使用条件（JSONSchema required 反映不到的部分）：
# v3 试点发现 qfk_log 的 file 在校验器里条件性必传，但 schema required=[] 使目录摘要标成可选，
# 模型合理省略 file 后被拒两次耗尽重试——目录文本必须把这类隐含约束显式化。
_TOOL_USAGE_NOTES = {
    "qfk_log": "file 必传且必须是已登记日志源的 basename（如 messages、sflog、vmtool）；根因无法确定具体日志文件名时不要选 qfk_log",
}


def acquirer_catalog_text() -> str:
    """Prompt 内嵌采集器目录：qfk_* 消费判定原语 + args 契约摘要（qfk_var 专家专属，禁止生成）。

    试点发现（2026-10-12）：仅给一句话描述时 LLM 无法猜中 additionalProperties:false 的
    严格契约（file 传全路径、自造 keyword 字段），executable 型全部被迫退化 text；
    故目录文本必须携带与 validate_acquire_args 同源的关键字段契约。
    """
    lines = []
    for name, desc in ACQUIRER_CATALOG.items():
        if not name.startswith("qfk_") or name == "qfk_var":
            continue
        note = _TOOL_USAGE_NOTES.get(name)
        suffix = f"\n  ⚠ {note}" if note else ""
        lines.append(
            f"- {name}: {desc}\n"
            f"  args 契约（带*必填，字段名不得超出此表）: {_args_contract_lines(name)}{suffix}"
        )
    return "\n".join(lines)


def build_root_signal_prompt(template: str, entry: dict[str, Any]) -> str:
    """按占位符契约渲染抽取 Prompt（缺参即抛错，绝不静默降级）。"""
    missing = [key for key in ROOT_SIGNAL_PLACEHOLDERS if key not in entry]
    if missing:
        raise KeyError(f"根因信号抽取输入缺少占位符字段: {missing}")
    return template.format(**{key: entry[key] for key in ROOT_SIGNAL_PLACEHOLDERS})


# ─── 门禁（纯函数，返回 (结果, 错误原因)）─────────────────────────────────────

def gate_signals(payload: Any, *, max_signals: int = 8) -> tuple[list[dict[str, Any]], str | None]:
    """LLM 返回的根因信号集合门禁（v2：不限条数，但仍封顶防长输出噪声洪水）。

    接受 `signal` 单对象、`signals` 数组或裸对象；空集即拒收；按 signal_text 去重。
    返回 (原始信号列表, 错误原因)。
    """
    if not isinstance(payload, dict):
        return [], f"LLM 响应必须是 JSON 对象，实际为 {type(payload).__name__}"
    raw_list: Any = payload.get("signals")
    if raw_list is None:
        single = payload.get("signal")
        raw_list = [single] if isinstance(single, dict) else ([payload] if payload.get("match_type") else [])
    if not isinstance(raw_list, list) or not raw_list:
        return [], "响应缺少 signals 数组（或 signal 对象）"
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        key = _normalize_for_trace(str(item.get("signal_text") or ""))
        if key and key in seen:
            continue  # 按 signal_text 去重
        seen.add(key)
        out.append(item)
        if len(out) >= max_signals:
            break
    if not out:
        return [], "signals 数组无有效对象"
    return out, None


def coerce_acquire(acquire: Any) -> Any:
    """无损归一化 acquire.args：file 类字段全路径降级为 basename。

    试点发现（2026-10-12 v3）：「file 只收 basename」回喂两次仍被模型惯性违反（2 篇因此 FAIL）。
    契约上 qfk_log 的目录不参与资源选择（由采集器内部定位），取 basename 不改变目标文件语义，
    故服务端做确定性归一化而非烧轮次拒收；归一化后仍过完整 args 契约与只读门，不降低任何门禁。
    """
    if not isinstance(acquire, dict):
        return acquire
    args = acquire.get("args")
    if not isinstance(args, dict):
        return acquire
    coerced = False
    new_args = dict(args)
    # 仅 file 字段适用 basename 降级；path 是搜索目录（要求绝对路径），方向相反不得归一
    value = new_args.get("file")
    if isinstance(value, str) and "/" in value:
        base = posixpath.basename(value.rstrip("/"))
        if base:
            new_args["file"] = base
            coerced = True
    if not coerced:
        return acquire
    return {**acquire, "args": new_args}


def gate_executable(signal: dict[str, Any]) -> str | None:
    """executable 型门禁：qfk_* 词表 + args 契约 + 只读安全。返回违规原因或 None。"""
    acquire = signal.get("acquire")
    if not isinstance(acquire, dict):
        return "executable 型必须携带 acquire 对象"
    tool = str(acquire.get("tool") or "")
    if tool not in ACQUIRER_CATALOG:
        return f"acquire.tool 不在采集器词表内: {tool}"
    if not tool.startswith("qfk_"):
        return f"根因信号是结论判定，acquire.tool 必须是 qfk_* 消费原语: {tool}"
    if tool == "qfk_var":
        return "qfk_var 仅供专家在管理端维护，根因信号抽取禁止生成"
    ok, error = validate_acquire_args(tool, acquire.get("args") or {})
    if not ok:
        return f"acquire.args 契约校验失败: {error}"
    violation = kbd_signal_read_only_violation({"acquire": acquire})
    if violation:
        return f"只读安全门拒绝: {violation}"
    return None


def _flatten_keywords(expected: Any) -> tuple[list[str] | None, str | None]:
    """扁平化 + 去重 match.expected（保留原序）。

    试点发现（2026-10-12）：LLM 偶发返回嵌套数组（如 [["a","b"]]），直接 str() 会把
    Python list repr 当关键词落盘；同时存在大小写异体重复，故统一扁平去重。
    """
    if not isinstance(expected, list):
        return None, "match.expected 必须是数组"
    flat: list[str] = []
    seen: set[str] = set()

    def _push(value: Any) -> str | None:
        if isinstance(value, dict | list | tuple | bool):
            return f"expected 元素必须是字符串，实际为 {type(value).__name__}: {value!r}"
        text = str(value or "").strip()
        if not text:
            return None
        key = text.casefold()
        if key in seen:
            return None
        seen.add(key)
        flat.append(text)
        return None

    for item in expected:
        if isinstance(item, list | tuple):
            for sub in item:
                error = _push(sub)
                if error:
                    return None, error
        else:
            error = _push(item)
            if error:
                return None, error
    if not flat:
        return None, "match.expected 必须是非空关键词数组"
    return flat, None


def gate_text_keywords(expected: list[str], root_cause: str) -> tuple[bool, str | None]:
    """text 型关键词门禁。

    Returns:
        (traceable, error)：error 非 None 表示结构性违规（应拒收）；
        traceable=False 表示关键词无法溯源至根因原文（降级 needs_review，不拒收）。
    """
    normalized_cause = _normalize_for_trace(root_cause)
    traceable = True
    for keyword in expected:
        text = str(keyword or "").strip()
        if len(text) < MIN_KEYWORD_LEN:
            return False, f"关键词过短（<{MIN_KEYWORD_LEN} 字符）无法承担定位职责: {text!r}"
        if _WEAK_KEYWORD_RE.match(text):
            return False, f"纯数字关键词无区分度（需搭配语境词如 'HCI {text}'）: {text!r}"
        if _normalize_for_trace(text) not in normalized_cause:
            traceable = False
    return traceable, None


def _trace_tier(evidence: str, root_cause: str, content: str) -> str:
    """溯源分层：主锚（root_cause）> 兜底（整篇其余章节）> none（整篇都无法逐字溯源）。

    实测依据（阶段一 n=100）：锁定 root_cause 时确证命中率 96.9%、泛化率 0%；
    放开到整篇后增量信号 52.7% 为噪声（多为告警/步骤的现象，属关键信号地盘）。
    故 root_cause 级为高信任主锚，content 级仅作兑底（强制人工复核）。
    """
    ev = _normalize_for_trace(evidence)
    if not ev:
        return "none"
    if ev in _normalize_for_trace(root_cause):
        return "root_cause"
    if ev in _normalize_for_trace(content):
        return "content"
    return "none"


def normalize_root_signal(
    raw: dict[str, Any],
    *,
    root_cause: str,
    content: str,
    category_id: str,
    prompt_revision: str,
) -> tuple[dict[str, Any] | None, str | None, str]:
    """校验并归一化单条根因信号为 v2 契约；返回 (signal, 违规原因, 溯源层级)。

    tier 取 "root_cause"（主锚）/"content"（兑底）/"none"（无法溯源，拒收）。
    """
    match_type = str(raw.get("match_type") or "")
    if match_type not in VALID_MATCH_TYPES:
        return None, f"match_type 必须是 {sorted(VALID_MATCH_TYPES)}，实际: {match_type!r}", "none"

    signal_text = str(raw.get("signal_text") or "").strip()
    if not signal_text:
        return None, "signal_text（一句话特性结论）不能为空", "none"

    match = raw.get("match")
    if not isinstance(match, dict):
        return None, "match 必须是对象", "none"
    matcher_type = str(match.get("type") or "")
    allowed_types = EXECUTABLE_MATCH_TYPES if match_type == "executable" else TEXT_MATCH_TYPES
    if matcher_type not in allowed_types:
        return None, f"{match_type} 型 match.type 必须是 {sorted(allowed_types)}，实际: {matcher_type!r}", "none"
    expected, error = _flatten_keywords(match.get("expected"))
    if error:
        return None, error, "none"

    provenance = raw.get("provenance") or {}
    if not isinstance(provenance, dict):
        return None, "provenance 必须是对象", "none"
    source_section = str(provenance.get("source_section") or "")
    if source_section not in VALID_SOURCE_SECTIONS:
        return None, f"provenance.source_section 必须是 {sorted(VALID_SOURCE_SECTIONS)} 之一，实际: {source_section!r}", "none"
    evidence = str(provenance.get("evidence") or "").strip()
    if not evidence:
        return None, "provenance.evidence（原文逐字摘录）不能为空", "none"
    try:
        confidence = max(0.0, min(1.0, float(provenance.get("confidence", 0.5))))
    except (TypeError, ValueError):
        return None, f"provenance.confidence 必须是 0~1 数值: {provenance.get('confidence')!r}", "none"

    # 溯源分层：以 evidence 逐字命中为准；整篇都溯源不到即拒收（绝不伪造）
    tier = _trace_tier(evidence, root_cause, content)
    if tier == "none":
        return None, "provenance.evidence 无法逐字溯源到整篇 KBD 任一章节", "none"
    # 主锚信号关键词逐字校 root_cause；兑底信号关键词校整篇 content
    target_text = root_cause if tier == "root_cause" else content

    needs_review = bool(provenance.get("needs_review"))
    acquire: dict[str, Any] | None = None
    if match_type == "executable":
        raw = {**raw, "acquire": coerce_acquire(raw.get("acquire"))}  # 先无损归一再过门禁
        violation = gate_executable(raw)
        if violation:
            return None, violation, "none"
        acquire = {"tool": str(raw["acquire"]["tool"]), "args": raw["acquire"].get("args") or {}}
    else:
        if raw.get("acquire"):
            return None, "text 型不允许携带 acquire（可执行取证请改用 executable 型重新抽取）", "none"
        traceable, error = gate_text_keywords(expected, target_text)
        if error:
            return None, error, "none"
        if not traceable:
            # 关键词无法逐字溯源（LLM 归一化改写）：保留信号但钉上人工复核
            needs_review = True

    # 兑底层（仅靠整篇其余章节支撑）强制人工复核，绝不自动信任
    if tier == "content":
        needs_review = True

    signal: dict[str, Any] = {
        "schema_version": ROOT_SIGNAL_SCHEMA_VERSION,
        "role": "root_cause",
        "match_type": match_type,
        "signal_text": signal_text,
        "acquire": acquire,
        "match": {
            "type": matcher_type,
            "mode": str(match.get("mode") or "all"),
            "expected": expected,
        },
        "distinctiveness": raw.get("distinctiveness") or {"category_conflicts": None, "global_conflicts": None},
        "provenance": {
            # 信号逐字出自的真实章节（v2 不再钉死 root_cause）
            "source_section": source_section,
            # 溯源层级：root_cause=高信任主锚 / content=整篇兑底（已强制 needs_review）
            "trace_tier": tier,
            # 抽取时的整篇内容指纹：回刷后若整篇 content_md 被编辑，指纹不一致即信号陈旧，
            # 消费侧（Agent / 审核 UI）据此判定是否需重新抽取，不得把陈旧信号当现场结论。
            "source_sha256": hashlib.sha256((content or root_cause or "").encode("utf-8")).hexdigest(),
            "source_category_id": category_id,
            "method": ROOT_SIGNAL_METHOD,
            "evidence": evidence,
            "confidence": confidence,
            "needs_review": needs_review,
        },
        "prompt_revision": prompt_revision,
    }
    return signal, None, tier


# ─── 抽取流水线（LLM 调用经回调注入，便于单测与离线试点）─────────────────────

async def extract_root_signal(
    entry: dict[str, Any],
    *,
    prompt_template: str,
    llm_call: Callable[[list[dict[str, str]]], Awaitable[Any]],
    max_attempts: int = 2,
    enforce_closure: bool = False,
    key_signals: Any = None,
    verification_contract: Any = None,
    context_whitelist: tuple[str, ...] = (),
) -> dict[str, Any]:
    """对单篇 KBD 抽取全部可确定根因的信号（v2：不限条数）。

    Args:
        entry: 含 title/category_id/category_path/root_cause/kbd_content（acquirer_catalog 由本模块注入）。
        llm_call: 异步回调，接收 messages 列表，返回已解析 JSON（dict）或抛异常。
        enforce_closure: 开启后对可执行信号跑闭环裁决（§5.4）：硬拒项先回喂重试一轮，仍不可
            闭环则拆为 accepted/rejected（rejected 由调用方落 root_signal_rejected_json）。
            默认 False 保持旧行为（零回归）；key_signals 为本 KBD 关键轨 produces 池（可为空）。

    Returns:
        {"signals": accepted列表, "rejected": 拒绝候选, "error": str|None, "attempts": int,
         "gate_errors": list, 以及 enforce_closure 时的 "closure"/"all_rejected"/"has_context"}
        —— 主锚（root_cause）优先；无主锚时才采用整篇兑底（已强制 needs_review）。
        门禁失败带原因回喂重试；仍无可用信号则 signals=[]（绝不伪造）。
    """
    prompt = build_root_signal_prompt(
        prompt_template,
        {
            "title": entry.get("title") or "",
            "category_id": entry.get("category_id") or "",
            "category_path": entry.get("category_path") or entry.get("category_id") or "",
            "kbd_content": entry.get("kbd_content") or entry.get("root_cause") or "",
            "acquirer_catalog": acquirer_catalog_text(),
        },
    )
    prompt_revision = "sha256:" + hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()
    root_cause = entry.get("root_cause") or ""
    content = entry.get("kbd_content") or root_cause
    category_id = entry.get("category_id") or ""

    feedback = ""
    last_error = ""
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 HCI 根因定位信号抽取专家。只输出可被标准 json.loads 严格解析的 JSON 对象；"
                    "不得使用注释、尾随逗号或 Markdown。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        if feedback:
            messages.append({"role": "user", "content": feedback})
        try:
            payload = await llm_call(messages)
        except Exception as exc:  # LLM 传输/解析失败：计入尝试并带原因重试
            last_error = f"LLM 调用失败: {exc}"
            feedback = f"上一轮调用失败（{last_error}）。请重新生成，只输出合法 JSON 对象。"
            logger.warning(event="root_signal_llm_call_failed", attempt=attempt, error=last_error)
            continue

        raws, error = gate_signals(payload if isinstance(payload, dict) else {"signal": payload})
        gate_errors: list[str] = []
        chosen: list[dict[str, Any]] = []
        if error is None:
            primary: list[dict[str, Any]] = []
            fallback: list[dict[str, Any]] = []
            for raw in raws:
                signal, sig_err, tier = normalize_root_signal(
                    raw, root_cause=root_cause, content=content, category_id=category_id, prompt_revision=prompt_revision
                )
                if sig_err is not None or signal is None:
                    gate_errors.append(sig_err or "未知门禁错误")
                    continue
                if tier == "root_cause":
                    primary.append(signal)
                elif tier == "content":
                    fallback.append(signal)
            # 主锚优先：只要 root_cause 级有信号就只取主锚，不用整篇兑底填充（防噪声稀释）
            chosen = primary if primary else fallback
            if chosen:
                if not enforce_closure:
                    logger.info(
                        event="root_signal_extracted",
                        support_id=entry.get("support_id"),
                        count=len(chosen),
                        tiers=sorted({s["provenance"]["trace_tier"] for s in chosen}),
                        needs_review=sum(1 for s in chosen if s["provenance"]["needs_review"]),
                        attempt=attempt,
                    )
                    return {
                        "signals": chosen,
                        "rejected": [],
                        "error": None,
                        "attempts": attempt,
                        "gate_errors": gate_errors,
                    }
                adjud = adjudicate_root_closure(
                    chosen,
                    key_signals,
                    verification_contract=verification_contract,
                    context_whitelist=context_whitelist,
                )
                # 硬拒项先回喂重试一轮（§11 「回喂重试」）；bootstrap 保留项不触发重试（已被保守 tolerate）
                if adjud["hard_reject_count"] and attempt < max_attempts:
                    feedback = adjud["feedback"]
                    last_error = "根因信号未通过闭环校验"
                    logger.info(
                        event="root_signal_closure_retry",
                        support_id=entry.get("support_id"),
                        attempt=attempt,
                        hard_reject=adjud["hard_reject_count"],
                        has_context=adjud["has_context"],
                    )
                    continue
                logger.info(
                    event="root_signal_extracted",
                    support_id=entry.get("support_id"),
                    count=len(adjud["accepted"]),
                    rejected=len(adjud["rejected"]),
                    tiers=sorted({s["provenance"]["trace_tier"] for s in adjud["accepted"]}),
                    needs_review=sum(1 for s in adjud["accepted"] if s["provenance"]["needs_review"]),
                    attempt=attempt,
                )
                return {
                    "signals": adjud["accepted"],
                    "rejected": adjud["rejected"],
                    "closure": adjud["summary"],
                    "closure_results": adjud["results"],
                    "all_rejected": adjud["all_rejected"],
                    "has_context": adjud["has_context"],
                    "error": None,
                    "attempts": attempt,
                    "gate_errors": gate_errors,
                }
            last_error = gate_errors[0] if gate_errors else "无逐字可溯源的根因信号"
        else:
            last_error = error

        feedback = (
            f"上一轮输出未通过服务端门禁：{last_error}。"
            "请严格遵守输出契约：输出 1~8 条根因信号的 JSON 对象（键 signals 为数组），"
            "每条 provenance.evidence 必须逐字出自整篇 KBD 某章节；"
            "match_type=executable 时 acquire 必须使用采集器目录内的 qfk_* 工具；"
            "match_type=text 时 expected 关键词必须逐字出现在所溯源章节原文中；"
            "只抽能确定根因的信号，不抽处置动作/解决方案/泛化描述。"
        )
        logger.info(event="root_signal_gate_reject", support_id=entry.get("support_id"), attempt=attempt, reason=last_error)

    logger.error(event="root_signal_extraction_failed", support_id=entry.get("support_id"), reason=last_error)
    return {"signals": [], "rejected": [], "error": last_error, "attempts": attempts, "gate_errors": []}


# ─── 独特性（定位冲突计数）────────────────────────────────────────────────────

def _signal_match_tokens(signal: dict[str, Any]) -> list[str]:
    """提取信号的判定词面：expected ＋ executable 的采集具体资源（路径/设备/服务/端口）。

    对抗性审查（v3）：executable 的 expected 是现场输出判定串（如 "100%"），
    若只拿 expected 打根因语料，不同目录的 df 案例会被通用判定串误判冲突；
    真实判定面是「同一资源对象 + 同一判定串」同时命中，故采集参数必须并入。
    占位符（含 {{）与纯选项（-h 类）不具定位性，排除。
    """
    tokens = [str(k) for k in (signal.get("match") or {}).get("expected") or []]
    if signal.get("match_type") != "executable":
        return tokens
    args = (signal.get("acquire") or {}).get("args") or {}
    for value in args.get("command_args") or []:
        if isinstance(value, str) and len(value) >= 4 and "{{" not in value and not value.startswith("-"):
            tokens.append(value)
    for key in ("file", "service", "port", "vm_name", "device"):
        value = args.get(key)
        if value is not None and "{{" not in str(value) and len(str(value)) >= 4:
            tokens.append(str(value))
    return tokens


def signal_hits_root_cause(signal: dict[str, Any], root_cause_text: str) -> bool:
    """判定根因信号的词面是否全部命中某篇 KBD 的根因原文（包含匹配近似现场判定）。"""
    normalized = _normalize_for_trace(root_cause_text)
    keywords = [_normalize_for_trace(token) for token in _signal_match_tokens(signal)]
    return bool(keywords) and all(k in normalized for k in keywords)


def count_root_cause_conflicts(
    signal: dict[str, Any],
    corpus: dict[str, tuple[str, str]],
    *,
    self_support_id: str | None = None,
) -> dict[str, int]:
    """统计信号在语料中的冲突度（理想为 0：唯一定位）。

    Args:
        corpus: {support_id: (root_cause_text, category_id)}
    """
    return ConflictCorpus(
        [(support_id, root_cause_text, category_id) for support_id, (root_cause_text, category_id) in corpus.items()]
    ).conflicts(signal, self_support_id=self_support_id)


class ConflictCorpus:
    """冲突度语料：把「语料归一化」从每篇 O(N) 摊销为一次构建。

    全库回刷（~2700 篇）时，逐篇重建归一化语料会退化成 O(N²) 次正则调用；
    本类一次归一化建索引，之后每篇抽取只做 N 次子串判定。同一实例可在线程内安全复用（纯读）。
    """

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        # rows: [(support_id, root_cause_text, category_id)]
        self._rows = [
            (str(support_id), _normalize_for_trace(root_cause_text or ""), str(category_id or ""))
            for support_id, root_cause_text, category_id in rows
        ]

    def __len__(self) -> int:
        return len(self._rows)

    def conflicts(
        self, signal: dict[str, Any], *, self_support_id: str | None = None
    ) -> dict[str, int]:
        tokens = [_normalize_for_trace(token) for token in _signal_match_tokens(signal)]
        if not tokens:
            return {"category_conflicts": 0, "global_conflicts": 0}
        own_category = (signal.get("provenance") or {}).get("source_category_id") or ""
        category_conflicts = 0
        global_conflicts = 0
        for support_id, normalized_cause, category_id in self._rows:
            if self_support_id is not None and support_id == self_support_id:
                continue
            if all(token in normalized_cause for token in tokens):
                global_conflicts += 1
                if own_category and category_id == own_category:
                    category_conflicts += 1
        return {"category_conflicts": category_conflicts, "global_conflicts": global_conflicts}


# ─── Prompt 种子离线加载（试点用；线上路径走 StrictPromptLoader 热加载）───────

def load_prompt_template_from_sql(
    sql_text: str, prompt_name: str = ROOT_SIGNAL_PROMPT_NAME
) -> str:
    """从种子 SQL 解析指定 Prompt 的 $TEMPLATE$ 模板块，并按 StrictPromptLoader 同法校验占位符契约。

    种子事实源是 `database/seeds/02_system_prompts.sql`（由 db-seed-job 在部署后自动应用，
    ON CONFLICT DO NOTHING 不覆盖 admin-ui 自定义）；该文件含多个 $TEMPLATE$ 块，故先按
    Prompt 名定位，再取其后的首个模板块，避免误取别的 Prompt。
    """
    start = 0
    if prompt_name:
        at = sql_text.find(f"'{prompt_name}'")
        if at < 0:
            raise ValueError(f"种子 SQL 中未找到 Prompt: {prompt_name}")
        start = at
    match = re.search(r"\$TEMPLATE\$(.*?)\$TEMPLATE\$", sql_text[start:], re.DOTALL)
    if not match:
        raise ValueError(f"种子 SQL 中未找到 {prompt_name or ''} 的 $TEMPLATE$ 模板块")
    template = match.group(1)
    actual = {name for _, name, _, _ in Formatter().parse(template) if name is not None}
    expected = set(ROOT_SIGNAL_PLACEHOLDERS)
    if actual != expected:
        raise ValueError(f"Prompt 占位符契约不符: 期望 {sorted(expected)}，实际 {sorted(actual)}")
    return template
