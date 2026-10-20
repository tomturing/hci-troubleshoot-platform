"""按 S0 已确认分类返回完整的已发布知识清单。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.dynamic_resource.adapters import sop_resource_payload
from shared.dynamic_resource.loader import DynamicResourceLoader, snapshot_revision_metadata
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.observability.logger import get_logger
from shared.observability.metrics import (
    KBD_CONTRACT_HARD_BREAK_TOTAL,
    KBD_CONTRACT_SOFT_STALE_TOTAL,
)
from shared.observability.otel import get_current_trace_id
from shared.schemas.semantic_entry import capability_of
from shared.schemas.signal_generation import current_tool_contract_revision
from shared.schemas.signal_schema import validate_publishable_signals_json
from sqlalchemy import select

from app.models.kbd_entry import KbdEntry
from app.models.sop_document import SopDocument

logger = get_logger("kb-service-playbooks")
router = APIRouter(prefix="/api/kb/v2", tags=["playbooks"])

_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    global _db_manager
    _db_manager = db


def _signals(entry: KbdEntry) -> list[dict[str, Any]]:
    raw = entry.signals_json or []
    if isinstance(raw, dict):
        raw = raw.get("signals", [])
    signals = [signal for signal in raw if isinstance(signal, dict)] if isinstance(raw, list) else []
    # 轨道一：可执行根因信号以 track="root" 进入同一计划，但归约时按 track 各算一份，
    # 与关键信号（track=process）互不影响（见 shared/cdd/fusion.py）。
    # 本函数仅在非语义 live 路径被采用（语义/frozen 路径在调用点用冻结快照覆盖 signals，
    # 保持其可复现契约）；text 根因绝不进此列表（仅作诊断报告展示锚点）。
    for root in _root_signals(entry):
        if str(root.get("match_type") or "") != "executable":
            continue
        # 融合准入闸门：只有经「最少充分集」门禁（gate_version>=2 且 gate_status=passed）产出的
        # 信号才参与判定。存量信号未经充分性校验（实测含恒真判据），在 OR 融合下会变成万能匹配；
        # 疑似恒真的 warning 信号同样只留库供审核，待人工改写为偏离态后再重抽生效。
        if _root_gate_version(root) < ROOT_SIGNAL_RUNTIME_GATE_VERSION:
            continue
        if str((root.get("provenance") or {}).get("gate_status") or "") != "passed":
            continue
        normalized = normalize_root_for_runtime(root)
        if normalized is not None:
            signals.append(normalized)
    return signals


# 根因信号参与运行时判定所需的最低门禁版本。抽取侧写入 provenance.gate_version；
# 缺失或低于该值视为 legacy，不参与采集与判定，重抽后自动升级。
ROOT_SIGNAL_RUNTIME_GATE_VERSION = 2


def _root_gate_version(root: dict[str, Any]) -> int:
    provenance = root.get("provenance") or {}
    try:
        return int(provenance.get("gate_version") or 0)
    except (TypeError, ValueError):
        return 0


# 根因 executable 信号并入 CDD 时统一使用的「整段标准输出」取数契约：与关键信号
# keyword/regex 样本一致（rows=all·cardinality=all 覆盖 stdout 全文），从而复用运行时
# evaluate_matcher 的确定性求值。
_ROOT_WHOLE_STDOUT_EXTRACT: dict[str, Any] = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}

# 根因 match.mode（抽取轨方言 all/any）→ 运行时 matcher.mode（求值轨方言 and/or/not）。
# 若原样透传 all/any，evaluate_matcher 的 _normalize_mode 会落到 else→or 分支，令「全部命中」
# 被误读为「任一命中」。故必须显式翻译。
_ROOT_MODE_MAP = {"all": "and", "and": "and", "any": "or", "or": "or", "not": "not"}


def _root_signals(entry: KbdEntry) -> list[dict[str, Any]]:
    """读取 root_signal_json（v2 顶层数组，兼容 {signals:[...]} 包裹）为 dict 列表。

    用 getattr 防御：既有调用方/测试可能传入无该字段的轻量对象（如 SimpleNamespace），
    缺字段视为「无根因信号」而非抛异常，保持与关键信号读取同级的宽容度。
    """
    raw = getattr(entry, "root_signal_json", None) or []
    if isinstance(raw, dict):
        raw = raw.get("signals", [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _root_signal_runtime_id(root: dict[str, Any]) -> str:
    """为根因信号派生稳定运行时 id：优先自带 id，否则以 signal_text 指纹派生（幂等、跨请求稳定）。"""
    base = str(root.get("id") or root.get("signal_id") or "").strip()
    if base:
        return base
    digest = hashlib.sha256(str(root.get("signal_text") or "").encode("utf-8")).hexdigest()[:12]
    return f"root_{digest}"


def _root_match_to_cdd(match: dict[str, Any]) -> dict[str, Any] | None:
    """把根因简单 match（{type, mode, expected:[关键词]}）转成运行时 CDD Matcher 契约。

    根因抽取轨的 match 只存关键词列表，而运行时 evaluate_matcher 需要 pattern/expected 布尔/
    extract 的完整契约，二者不可直接透传。转换仅覆盖可无损还原的 keyword/regex：
    - keyword 单词或 any 语义：keyword matcher（逐行任一/同现即可表达）。
    - keyword 多词 all（要求全部关键词出现、允许跨行）：逐行 records 的 and 只认同行同现，
      无法表达跨行；改用 lookahead 正则跑整段输出（正则分支以 predicate_text 全文 + re.DOTALL 求值，
      天然跨行），忠实复现根因「all」语义。
    - state/boolean/threshold/exists：根因简单形不携带 value/operator 等数值契约，无法无损还原，
      保守返回 None 丢弃（试点实测 executable 根因 100% 为 keyword，此类出现率 0%，不稀释判定）。
    """
    mtype = str(match.get("type") or "")
    raw_expected = match.get("expected")
    if isinstance(raw_expected, list):
        tokens = [str(item).strip() for item in raw_expected if str(item).strip()]
    elif raw_expected in (None, ""):
        tokens = []
    else:
        tokens = [str(raw_expected).strip()]
    if not tokens:
        return None
    mode = str(match.get("mode") or "all").lower()
    if mtype == "keyword":
        if len(tokens) >= 2 and mode in {"all", "and"}:
            pattern = "".join(f"(?=.*{re.escape(token)})" for token in tokens)
            return {"type": "regex", "pattern": pattern, "expected": True, "extract": _ROOT_WHOLE_STDOUT_EXTRACT}
        return {
            "type": "keyword",
            "pattern": tokens,
            "mode": _ROOT_MODE_MAP.get(mode, "or"),
            "expected": True,
            "extract": _ROOT_WHOLE_STDOUT_EXTRACT,
        }
    if mtype == "regex":
        return {"type": "regex", "pattern": tokens[0], "expected": True, "extract": _ROOT_WHOLE_STDOUT_EXTRACT}
    return None


def normalize_root_for_runtime(root: dict[str, Any]) -> dict[str, Any] | None:
    """把单条 executable 根因信号归一为可并入关键信号 CDD 计划的运行时信号 dict。

    透传 acquire（tool/args），match 经 _root_match_to_cdd 转成运行时契约，补 provenance.category=backend
    使 _signal_category 判为消费者（qfk_* 本就推导 backend，此处显式钉牢）；派生稳定 id 满足运行
    时 signal_id 门禁。多余键（role/provenance 细节/distinctiveness/prompt_revision）由
    KBDStep(extra=ignore)/compile_signal_plan 忽略。无法忠实转换（非 keyword/regex）返回 None 丢弃。
    """
    acquire = root.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    if not tool:
        return None
    cdd_match = _root_match_to_cdd(root.get("match") or {})
    if cdd_match is None:
        return None
    return {
        "id": _root_signal_runtime_id(root),
        "acquire": {"tool": tool, "args": acquire.get("args") or {}},
        "match": cdd_match,
        "provenance": {"category": "backend"},
        # 证据路径标记：归约时与 process 路分开计算，晚融合互不影响。
        "track": "root",
    }


def _root_display_anchors(entry: KbdEntry) -> list[dict[str, Any]]:
    """轨道二：text 根因信号作为诊断报告「人工确认锚点」的展示载荷（绝不进 signals 列表/不入门禁）。"""
    anchors: list[dict[str, Any]] = []
    for root in _root_signals(entry):
        if str(root.get("match_type") or "") != "text":
            continue
        match = root.get("match") or {}
        expected = match.get("expected")
        if isinstance(expected, list):
            keywords = [str(item) for item in expected if str(item).strip()]
        elif expected in (None, ""):
            keywords = []
        else:
            keywords = [str(expected)]
        anchors.append(
            {
                "signal_text": str(root.get("signal_text") or ""),
                "keywords": keywords,
                "match_type": str(match.get("type") or ""),
            }
        )
    return anchors


def _execution_issues(
    signals: list[dict[str, Any]],
    document: dict[str, Any] | None = None,
    support_id: str | None = None,
    category_id: str | None = None,
) -> list[str]:
    """返回自动执行硬门禁问题；知识条目本身不会因此从清单消失。

    设计原则（校验器驱动，见 ADR 2026-08-13）：
    - validate_publishable_signals_json 是唯一真相源，只调用一次，结果全程复用。
    - tool_contract_revision 字节哈希作为粗粒度变化探测（是否有任何 schema 变动）。
    - schema 变了且校验通过 → 兼容演进（soft_stale 观测，不阻断）；
      schema 变了且校验失败 → 破坏性变更（hard_break 阻断，issues 里已有具体原因）。
    """
    issues: list[str] = []
    validation_document = document or {"schema_version": 2, "signals": signals}
    # 轨道一（#2）：signals 已是「关键信号 ∪ 可执行根因信号」的运行时并集，能力判定与契约
    # 校验必须以并集为准。否则 signals_json 为空、仅由 executable 根因支撑的 KBD 会被
    # is_reference_only 误读为「干净空文档 → reference_only」而永不进入 CDD（违背轨道一）。
    # 仅在并集非空时覆盖 document["signals"]，保留「既无关键也无根因」空文档的 reference_only 语义；
    # 归一后的 keyword/regex 根因信号已实测通过 validate_publishable_signals_json。
    if isinstance(validation_document, dict) and signals and validation_document.get("signals") != signals:
        # track 是运行时证据路径标记（process/root），不属发布契约：signal schema 是
        # additionalProperties=false，若直接拿去校验会把合法运行时计划误判为破坏性变更。
        # 在进入校验器前确定性剥离，与 normalize_optional_matcher_nulls 同一类边界归一。
        validation_document = {
            **validation_document,
            "signals": [
                {key: value for key, value in signal.items() if key != "track"}
                if isinstance(signal, dict)
                else signal
                for signal in signals
            ],
        }
    semantic_capability = capability_of(validation_document)
    if semantic_capability == "reference_only":
        return ["仅案例推荐：无现场验证信号，不进入自动执行 CDD"]
    if semantic_capability == "guidance_only":
        return ["语义入口为 guidance_only：仅可请求人工补充证据，不进入自动执行 CDD"]
    if semantic_capability == "capability_gap":
        return ["语义入口为 capability_gap：当前平台缺少可执行消费者，必须人工升级处理"]

    # ── Layer 1：唯一真相源，一次校验，结果向下传递 ──────────────────────────────
    schema_valid = True
    schema_issue: str | None = None
    try:
        validate_publishable_signals_json(validation_document)
    except Exception as exc:
        schema_valid = False
        # jsonschema.ValidationError 的 str(exc) 会附带整段 Schema 与实例，既不利于
        # API 消费，也可能产生数 KB 的重复错误；message 才是可操作的门禁原因。
        schema_issue = str(getattr(exc, "message", exc))

    # ── signal 级别诊断（signal_id / acquire.tool / QFK 模式互斥）────────────────
    seen_ids: set[str] = set()
    for index, signal in enumerate(signals, start=1):
        signal_id = str(signal.get("id") or signal.get("signal_id") or "").strip()
        if not signal_id:
            issues.append(f"signal[{index}] 缺少 signal_id")
        elif signal_id in seen_ids:
            issues.append(f"signal_id 重复: {signal_id}")
        else:
            seen_ids.add(signal_id)
        acquire = signal.get("acquire") or {}
        if not acquire.get("tool"):
            issues.append(f"{signal_id or f'signal[{index}]'} 缺少 acquire.tool")
        tool = str(acquire.get("tool") or "")
        matcher = signal.get("match")
        produces = (signal.get("orchestrate") or {}).get("produces") or []
        has_match = isinstance(matcher, dict)
        has_produces = any(
            isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip() for item in produces
        )
        if tool != "qkv_case_context" and tool.startswith("qkv_") and (has_match or not has_produces):
            issues.append(f"{signal_id or f'signal[{index}]'} 的 QKV 必须配置有效产出变量且 match 为 null")
        if tool.startswith("qfk_") and has_match == has_produces:
            # QFK v2 有两种互斥且都可自动执行的模式：
            # 1. match：对命令结果做确定性判定；
            # 2. orchestrate.produces：命令成功并完成受控提取后写入变量池。
            # 不能再把 match=None 一律判成不可执行，否则合法的产出变量链会在
            # 分类快照阶段被提前过滤，永远无法进入实际执行器。
            issues.append(f"{signal_id or f'signal[{index}]'} 必须且只能配置确定性 matcher 或有效产出变量")

    # match/produces 互斥已经由上面的带 signal_id 诊断表达，不重复返回 Schema
    # 的 signals[index] 版本；其他 Schema 问题（字段缺失、类型错误、非法枚举等）
    # 仍作为第一条硬门禁问题返回。
    if schema_issue and not (
        "“关键字判定(match)”或“产出变量(orchestrate.produces)”之一" in schema_issue
        or "是产出变量信号，必须配置 orchestrate.produces 且 match 必须为 null" in schema_issue
    ):
        issues.insert(0, f"signals_json 契约校验失败: {schema_issue}")

    # ── Layer 2 + 3：变化感知 + 可观测性分发（复用顶层校验结果，不重复调用校验器）──
    publish_validation = validation_document.get("publish_validation") or {}
    generation = validation_document.get("generation_metadata") or {}
    if isinstance(publish_validation, dict) and publish_validation:
        if publish_validation.get("status") != "passed":
            issues.append("专家发布校验状态无效，必须重新发布")
        else:
            stored_rev = publish_validation.get("tool_contract_revision")
            cur_rev = current_tool_contract_revision()
            if stored_rev != cur_rev:
                # schema 有变动：依据顶层校验结果（单一真相源）分发可观测性指标。
                # 不重复调用校验器——顶层已跑，schema_valid 即为最终判定。
                if not schema_valid:
                    # 版本变了且旧信号无法通过新 schema 校验 → 破坏性变更。
                    # issues 里顶层已有具体的校验失败原因，此处仅埋点不重复报错。
                    KBD_CONTRACT_HARD_BREAK_TOTAL.labels(
                        support_id=support_id or "unknown", category=category_id or "unknown"
                    ).inc()
                elif not issues:
                    # 版本变了但旧信号仍合法且无其他问题 → 纯兼容漂移（如新增可选属性）。
                    # 仅在 KBD 无任何其他阻断问题时计数，保证指标语义精确。
                    KBD_CONTRACT_SOFT_STALE_TOTAL.labels(
                        support_id=support_id or "unknown", category=category_id or "unknown"
                    ).inc()
    # 发布章只证明发布当时通过门禁；正文或证据后来变更时，generation_metadata 会
    # 显式标记 stale。该标记必须独立阻断，否则分类快照会把 KBD 宣告为 executable，
    # 而 CDD 编译器又在运行时拒绝它，导致强生产者完整性计算出现虚假 source_unavailable。
    if isinstance(generation, dict) and generation and generation.get("status") == "stale":
        issues.append("Signal/Contract 生成输入已变化，必须重新抽取或完成人工复核")
    # 仅在没有专家发布章时才用生成时的工具契约版本作兼容路径门禁；已发布 KBD
    # 的兼容漂移由上方 publish_validation 统一处理，不能因为旧生成指纹重复阻断。
    if (
        not (isinstance(publish_validation, dict) and publish_validation)
        and isinstance(generation, dict)
        and generation
        and generation.get("tool_contract_revision") != current_tool_contract_revision()
    ):
        issues.append("Signal/Contract 使用的工具契约版本已过期，必须重新编译")
    if not signals:
        issues.append("未配置关键信号")
    return issues


@router.get("/categories/{category_id}/playbooks")
async def get_category_playbooks(
    category_id: str,
    taxonomy_version: str | None = Query(None, max_length=64),
) -> dict[str, Any]:
    """返回分类内全部已发布 SOP/KBD，不接受 query、top_k 或检索门禁。"""
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务依赖未初始化")

    async with _db_manager.async_session_factory() as session:
        sop_rows = await session.execute(
            select(SopDocument)
            .where(SopDocument.category_id == category_id, SopDocument.status == "published")
            .order_by(SopDocument.id)
        )
        kbd_rows = await session.execute(
            select(KbdEntry)
            .where(KbdEntry.category_id == category_id, KbdEntry.status == "published")
            .order_by(KbdEntry.id)
        )
        sops = list(sop_rows.scalars().all())
        kbds = list(kbd_rows.scalars().all())

        trace_id = get_current_trace_id()
        serialized_sops: list[dict[str, Any]] = []
        serialized_kbds: list[dict[str, Any]] = []
        revision_keys: list[str] = []

        for sop in sops:
            snapshot = await DynamicResourcePublisher(session).ensure_published(
                **sop_resource_payload(sop), trace_id=trace_id
            )
            revision = snapshot_revision_metadata(snapshot)
            revision_keys.append(f"sop:{sop.id}:{revision.get('revision', 0)}")
            serialized_sops.append(
                {
                    "id": sop.id,
                    "title": sop.title,
                    "category_id": sop.category_id,
                    "content_md": sop.content_md,
                    "tree_json": sop.tree_json,
                    "variable_schema": sop.variable_schema or [],
                    "status": sop.status,
                    "resource_revision": revision,
                }
            )

        for kbd in kbds:
            snapshot = await DynamicResourceLoader(session).get_active("kbd", str(kbd.id))
            frozen = getattr(snapshot, "content", {})
            signals = _signals(kbd)
            raw_signal_doc = kbd.signals_json if isinstance(kbd.signals_json, dict) else {}
            # 语义候选执行与检索必须读取同一份发布修订，不能用工作稿配旧摘要。
            frozen_document = frozen.get("signals_json") if isinstance(frozen.get("signals_json"), dict) else {}
            is_semantic = bool(
                raw_signal_doc.get("semantic_entry_profile") or frozen_document.get("semantic_entry_profile")
                or ("signals_json" in frozen and capability_of(frozen_document) == "reference_only")
                or capability_of(raw_signal_doc) == "reference_only"
            )
            if is_semantic:
                if getattr(snapshot, "status", "") != "published" or frozen.get("category_id") != category_id:
                    continue
                raw_signal_doc = frozen.get("signals_json") or {"schema_version": 2, "signals": []}
                signals = raw_signal_doc.get("signals") or []
            verification_contract = raw_signal_doc.get("verification_contract") or {}
            generation_metadata = raw_signal_doc.get("generation_metadata") or {}
            publish_validation = raw_signal_doc.get("publish_validation") or {}
            semantic_profile = raw_signal_doc.get("semantic_entry_profile") or {}
            # 轨道一：能力分类（diagnosis_capability/recommendable）与执行门禁必须以「关键 ∪ 可执行根因」
            # 并集为准，使仅 executable 根因支撑的 KBD 不被空原始 signals_json 误判 reference_only。
            # 语义/frozen 路径的 signals 已来自冻结快照（不并入 live 根因），覆盖为同值无害。
            capability_document = (
                {**raw_signal_doc, "signals": signals} if isinstance(raw_signal_doc, dict) and signals else raw_signal_doc
            )
            issues = _execution_issues(
                signals,
                capability_document,
                support_id=kbd.support_id,
                category_id=kbd.category_id,
            )
            revision = snapshot_revision_metadata(snapshot)
            revision_keys.append(f"kbd:{kbd.id}:{revision.get('revision', 0)}")
            serialized_kbds.append(
                {
                    "id": str(kbd.id),
                    "kbd_id": kbd.id,
                    "support_id": kbd.support_id,
                    "name": frozen.get("title", kbd.title) if is_semantic else kbd.title,
                    "title": frozen.get("title", kbd.title) if is_semantic else kbd.title,
                    "category_id": kbd.category_id,
                    "status": kbd.status,
                    "executable": not issues,
                    "execution_issues": issues,
                    "signals": signals,
                    # 轨道二：text 根因仅作为诊断报告「人工确认锚点」展示，独立于 signals/门禁/召回。
                    "root_display_anchors": _root_display_anchors(kbd),
                    "verification_contract": verification_contract,
                    "generation_metadata": generation_metadata,
                    "publish_validation": publish_validation,
                    "semantic_entry_profile": semantic_profile,
                    "diagnosis_capability": capability_of(capability_document) or "executable",
                    "recommendable": capability_of(capability_document) in {"reference_only", "guidance_only"} or not issues,
                    "root_cause": frozen.get("root_cause") if is_semantic else kbd.root_cause,
                    "solution": frozen.get("solution") if is_semantic else kbd.solution,
                    "problem_description": frozen.get("problem_description")
                    if is_semantic
                    else kbd.problem_description,
                    "resource_revision": revision,
                }
            )

        await session.commit()

    snapshot_material = json.dumps(revision_keys, ensure_ascii=False, separators=(",", ":"))
    snapshot_id = "kbsnap-" + hashlib.sha256(snapshot_material.encode()).hexdigest()[:16]
    logger.info(
        event="category_playbooks_loaded",
        category_id=category_id,
        snapshot_id=snapshot_id,
        sop_count=len(serialized_sops),
        kbd_count=len(serialized_kbds),
        trace_id=trace_id,
    )
    return {
        "category": {"id": category_id, "taxonomy_version": taxonomy_version},
        "snapshot_id": snapshot_id,
        "sops": serialized_sops,
        "kbds": serialized_kbds,
    }
