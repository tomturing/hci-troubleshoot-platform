"""KB Service - 根因信号抽取路由（Root-Signal Extraction）

POST /api/admin/kbd/{kbd_id}/extract-root-signal
  - 从整篇 KBD 抽取「全部可确定根因的」结论侧定位信号（不限条数），写入 kbd_entry.root_signal_json（v2 顶层数组）
  - 与关键信号抽取（/kbd/{kbd_id}/extract-signals → signals_json）完全解耦：
    不同 Prompt、不同落库列、不同门禁、不进入 CDD/发布盖章与 kbd_revision payload
  - 异步模式（默认，与 extract-signals / VISION 对齐）：立即 202 + job_id，轮询 status
  - ?sync=true 同步返回完整结果（供 admin-ui 单条按钮与联调）

POST /api/admin/kbd/batch/extract-root-signal（见 routes/admin.py）
  - 全库回刷走批量任务（kbd_batch_job 持久化进度），本模块只提供单篇执行函数

Prompt 管理与关键信号完全对齐：
  - 种子：database/seeds/02_system_prompts.sql（stage='KEY'，ON CONFLICT DO NOTHING）
  - 加载：每次抽取经 StrictPromptLoader 热加载 + 占位符契约强校验
  - 编辑：admin-ui「Prompt 管理 → KEY」页在线改，下一次抽取即生效（无需重启/改代码）

可观测性（与关键信号同一套范式）：
  - 指标：KBD_LLM_REQUESTS_TOTAL / KBD_LLM_TOKENS_TOTAL（operation=extract_root_signal）
  - 日志：event=* 结构化日志 + 唯一 trace_id 贯穿「请求 → LLM → 门禁 → 落库」
  - 链路：Langfuse observe_llm_generation（prompt_revision 作为 metadata 可复现）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from shared.observability.langfuse import observe_llm_generation, update_observation
from shared.observability.logger import get_logger
from shared.observability.metrics import (
    KBD_LLM_REQUESTS_TOTAL,
    KBD_LLM_TOKENS_TOTAL,
    ROOT_SIGNAL_CLOSURE_TOTAL,
    ROOT_SIGNAL_PRUNED_TOTAL,
    ROOT_SIGNAL_REJECTED_TOTAL,
)
from shared.observability.otel import get_current_trace_id
from shared.utils.prompt_loader import StrictPromptLoader
from sqlalchemy import select, text

from app.models.kb_category import KbCategory
from app.models.kbd_entry import KbdEntry
from app.routes.extract_signals import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLE_THINKING,
    _decode_llm_json_object,
)
from app.routes.extract_signals import (
    LLM_MODEL as SIGNALS_LLM_MODEL,
)
from app.services.llm_runtime import call_with_llm_governance
from app.services.root_signal_closure import adjudicate_root_closure
from app.services.root_signal_extractor import (
    ROOT_SIGNAL_PLACEHOLDERS,
    ROOT_SIGNAL_PROMPT_NAME,
    ConflictCorpus,
    extract_root_signal,
    gate_minimality,
    gate_signals,
    normalize_root_signal,
)
from app.services.signal_job_manager import SignalJobManager

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-extract-root-signal")
router = APIRouter(prefix="/api/admin", tags=["extract-root-signal"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None

# 根因信号模型：允许独立于关键信号选型（默认沿用 EXTRACT_SIGNALS 模型链）
LLM_MODEL = (os.environ.get("ROOT_SIGNAL_MODEL") or "").strip() or SIGNALS_LLM_MODEL
_ROOT_SIGNAL_BASE_URL = (os.environ.get("ROOT_SIGNAL_BASE_URL") or os.environ.get("EXTRACT_SIGNALS_BASE_URL") or LLM_BASE_URL).rstrip("/")
_ROOT_SIGNAL_API_KEY = (
    os.environ.get("ROOT_SIGNAL_API_KEY") or os.environ.get("EXTRACT_SIGNALS_API_KEY") or LLM_API_KEY
)
# v2 多信号（最多 8 条）输出体量显著大于 v1 单信号，预算上调避免长输出被截断（finish_reason=length 即解析失败）
ROOT_SIGNAL_MAX_TOKENS = max(1024, int(os.environ.get("ROOT_SIGNAL_MAX_TOKENS", "8192")))
# 网关代理超时 240s、Governor 默认 3 次重试：单次超时必须先于网关超时落地
_ROOT_SIGNAL_LLM_TIMEOUT = float(os.environ.get("ROOT_SIGNAL_LLM_TIMEOUT") or "60")
# v2 取消 MIN_ROOT_CAUSE_LEN 硬门：抽取输入面已放开为整篇 KBD，根因短≠无确证信号（是否可抽由门禁与实测裁决）
# 冲突度语料缓存 TTL：全库回刷期间避免每篇重查 ~2700 行（TTL 到期后自动反映最新根因）
_CORPUS_TTL_SECONDS = float(os.environ.get("ROOT_SIGNAL_CORPUS_TTL", "300"))

CONSUMER = "kb-service.extract_root_signal.kbd"

# 根因信号专用 Job 管理器（独立实例）：
# SignalJobManager 的幂等去重按 kbd_id 匹配 pending/running 任务，若与关键信号共用单例，
# 同一篇 KBD 并发触发两种任务时会拿到「另一个任务」的 job_id 和结果，因此必须物理隔离。
_root_signal_jobs = SignalJobManager(max_concurrent=4)


def set_dependencies(db_manager: DatabaseManager) -> None:
    global _db_manager
    _db_manager = db_manager


def get_root_signal_job_manager() -> SignalJobManager:
    return _root_signal_jobs


# ─── 冲突度语料（进程内 TTL 缓存）─────────────────────────────────────────────
_corpus_cache: tuple[float, ConflictCorpus] | None = None
_corpus_lock = asyncio.Lock()


async def get_conflict_corpus(db_manager: DatabaseManager, *, force: bool = False) -> ConflictCorpus:
    """加载全库根因语料（draft+published），用于计算「一信号定位一 KBD」的冲突度。

    archived/rejected/unpublishable 不参与线上定位，不计入冲突，避免把死条目当竞争对手。
    """
    global _corpus_cache
    now = time.monotonic()
    async with _corpus_lock:
        if not force and _corpus_cache is not None and now - _corpus_cache[0] < _CORPUS_TTL_SECONDS:
            return _corpus_cache[1]
        async with db_manager.async_session_factory() as session:
            result = await session.execute(
                text("""
                    SELECT support_id, root_cause, COALESCE(category_id, ai_category_id, '') AS category_id
                    FROM kbd_entry
                    WHERE status IN ('draft', 'published') AND root_cause <> ''
                """)
            )
            rows = [(row[0], row[1], row[2]) for row in result.all()]
        corpus = ConflictCorpus(rows)
        _corpus_cache = (now, corpus)
        logger.info(event="root_signal_corpus_loaded", size=len(corpus))
        return corpus


# ─── LLM 调用（消息列表驱动：门禁反馈重试在 root_signal_extractor 内完成）────
async def _call_llm_messages(
    messages: list[dict[str, str]],
    *,
    prompt_revision: str,
    kbd_id: int,
    attempt: int,
) -> dict[str, Any]:
    """单次 LLM 调用（json_object 模式）+ 治理舱壁 + 指标 + Langfuse 观测。"""
    from openai import AsyncOpenAI

    if not _ROOT_SIGNAL_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY 未配置，无法抽取根因信号")

    client = AsyncOpenAI(
        api_key=_ROOT_SIGNAL_API_KEY,
        base_url=_ROOT_SIGNAL_BASE_URL,
        timeout=_ROOT_SIGNAL_LLM_TIMEOUT,
        # 重试由共享治理器负责，避免 SDK/路由双重重试
        max_retries=0,
    )

    async def _call():
        return await client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=ROOT_SIGNAL_MAX_TOKENS,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled" if not LLM_ENABLE_THINKING else "enabled"}},
        )

    try:
        with observe_llm_generation(
            operation="extract_root_signal",
            model=LLM_MODEL,
            input={"messages": messages},
            metadata={
                "prompt_name": ROOT_SIGNAL_PROMPT_NAME,
                "prompt_revision": prompt_revision,
                "kbd_id": kbd_id,
                "gate_attempt": attempt,
            },
            model_parameters={
                "temperature": 0.0,
                "max_tokens": ROOT_SIGNAL_MAX_TOKENS,
                "enable_thinking": LLM_ENABLE_THINKING,
                "response_format": "json_object",
            },
        ) as observation:
            response = await call_with_llm_governance("extract_root_signal", _call)
            content = (response.choices[0].message.content or "").strip()
            finish_reason = str(getattr(response.choices[0], "finish_reason", None) or "unknown")
            usage = getattr(response, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
            truncated = finish_reason == "length"
            parsed: dict[str, Any] | None = None
            parse_error: Exception | None = None
            if content and not truncated:
                try:
                    parsed = _decode_llm_json_object(content)
                except (json.JSONDecodeError, ValueError) as exc:
                    parse_error = exc
            KBD_LLM_REQUESTS_TOTAL.labels(
                operation="extract_root_signal",
                model=LLM_MODEL,
                status="success" if parsed is not None else "error",
                finish_reason=finish_reason,
            ).inc()
            for token_type, value in (("input", prompt_tokens), ("output", completion_tokens), ("total", total_tokens)):
                if value:
                    KBD_LLM_TOKENS_TOTAL.labels(
                        operation="extract_root_signal", model=LLM_MODEL, type=token_type
                    ).inc(value)
            update_observation(
                observation,
                output={"content": content},
                metadata={"finish_reason": finish_reason, "response_chars": len(content), "gate_attempt": attempt},
                usage_details={"input": prompt_tokens, "output": completion_tokens, "total": total_tokens},
                level="ERROR" if parsed is None else None,
                status_message=(
                    "根因信号输出被截断（finish_reason=length）"
                    if truncated
                    else ("根因信号响应为空" if not content else (str(parse_error) if parse_error else None))
                ),
            )
            logger.info(
                event="root_signal_llm_response",
                trace_id=get_current_trace_id(),
                kbd_id=kbd_id,
                model=LLM_MODEL,
                prompt_revision=prompt_revision,
                gate_attempt=attempt,
                finish_reason=finish_reason,
                response_chars=len(content),
                response_sha256=hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest(),
                parsed=parsed is not None,
                total_tokens=total_tokens,
            )
            if parsed is None:
                raise ValueError(
                    f"根因信号 LLM 响应不是合法 JSON 对象（finish_reason={finish_reason}, chars={len(content)}）"
                )
            return parsed
    except HTTPException:
        raise
    except Exception as exc:
        KBD_LLM_REQUESTS_TOTAL.labels(
            operation="extract_root_signal", model=LLM_MODEL, status="error", finish_reason="transport_error"
        ).inc()
        logger.exception(
            event="root_signal_llm_failed",
            error=exc,
            error_code="ROOT_SIGNAL_LLM_CALL_FAILED",
            trace_id=get_current_trace_id(),
            kbd_id=kbd_id,
            model=LLM_MODEL,
        )
        raise exc


# ─── 核心执行函数（单篇；批量与 HTTP 路由均复用它）──────────────────────────
class RootSignalResult(BaseModel):
    success: bool
    kbd_id: int
    support_id: str | None = None
    match_type: str | None = None
    needs_review: bool | None = None
    distinctiveness: dict[str, int] = Field(default_factory=dict)
    signals: list[dict[str, Any]] = Field(default_factory=list)
    attempts: int = 0
    signals_count: int = 0
    prompt_revision: str | None = None
    error: str | None = None
    code: str | None = None  # 显式收口码（如 ROOT_SIGNAL_ALL_REJECTED）
    rejected: list[dict[str, Any]] = Field(default_factory=list)  # 不可闭环的拒绝候选（入 root_signal_rejected_json）
    closure: dict[str, Any] = Field(default_factory=dict)  # 闭包汇总（total/closed/rejected/all_rejected）


def _render_kbd_content(row: Any) -> str:
    """渲染供 LLM 阅读的整篇 KBD 文本。

    优先取聚合渲染字段 content_md（本就是「供 LLM 上下文注入」字段）；
    content_md 缺失时回退按章节字段拼接，保证抽取输入不为空。
    """
    content_md = (row["content_md"] or "").strip()
    if content_md:
        return content_md
    section_labels = {
        "problem_description": "问题描述",
        "alert_info": "告警信息",
        "steps_text": "有效排查步骤",
        "root_cause": "根因",
        "solution": "解决方案",
        "operational_impact": "操作影响范围",
        "recommendations": "建议与总结",
    }
    parts = [f"# {row['title'] or ''}"]
    for key, label in section_labels.items():
        text = (row[key] or "").strip()
        if text:
            parts.append(f"## {label}\n{text}")
    return "\n\n".join(parts).strip()


async def extract_root_signal_for_kbd(
    db_manager: DatabaseManager, kbd_id: int, *, corpus: ConflictCorpus | None = None
) -> dict[str, Any]:
    """读取整篇 KBD → 热加载 Prompt → LLM 抽取多信号 → 门禁 → 逐条冲突度 → 写回 root_signal_json 数组。

    治本约束（v2）：
    - 输入面：抽取 Prompt 读整篇 content_md（缺失时回退章节字段拼接）；root_cause 仍单独传入供主锚溯源；
    - 门禁失败不伪造、不清空：既有信号保持原值，本次无可用信号时以 success=false 返回原因（部分成功逐条丢弃）；
    - 写回只动 root_signal_json（AI 派生元数据，顶层数组），不触发 kbd_revision、不改 content_md/embedding/tsv。
    """
    trace_id = get_current_trace_id()
    async with db_manager.async_session_factory() as session:
        result = await session.execute(
            select(
                KbdEntry.id,
                KbdEntry.support_id,
                KbdEntry.title,
                KbdEntry.root_cause,
                KbdEntry.content_md,
                KbdEntry.signals_json,
                KbdEntry.problem_description,
                KbdEntry.alert_info,
                KbdEntry.steps_text,
                KbdEntry.solution,
                KbdEntry.operational_impact,
                KbdEntry.recommendations,
                KbdEntry.category_id,
                KbdEntry.ai_category_id,
                KbdEntry.status,
            ).where(KbdEntry.id == kbd_id)
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

        root_cause = (row["root_cause"] or "").strip()
        # v2 废除服务端 MIN_ROOT_CAUSE_LEN 硬 422 门：整篇输入后根因短≠无确证信号（是否可抽由门禁与实测裁决）。
        kbd_content = _render_kbd_content(row)

        category_code = row["category_id"] or row["ai_category_id"] or ""
        category_path = category_code
        if category_code:
            category = await session.scalar(select(KbCategory).where(KbCategory.code == category_code))
            if category is not None and category.path_labels:
                category_path = " > ".join(str(item) for item in category.path_labels)

        # 热加载：admin-ui 改 Prompt 后下一次抽取即生效；占位符契约不符即抛错阻断（不静默降级）
        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            ROOT_SIGNAL_PROMPT_NAME,
            list(ROOT_SIGNAL_PLACEHOLDERS),
            consumer=CONSUMER,
            trace_id=trace_id,
        )

    prompt_revision = "sha256:" + hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()
    entry = {
        "support_id": row["support_id"],
        "title": row["title"] or "",
        "category_id": category_code,
        "category_path": category_path,
        "root_cause": root_cause,
        "kbd_content": kbd_content,
    }

    async def _llm_call(messages: list[dict[str, str]]) -> dict[str, Any]:
        # messages = [system, user]（首轮）或 [system, user, feedback]（门禁回喂重试）
        return await _call_llm_messages(
            messages,
            prompt_revision=prompt_revision,
            kbd_id=kbd_id,
            attempt=max(1, len(messages) - 1),
        )

    # 解析本 KBD 关键轨（signals_json v2 文档）作为闭包 available 池：diagnostic produces + 外部声明变量。
    raw_signals_json = row["signals_json"]
    if isinstance(raw_signals_json, dict):
        key_signals = raw_signals_json.get("signals") or []
        verification_contract = raw_signals_json.get("verification_contract")
    elif isinstance(raw_signals_json, list):
        key_signals = raw_signals_json  # 兼容旧列表形态
        verification_contract = None
    else:
        key_signals, verification_contract = [], None

    outcome = await extract_root_signal(
        entry,
        prompt_template=prompt_template,
        llm_call=_llm_call,
        enforce_closure=True,
        key_signals=key_signals,
        verification_contract=verification_contract,
    )
    signals = outcome["signals"]              # 闭包裁决后保留的 accepted（closed executable + text + bootstrap 保留）
    rejected = outcome.get("rejected") or []  # 不可闭环的拒绝候选（落 root_signal_rejected_json）
    # L3 剪枝项同样是「人工需要知道的信号去向」：新增门禁不能静默删信号。
    # 与 rejected 合写入同一审计列（reason_code 区分），但指标独立计量。
    pruned = outcome.get("pruned") or []
    all_rejected = outcome.get("all_rejected", False)
    closure = outcome.get("closure") or {}
    rejected_for_audit = [*rejected, *pruned]

    # 指标（§8）：closure_total 按每条 executable 四态分维；rejected_total 仅计实际被硬拒的候选。
    # bootstrap 保留的悬空计入 dangling 态但不入 rejected 计数（未被真拒）。
    for rec in outcome.get("closure_results") or []:
        status = rec.get("closure_status")
        if status in {"closed", "dangling_requires", "hardcoded_literals", "capability_missing"}:
            ROOT_SIGNAL_CLOSURE_TOTAL.labels(status=status).inc()
    for item in rejected:
        ROOT_SIGNAL_REJECTED_TOTAL.labels(reason_code=item.get("reason_code") or "unknown").inc()
    for item in pruned:
        ROOT_SIGNAL_PRUNED_TOTAL.labels(reason_code=item.get("reason_code") or "unknown").inc()

    if not signals and not rejected:
        # 真·无产出（LLM 无可用信号或门禁全失败）：既有信号保持原值，本次不写库
        logger.error(
            event="root_signal_extract_failed",
            kbd_id=kbd_id,
            support_id=row["support_id"],
            attempts=outcome["attempts"],
            reason=outcome["error"],
            trace_id=trace_id,
        )
        return RootSignalResult(
            success=False,
            kbd_id=kbd_id,
            support_id=row["support_id"],
            attempts=outcome["attempts"],
            prompt_revision=prompt_revision,
            error=outcome["error"],
        ).model_dump()

    # all_rejected 仅按 executable 维度判定（见 root_signal_closure.adjudicate：executable_total>0 且无一 closed），
    # 不代表「本次零产出」：模型可能同时产出了合法的 text 根因（accepted）。故仅当一个 accepted 都没有时
    # 才走显式收口失败分支；只要有 accepted 信号就落到下方成功写库（同时保留拒绝审计），
    # 否则会误把好的 text 根因连同拒绝一起丢弃（工单 38819 复盘：1 条 text + 1 条悬空 DATE executable 被整单判失败）。
    if all_rejected and not signals:
        # 存在 executable 候选、回喂重试后仍全部不可闭环且无任何 accepted → 显式收口 ROOT_SIGNAL_ALL_REJECTED（retryable），
        # 绝不静默产出空数组伪装「无根因」（对齐 #1159）；落拒绝候选审计快照供人工恢复/降级，
        # 但不清空既有 root_signal_json（治本约束：门禁失败不静默清空既有信号）。
        async with db_manager.async_session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE kbd_entry
                    SET root_signal_rejected_json = CAST(:rejected AS jsonb), updated_at = now()
                    WHERE id = :kbd_id
                    """
                ),
                {"kbd_id": kbd_id, "rejected": json.dumps(rejected_for_audit, ensure_ascii=False)},
            )
            await session.commit()
        reason = "；".join(f"{r.get('reason_code')}: {r.get('reason')}" for r in rejected[:5])
        logger.error(
            event="root_signal_all_rejected",
            kbd_id=kbd_id,
            support_id=row["support_id"],
            rejected_count=len(rejected),
            attempts=outcome["attempts"],
            reason=reason,
            trace_id=trace_id,
        )
        return RootSignalResult(
            success=False,
            kbd_id=kbd_id,
            support_id=row["support_id"],
            attempts=outcome["attempts"],
            prompt_revision=prompt_revision,
            code="ROOT_SIGNAL_ALL_REJECTED",
            error=f"根因候选全部不可闭环：{reason}",
            rejected=rejected,
            closure=closure,
        ).model_dump()

    # accepted 非空：冲突度按现场语料逐条实测（LLM 自报的 distinctiveness 不采信）
    # 显式区分「未传入语料（现取）」与「传入空语料（如单测对照，零冲突基线）」
    conflict_corpus = corpus if corpus is not None else await get_conflict_corpus(db_manager)
    for signal in signals:
        signal["distinctiveness"] = conflict_corpus.conflicts(signal, self_support_id=row["support_id"])

    async with db_manager.async_session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE kbd_entry
                SET root_signal_json = CAST(:signals AS jsonb),
                    root_signal_rejected_json = CAST(:rejected AS jsonb),
                    updated_at = now()
                WHERE id = :kbd_id
                """
            ),
            {
                "kbd_id": kbd_id,
                "signals": json.dumps(signals, ensure_ascii=False),
                "rejected": json.dumps(rejected_for_audit, ensure_ascii=False),
            },
        )
        await session.commit()

    # 首条主锚代表本（仅供旧单信号消费方兼容展示）；完整多信号见 signals 数组
    lead = signals[0]
    logger.info(
        event="root_signal_extract_done",
        kbd_id=kbd_id,
        support_id=row["support_id"],
        kbd_status=row["status"],
        signals_count=len(signals),
        rejected_count=len(rejected),
        pruned_count=len(pruned),
        tiers=sorted({s["provenance"]["trace_tier"] for s in signals}),
        needs_review=sum(1 for s in signals if s["provenance"]["needs_review"]),
        attempts=outcome["attempts"],
        prompt_revision=prompt_revision,
        corpus_size=len(conflict_corpus),
        trace_id=trace_id,
    )
    return RootSignalResult(
        success=True,
        kbd_id=kbd_id,
        support_id=row["support_id"],
        match_type=lead["match_type"],
        needs_review=lead["provenance"]["needs_review"],
        distinctiveness=lead["distinctiveness"],
        signals=signals,
        # 与落库审计列同口径（拒绝 + 剪枝），否则 API 看到的“去向”少于库里留的痕迹。
        rejected=rejected_for_audit,
        closure=closure,
        attempts=outcome["attempts"],
        signals_count=len(signals),
        prompt_revision=prompt_revision,
    ).model_dump()


# ─── HTTP 路由 ────────────────────────────────────────────────────────────────
@router.post("/kbd/{kbd_id}/extract-root-signal", response_model=RootSignalResult)
async def extract_root_signal_kbd_api(request: Request, kbd_id: int):
    """根因信号抽取（KBD）：从整篇 KBD 抽取全部可确定根因的结论侧定位信号并写回 root_signal_json 数组。

    默认异步：立即 202 + job_id，经 GET /kbd/{kbd_id}/extract-root-signal/status 轮询；
    ?sync=true 同步返回完整结果（admin-ui 单条按钮使用，行为与 extract-signals 一致）。
    """
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(event="extract_root_signal_kbd_request", kbd_id=kbd_id, trace_id=trace_id)

    sync_mode = request.query_params.get("sync", "").lower() == "true"
    if sync_mode:
        return await extract_root_signal_for_kbd(_db_manager, kbd_id)

    async def _runner(k_id: int) -> dict[str, Any]:
        return await extract_root_signal_for_kbd(_db_manager, k_id)

    job_id = await _root_signal_jobs.submit(kbd_id, _runner, trace_id=trace_id)
    logger.info(event="extract_root_signal_kbd_submitted", kbd_id=kbd_id, job_id=job_id, trace_id=trace_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "success": True,
            "kbd_id": kbd_id,
            "job_id": job_id,
            "status": "pending",
            "message": "根因信号抽取任务已提交，请通过 GET /extract-root-signal/status 轮询进度",
        },
    )


@router.get("/kbd/{kbd_id}/extract-root-signal/status")
async def get_extract_root_signal_status(request: Request, kbd_id: int, job_id: str):
    """查询异步根因信号抽取 Job 状态（与 extract-signals/status 同契约）。"""
    _check_auth(request)
    job = await _root_signal_jobs.get_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} 不存在（可能已重启丢失）")
    if job["kbd_id"] != kbd_id:
        raise HTTPException(status_code=400, detail="job_id 与 kbd_id 不匹配")
    if job.get("finished_at"):
        job["elapsed_s"] = round(job["finished_at"] - (job.get("started_at") or job["created_at"]), 1)
    return job


@router.get("/kbd/{kbd_id}/root-signal-conflicts")
async def preview_root_signal_conflicts(request: Request, kbd_id: int):
    """只读预览已落库根因信号的冲突度（按最新语料重算，不写库）。

    用途：治理复盘「这条锚点是否真的唯一定位」；不回写 distinctiveness，
    避免把随语料漂移的快照值与抽取时刻混为一谈。
    """
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    async with _db_manager.async_session_factory() as session:
        row = await session.execute(
            select(KbdEntry.support_id, KbdEntry.root_signal_json).where(KbdEntry.id == kbd_id)
        )
        item = row.mappings().first()
    if item is None:
        raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")
    signal = item["root_signal_json"]
    if not signal:
        return {"kbd_id": kbd_id, "extracted": False, "distinctiveness": None}
    # v2 落库为顶层数组（旧单对象亦兼容）：逐条按最新语料重算冲突度
    signals = signal if isinstance(signal, list) else [signal]
    signals = [s for s in signals if isinstance(s, dict)]
    if not signals:
        return {"kbd_id": kbd_id, "extracted": False, "distinctiveness": None}
    corpus = await get_conflict_corpus(_db_manager)
    return {
        "kbd_id": kbd_id,
        "extracted": True,
        "signals_count": len(signals),
        "signals": [
            {
                "match_type": s.get("match_type"),
                "trace_tier": (s.get("provenance") or {}).get("trace_tier"),
                "needs_review": (s.get("provenance") or {}).get("needs_review"),
                "stored_distinctiveness": s.get("distinctiveness"),
                "live_distinctiveness": corpus.conflicts(s, self_support_id=item["support_id"]),
            }
            for s in signals
        ],
        "corpus_size": len(corpus),
    }


# ─── 根因信号人工编辑（逐条编辑/删除/新增，复用抽取同一套硬门禁）─────────────
class RootSignalEditRequest(BaseModel):
    """全量替换根因信号数组：删除某条=提交不含它的数组，新增=追加一条。"""

    signals: list[dict[str, Any]] = Field(default_factory=list)


class RootSignalEditResult(BaseModel):
    success: bool
    kbd_id: int
    support_id: str | None = None
    signals: list[dict[str, Any]] = Field(default_factory=list)
    signals_count: int = 0
    rejected: list[dict[str, Any]] = Field(default_factory=list)  # 不可闭环的拒绝候选（已落 root_signal_rejected_json）
    closure: dict[str, Any] = Field(default_factory=dict)  # 闭包汇总（供审核 UI 即时回显）


@router.patch("/kbd/{kbd_id}/root-signal", response_model=RootSignalEditResult)
async def edit_root_signal(request: Request, kbd_id: int, body: RootSignalEditRequest) -> RootSignalEditResult:
    """人工编辑根因信号（逐条编辑/删除/新增）——保存即全量替换 root_signal_json 数组。

    与自动抽取**同标准硬门禁**：每条先经 gate_signals 去重/封顶，再复用
    normalize_root_signal（executable 的 qfk_* 词表/args 契约/只读安全门 +
    溯源逐字命中；text 的 keyword/regex + 逐字溯源），形状不合即 422 拒绝，绝不落非法信号；
    形状合规后再过**同一闭包门** `adjudicate_root_closure`（与抽取同源、含 bootstrap 保守守卫）：
    不可闭环的 executable 不进 root_signal_json、也不自动降级 text（决策②），而是落 root_signal_rejected_json
    （防绕过回潮）。写回只动 root_signal_json / root_signal_rejected_json（AI 派生元数据），不触发 kbd_revision、不改发布盖章（同回刷语义）。
    """
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            select(
                KbdEntry.id,
                KbdEntry.support_id,
                KbdEntry.title,
                KbdEntry.root_cause,
                KbdEntry.content_md,
                KbdEntry.signals_json,
                KbdEntry.problem_description,
                KbdEntry.alert_info,
                KbdEntry.steps_text,
                KbdEntry.solution,
                KbdEntry.operational_impact,
                KbdEntry.recommendations,
                KbdEntry.category_id,
                KbdEntry.ai_category_id,
                KbdEntry.status,
            ).where(KbdEntry.id == kbd_id)
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

    root_cause = (row["root_cause"] or "").strip()
    kbd_content = _render_kbd_content(row)
    category_code = row["category_id"] or row["ai_category_id"] or ""

    # 关键轨 produces 上下文（闭包可用来源池），与自动抽取同源：人工编辑亦须过同一闭包门（§7 防绕过回潮）。
    raw_signals_json = row["signals_json"]
    if isinstance(raw_signals_json, dict):
        key_signals = raw_signals_json.get("signals") or []
        verification_contract = raw_signals_json.get("verification_contract")
    elif isinstance(raw_signals_json, list):
        key_signals = raw_signals_json  # 兼容旧列表形态
        verification_contract = None
    else:
        key_signals, verification_contract = [], None

    # 空数组 = 清空根因信号与拒绝审计（删除最后一条的合法终态）：不跑门禁直接落两列 []
    signals: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    pruned: list[dict[str, Any]] = []
    closure: dict[str, Any] = {}
    all_rejected = False
    if body.signals:
        gated, gate_error = gate_signals({"signals": body.signals})
        if gate_error:
            raise HTTPException(
                status_code=422,
                detail={"code": "ROOT_SIGNAL_INVALID", "message": gate_error},
            )
        normalized: list[dict[str, Any]] = []
        for raw in gated:
            signal, violation, _tier = normalize_root_signal(
                raw,
                root_cause=root_cause,
                content=kbd_content,
                category_id=category_code,
                prompt_revision="manual-edit",
            )
            if violation:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "ROOT_SIGNAL_NOT_CONFIRMING",
                        "signal_text": str(raw.get("signal_text") or ""),
                        "message": violation,
                    },
                )
            normalized.append(signal)

        # 闭包门（§7 编辑即校验，防绕过回潮）：复用抽取同一 adjudicate_root_closure（含 bootstrap 保守守卫）——
        # 不可闭环的 executable 不进 root_signal_json、也不自动降级 text（决策②），而是落 root_signal_rejected_json。
        adjud = adjudicate_root_closure(
            normalized,
            key_signals,
            verification_contract=verification_contract,
        )
        signals = adjud["accepted"]
        rejected = adjud["rejected"]
        closure = adjud["summary"]
        all_rejected = adjud["all_rejected"]
        for rec in adjud["results"]:
            rec_status = rec.get("closure_status")
            if rec_status in {"closed", "dangling_requires", "hardcoded_literals", "capability_missing"}:
                ROOT_SIGNAL_CLOSURE_TOTAL.labels(status=rec_status).inc()
        for item in rejected:
            ROOT_SIGNAL_REJECTED_TOTAL.labels(reason_code=item.get("reason_code") or "unknown").inc()

        # L3 最少性（与抽取同标准）：人工一次提交多条时仍要求「最少充分集」。
        # 冗余项不静默丢弃（落拒绝审计），但**不 422**：人工显式提交的冗余是可用但低效的，
        # 硬拒会让「先保存再逐步精简」的工作流无法进行。
        if signals:
            signals, pruned = gate_minimality(signals)
            for item in pruned:
                ROOT_SIGNAL_PRUNED_TOTAL.labels(reason_code=item.get("reason_code") or "unknown").inc()
            rejected = [*rejected, *pruned]

        # 全拒收口（与自动抽取同款保护，见 extract_root_signal_for_kbd 的 all_rejected 分支）：
        # 本次提交的 executable 候选全部不可闭环且无任何 accepted 时，只落拒绝审计、
        # **绝不清空既有 root_signal_json**，并 422 明确告知。
        # 修复前的反例：专家把 3 条改成 3 条全悬空 → 下方 UPDATE 写 [] → 原有好信号被静默抹掉。
        # 「清空」这一意图由 body.signals == [] 单独表达（见本函数开头的空数组分支），不需要靠全拒来达成。
        if all_rejected and not signals:
            async with _db_manager.async_session_factory() as session:
                await session.execute(
                    text(
                        """
                        UPDATE kbd_entry
                        SET root_signal_rejected_json = CAST(:rejected AS jsonb), updated_at = now()
                        WHERE id = :kbd_id
                        """
                    ),
                    {"kbd_id": kbd_id, "rejected": json.dumps(rejected, ensure_ascii=False)},
                )
                await session.commit()
            logger.warning(
                event="root_signal_manual_edit_all_rejected",
                trace_id=trace_id,
                kbd_id=kbd_id,
                support_id=row["support_id"],
                rejected_count=len(rejected),
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ROOT_SIGNAL_ALL_REJECTED",
                    "message": (
                        f"本次提交的 {len(rejected)} 条可执行信号均不可闭环（缺生产者/硬编码字面量/能力缺失），"
                        "已拒绝保存；既有根因信号保持不变（如需清空请提交空数组）"
                    ),
                    "rejected": rejected,
                    "retryable": True,
                },
            )

        # 冲突度按现场语料逐条实测（与抽取一致，不采信前端自报；仅对 accepted）
        corpus = await get_conflict_corpus(_db_manager)
        for signal in signals:
            signal["distinctiveness"] = corpus.conflicts(signal, self_support_id=row["support_id"])

    async with _db_manager.async_session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE kbd_entry
                SET root_signal_json = CAST(:signals AS jsonb),
                    root_signal_rejected_json = CAST(:rejected AS jsonb),
                    updated_at = now()
                WHERE id = :kbd_id
                """
            ),
            {
                "kbd_id": kbd_id,
                "signals": json.dumps(signals, ensure_ascii=False),
                "rejected": json.dumps(rejected, ensure_ascii=False),
            },
        )
        await session.commit()

    logger.info(
        event="root_signal_manual_edit",
        trace_id=trace_id,
        kbd_id=kbd_id,
        support_id=row["support_id"],
        kbd_status=row["status"],
        signals_count=len(signals),
        rejected_count=len(rejected),
        pruned_count=len(pruned),
        all_rejected=all_rejected,
        needs_review=sum(1 for s in signals if s["provenance"]["needs_review"]),
    )
    return RootSignalEditResult(
        success=True,
        kbd_id=kbd_id,
        support_id=row["support_id"],
        signals=signals,
        signals_count=len(signals),
        rejected=rejected,
        closure=closure,
    )


def _check_auth(request: Request) -> None:
    """验证内部服务 Token（与 admin._check_auth 同源）。

    延迟导入：admin.py 会注册本模块的批量处理器，顶部互相 import 会构成循环依赖。
    """
    from app.routes.admin import _check_auth as _admin_check_auth

    _admin_check_auth(request)
