"""
KB Service - 关键信号分级抽取路由（Key-Signal Field-level Extraction）

POST /api/admin/kbd/{kbd_id}/extract-signals
  - 镜像 VISION reanalyze-images 模式：kb-service 服务端做 LLM + 直接写回 kbd_entry.signals_json
  - 从 KBD 自然语言章节（steps_text/problem_description/alert_info/title）抽取 producer(QKV)/consumer(QFK) 结构化关键信号
  - 封闭采集器词表校验 + {{VAR}} 大写占位符强制校验（ADR-2）
  - 调用方：data-pipeline Stage.EXTRACT_SIGNALS（INTERNAL_API_TOKEN）、admin-ui 手动按钮

POST /api/admin/sop/{sop_id}/extract-signals
  - 镜像 KBD 路径：从 sop_document.tree_json 决策树节点提取诊断动作 → 关键信号
  - 与 KBD 共用 ACQUIRER_CATALOG / DEFAULT_VARIABLE_SCHEMA / 校验 / 写回 signals_json
  - 跨文档通用（详见 docs/.../关键信号字段级分别抽取.md ADR-4）

设计参考：docs/solution/agent/02-架构设计/关键信号字段级分别抽取.md
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.utils.prompt_loader import StrictPromptLoader
from sqlalchemy import select, text

from app.services.signal_job_manager import get_signal_job_manager

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-extract-signals")
router = APIRouter(prefix="/api/admin", tags=["extract-signals"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None

# LLM 配置（与 classify.py 同源 LLM_* 命名）
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# 抽取可用更强模型；未配置则回退到分类模型
LLM_MODEL = os.environ.get("EXTRACT_SIGNALS_MODEL", os.environ.get("CLASSIFY_MODEL", "qwen3.7-plus"))

# Prompt 名称（system_prompt 表热加载，admin-ui 可在线编辑）
_EXTRACT_PROMPT_NAME = "kbd_extract_signals_v1"

# ─── 封闭采集器词表（acquirer 必须取自此处）─────────────────────────────────
# QKV（生产者，3 方法）+ QFK（消费者，8 个 namespace）
# display_name 标准命名（勿擅自修改以免造成误解）：
#   qkv_alert - 前端信号-告警查询
#   qkv_task  - 前端信号-任务查询
#   qkv_dialog - 前端信号-弹框查询
#   qfk_log      - 后端信号-日志检查和操作
#   qfk_service  - 后端信号-服务检查和操作
#   qfk_system   - 后端信号-系统检查和操作
#   qfk_vm       - 后端信号-虚拟机相关操作
#   qfk_network  - 后端信号-网络相关操作
#   qfk_storage  - 后端信号-存储相关操作
#   qfk_hardware - 后端信号-硬件相关操作
#   qfk_platform - 后端信号-平台相关操作
ACQUIRER_CATALOG: dict[str, str] = {
    "qkv_alert": "前端信号-告警查询：acli alert get，产出 host/vm/target/alert_type/end 等",
    "qkv_task": "前端信号-任务查询：acli task get，产出 status/host/vm/errcode_tracing/request_id 等",
    "qkv_dialog": "前端信号-弹框查询：acli dialog/log get",
    "qfk_log": "后端信号-日志检查和操作：acli log get -k <keyword> [-f resource] [-p path] [-t time_window]，keyword 求值",
    "qfk_service": "后端信号-服务检查和操作：acli service {asv|anet|host} <name> status，state 求值",
    "qfk_system": "后端信号-系统检查和操作：acli system <sub_command>（如 lsof/ps/lsblk/iostat/smartctl），threshold/keyword/json_path 求值",
    "qfk_vm": "后端信号-虚拟机相关操作：acli vm <sub_command>，state/json_path/exists 求值",
    "qfk_network": "后端信号-网络相关操作：acli network <sub_command>，state/json_path/exists 求值",
    "qfk_storage": "后端信号-存储相关操作：acli storage <sub_command>（如 asan disk list），state/json_path/exists 求值",
    "qfk_hardware": "后端信号-硬件相关操作：acli hardware <sub_command>，state/json_path/exists 求值",
    "qfk_platform": "后端信号-平台相关操作：acli platform <sub_command>，state/json_path/exists 求值",
}

# ─── 默认变量池 schema（produces/requires 引用的变量名集合）───────────────────
DEFAULT_VARIABLE_SCHEMA: list[str] = [
    "HOST", "VM", "NODE_IP", "TARGET", "END", "ALERT_TYPE",
    "STATUS", "ERRCODE_TRACING", "REQUEST_ID",
]

VALID_CATEGORIES = {"frontend", "backend"}
VALID_MATCHER_TYPES = {"keyword", "regex", "state", "threshold", "json_path", "exists"}

# ADR-2：{{VAR}} 大写占位符正则（单一真相源；运行期校验用）
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\}\}")
# 用于检测"任何双花括号占位符"（含非法大小写）以报错
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^}]*)\}\}")

# 字段级溯源：抽取方法标识（写入每条信号的 extraction_method）
EXTRACTION_METHOD = "llm_field_level_v1"
# 低置信阈值：校准后置信度低于此值标 needs_review（镜像 classify 的 low_confidence，§3）
NEEDS_REVIEW_CONFIDENCE_THRESHOLD = 0.5

# 合法信号来源章节（LLM 自报 source_section 必须落在此集合，否则回退推断）。
# 治本约束：只有「诊断叙事字段」可作为信号来源；根因(root_cause)与解决方案(solution)
# 是抽取 OUTPUT（确认后返回给用户），绝不作为信号抽取输入，避免把"处置动作"误抽成诊断信号。
_VALID_SOURCE_SECTIONS = {
    "title", "problem_description", "alert_info", "steps_text",
}

# ─── 写操作子命令词表（处置/变更动作，不得自动执行）────────────────────────
# 这是"诊断只读"原则的硬边界：凡 sub_command 命中以下动词的后端信号，一律标记为
# phase=solution / require_human_confirm=True，执行层绝不自动运行，必须人工授权。
WRITE_OP_SUB_COMMANDS: set[str] = {
    "start", "stop", "shutdown", "restart", "suspend", "resume",
    "migrate", "clone", "snapshot", "reset", "reboot",
    "delete", "remove", "del", "rm", "format", "wipe", "destroy",
    "enable", "disable", "kill", "killall", "pkill",
    "up", "down", "set", "create", "add", "modify", "update",
}
# 破坏性（不可逆）子命令 → risk=3（block）
_DESTRUCTIVE_SUB_COMMANDS: set[str] = {
    "delete", "remove", "del", "rm", "format", "wipe", "destroy",
}


def _is_write_op_signal(signal: dict[str, Any]) -> bool:
    """判断一条后端信号是否为写操作/处置动作（基于 acquirer + sub_command 词表）。

    仅 backend（qfk_*）信号可能携带写操作；前端生产者（qkv_*）均为只读查询。
    sub_command 形如 'kill -9 240132' / 'ps auxf' / 'start'，按空白与 | / 切词后命中词表即判写操作。
    """
    if signal.get("signal_category") != "backend":
        return False
    acquirer = signal.get("acquirer", "")
    if not acquirer.startswith("qfk_"):
        return False
    args = signal.get("acquirer_args") or {}
    sub = str(args.get("sub_command", "") or "")
    tokens = re.split(r"[\s|/]+", sub.strip())
    return any(tok in WRITE_OP_SUB_COMMANDS for tok in tokens)


def _write_op_risk(signal: dict[str, Any]) -> int:
    """写操作信号的风险等级：破坏性子命令=3(block)，其余写操作=2(confirm)。"""
    args = signal.get("acquirer_args") or {}
    sub = str(args.get("sub_command", "") or "").lower()
    tokens = set(re.split(r"[\s|/]+", sub))
    if tokens & _DESTRUCTIVE_SUB_COMMANDS:
        return 3
    return 2


def _signal_quality_score(signal: dict[str, Any]) -> float:
    """结构质量分(0-1)：占位符/变量/判定契约合法性。

    经过 _validate_signal 的信号已保证占位符大写、requires 在 schema 内，
    故此处仅对"判定/产出契约完整性"打分，作为置信度校准的结构质量因子。
    """
    score = 0.3  # 占位符大写 + 变量合法性基础分（已校验通过）
    if signal.get("signal_category") == "backend":
        score += 0.3 if signal.get("requires") else 0.3  # backend 允许无 requires
        score += 0.4 if signal.get("matcher") else 0.0
    else:  # frontend
        score += 0.3 if signal.get("requires") else 0.3
        score += 0.4 if signal.get("produces") else 0.0
    return min(1.0, score)


def _infer_source(signal: dict[str, Any]) -> str:
    """字段级溯源：推断信号主要来自哪个输入章节（source）。

    治本约束：信号只能来自诊断叙事字段（标题/问题描述/告警/有效排查步骤）；
    根因与解决方案不再作为信号来源，故一律回退到 steps_text。
    """
    ss = (signal.get("source_section") or "").strip()
    if ss in _VALID_SOURCE_SECTIONS:
        return ss
    return "steps_text"


def _calibrate_confidence(signal: dict[str, Any], quality: float) -> float:
    """字段级置信度校准（基于信号质量与证据充分性）。

    校准 = base × (0.5·结构质量 + 0.5·证据充分性)，并钳制到 [0.05, 0.99]。
    - base：LLM 自评估 confidence（0-1），缺失时取默认 0.7
    - 证据充分性：backend 必须有 matcher、frontend 必须有 produces，否则降级
    """
    llm_conf = signal.get("confidence")
    try:
        llm_conf = float(llm_conf) if llm_conf is not None else None
    except (TypeError, ValueError):
        llm_conf = None
    if llm_conf is None or not (0.0 <= llm_conf <= 1.0):
        llm_conf = 0.7

    if signal.get("signal_category") == "backend":
        evidence = 1.0 if signal.get("matcher") else 0.3
    else:
        evidence = 1.0 if signal.get("produces") else 0.4

    calibrated = llm_conf * (0.5 * quality + 0.5 * evidence)
    return round(max(0.05, min(0.99, calibrated)), 3)


def _enrich_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """为单条信号补充字段级溯源与置信度校准（不改变既有字段）。

    新增字段（对齐评审清单）：
    - source: 主要来源章节（字段级溯源）
    - extraction_method: 抽取方法标识（固定为 llm_field_level_v1）
    - confidence: 校准后置信度(0-1)
    - phase: diagnostic(诊断只读) / solution(处置动作)
    - require_human_confirm: 是否必须人工授权后才可执行
    - risk: 风险等级 1/2/3（供执行层门禁与分类器兜底）
    """
    quality = _signal_quality_score(signal)
    enriched = dict(signal)
    enriched["extraction_method"] = EXTRACTION_METHOD
    enriched["source"] = _infer_source(signal)
    enriched["confidence"] = _calibrate_confidence(signal, quality)
    # 低置信/歧义信号标 needs_review（镜像 classify 的 low_confidence，§3）
    enriched["needs_review"] = enriched["confidence"] < NEEDS_REVIEW_CONFIDENCE_THRESHOLD

    # 写操作/处置动作信号：标记为人⼯授权，绝不自动执行（诊断只读原则）
    if enriched.get("require_human_confirm"):
        enriched["phase"] = enriched.get("phase", "solution")
        enriched["risk"] = enriched.get("risk", 2)
    elif _is_write_op_signal(enriched):
        enriched["phase"] = "solution"
        enriched["require_human_confirm"] = True
        enriched["risk"] = _write_op_risk(enriched)
    else:
        enriched["phase"] = "diagnostic"
        enriched["require_human_confirm"] = False
        enriched["risk"] = 1
    return enriched


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖（由 main.py lifespan 调用）"""
    global _db_manager
    _db_manager = db


def _check_auth(request: Request) -> None:
    """验证内部服务 Token（与 admin._check_auth 一致）"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")


def validate_placeholder_case(template_str: str) -> list[str]:
    """校验字符串中所有 {{...}} 占位符均为全大写规范。

    Returns: 合法占位符列表。
    Raises: ValueError 当存在非法（小写/混合/空）占位符时。
    """
    valid: list[str] = []
    for m in _ANY_PLACEHOLDER_RE.finditer(template_str):
        inner = m.group(1)
        if not _PLACEHOLDER_RE.fullmatch(m.group(0)):
            raise ValueError(f"非法占位符 {{{{{inner}}}}}：必须为 {{全大写}} 形式（如 {{{{HOST}}}}）")
        valid.append(inner)
    return valid


def _validate_signal(signal: dict[str, Any], available_vars: set[str]) -> tuple[bool, str | None]:
    """校验单条信号：类别/acquirer/占位符/变量引用合法性。

    写操作拦截：对 backend（qfk_*）信号，若 sub_command 命中写操作词表，则就地标记
    phase=solution / require_human_confirm=True / risk，使其被排除在自动执行之外，
    交由人工授权（不丢弃——写操作可能是排查步骤里确实需要人确认的动作）。
    """
    cat = signal.get("signal_category")
    if cat not in VALID_CATEGORIES:
        return False, f"signal_category 非法: {cat}"

    acquirer = signal.get("acquirer", "")
    if acquirer not in ACQUIRER_CATALOG:
        return False, f"acquirer 不在采集器词表内: {acquirer}"

    for field in ("acquirer_args", "matcher"):
        val = signal.get(field)
        if val is None:
            continue
        try:
            _validate_obj_placeholders(val)
        except ValueError as e:
            return False, str(e)

    for p in signal.get("produces") or []:
        name = p.get("name", "")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            return False, f"produces 变量名非全大写: {name}"

    for r in signal.get("requires") or []:
        if r not in available_vars:
            return False, f"requires 变量不在 schema 内: {r}"

    if cat == "backend":
        matcher = signal.get("matcher") or {}
        if matcher.get("type") not in VALID_MATCHER_TYPES:
            return False, f"backend 信号 matcher.type 非法或缺失: {matcher.get('type')}"
        # 写操作/处置动作拦截（全面梳理 qfk_vm/qfk_system/... 的写子命令）
        if _is_write_op_signal(signal):
            signal["phase"] = "solution"
            signal["require_human_confirm"] = True
            signal["risk"] = _write_op_risk(signal)

    return True, None


def _validate_obj_placeholders(obj: Any) -> None:
    """递归校验 dict/list/str 中所有字符串值的 {{VAR}} 占位符为大写。"""
    if isinstance(obj, str):
        validate_placeholder_case(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _validate_obj_placeholders(v)
    elif isinstance(obj, list):
        for v in obj:
            _validate_obj_placeholders(v)


async def _call_llm(prompt: str) -> dict[str, Any]:
    """调用 LLM API（json_object 模式），返回解析后的 dict。"""
    from openai import AsyncOpenAI

    if not LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY 未配置")

    client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    try:
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是 HCI 关键信号抽取专家，输出严格遵循 JSON 格式。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=2000,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error("extract_signals LLM 响应 JSON 解析失败: %s", e)
        raise HTTPException(status_code=500, detail="LLM 响应格式错误") from e
    except Exception as e:
        logger.error("extract_signals LLM 调用失败: %s", e)
        raise HTTPException(status_code=503, detail=f"LLM API 调用失败: {e}") from e


def _validate_and_collect_signals(raw_signals: list, source_id: Any) -> tuple[list, list]:
    """对 LLM 返回的信号列表做校验，返回 (validated, rejected)。

    Args:
        raw_signals: LLM 返回的 signal dict 列表
        source_id: 来源标识（用于日志：kbd_id / sop_id）
    """
    available_vars = set(DEFAULT_VARIABLE_SCHEMA)
    for s in raw_signals:
        for p in s.get("produces") or []:
            name = p.get("name", "")
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                available_vars.add(name)

    validated: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for s in raw_signals:
        ok, err = _validate_signal(s, available_vars)
        if ok:
            # 字段级溯源 + 置信度校准（评审清单：source/extraction_method/confidence）
            validated.append(_enrich_signal(s))
        else:
            rejected.append({"signal": s, "reason": err})
            logger.warning("extract_signals 信号被丢弃 source=%s reason=%s", source_id, err)

    return validated, rejected


async def _persist_signals(db_manager: DatabaseManager, table: str, source_id: int,
                          signals: list[dict[str, Any]]) -> None:
    """通用写回：signals_json 列。table ∈ {'kbd_entry', 'sop_document'}。"""
    async with db_manager.async_session_factory() as session:
        await session.execute(
            text(f"UPDATE {table} SET signals_json = CAST(:sj AS jsonb), updated_at = NOW() WHERE id = :id"),
            {"sj": json.dumps(signals, ensure_ascii=False), "id": source_id},
        )
        await session.commit()


async def extract_signals_for_kbd(db_manager: DatabaseManager, kbd_id: int) -> dict[str, Any]:
    """KBD 路径：读取 kbd_entry 章节 → LLM 抽取 → 校验 → 写回 signals_json。

    治本约束：抽取输入仅含「诊断叙事字段」（title/problem_description/alert_info/steps_text）。
    root_cause 与 solution 是抽取 OUTPUT（确认后返回给用户），不再作为信号抽取来源，
    从根上杜绝把"处置动作"（如 acli vm start / kill -9）误抽成诊断信号。
    """
    from app.models.kbd_entry import KbdEntry

    async with db_manager.async_session_factory() as session:
        result = await session.execute(select(KbdEntry).where(KbdEntry.id == kbd_id))
        entry = result.scalar_one_or_none()
        if entry is None:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            _EXTRACT_PROMPT_NAME,
            ["title", "problem_description", "alert_info", "steps_text",
             "root_cause", "solution", "category_id", "acquirer_catalog", "variable_schema"],
            consumer="kb-service.extract_signals.kbd",
        )

    acquirer_catalog_text = "\n".join(f"- {k}: {v}" for k, v in ACQUIRER_CATALOG.items())
    variable_schema_text = ", ".join(DEFAULT_VARIABLE_SCHEMA)
    prompt = prompt_template.format(
        title=entry.title or "",
        problem_description=(entry.problem_description or "")[:2000],
        alert_info=(entry.alert_info or "")[:1000],
        steps_text=(entry.steps_text or "")[:4000],
        # 治本：根因/解决方案不参与信号抽取，置空传入（模板占位符仍存在，避免 StrictPromptLoader 校验失败）
        root_cause="",
        solution="",
        category_id=entry.category_id or entry.ai_category_id or "",
        acquirer_catalog=acquirer_catalog_text,
        variable_schema=variable_schema_text,
    )

    llm_result = await _call_llm(prompt)
    raw_signals = llm_result.get("signals", [])
    if not isinstance(raw_signals, list):
        raise HTTPException(status_code=500, detail="LLM 未返回 signals 数组")
    validated, rejected = _validate_and_collect_signals(raw_signals, f"kbd:{kbd_id}")

    await _persist_signals(db_manager, "kbd_entry", kbd_id, validated)
    logger.info(event="extract_signals_kbd_done", kbd_id=kbd_id,
                total=len(raw_signals), validated=len(validated), rejected=len(rejected))
    return {
        "success": True, "kbd_id": kbd_id,
        "signals_count": len(validated), "rejected_count": len(rejected),
        "signals": validated, "rejected": rejected,
    }


async def extract_signals_for_sop(db_manager: DatabaseManager, sop_id: int) -> dict[str, Any]:
    """SOP 路径：读取 sop_document.tree_json 决策树 + variable_schema → LLM 抽取 → 校验 → 写回。"""
    from app.models.sop_document import SopDocument

    async with db_manager.async_session_factory() as session:
        result = await session.execute(select(SopDocument).where(SopDocument.id == sop_id))
        doc = result.scalar_one_or_none()
        if doc is None:
            raise HTTPException(status_code=404, detail=f"SOP 文档 {sop_id} 不存在")

        # 决策树节点序列化为文本上下文（供 LLM 抽取诊断动作）
        tree = doc.tree_json or {}
        tree_text = json.dumps(tree, ensure_ascii=False)[:6000]
        # 变量 schema 列表
        var_schema = doc.variable_schema or []
        var_names = sorted({
            (v.get("name") if isinstance(v, dict) else None) or "" for v in var_schema
        }) if var_schema else DEFAULT_VARIABLE_SCHEMA
        # 与 KBD 共用 schema（追加 SOP 已声明的变量名）
        merged_vars = sorted(set(DEFAULT_VARIABLE_SCHEMA) | {
            n for n in var_names if re.fullmatch(r"[A-Z][A-Z0-9_]*", n)
        })

        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            _EXTRACT_PROMPT_NAME,
            ["title", "problem_description", "alert_info", "steps_text",
             "root_cause", "solution", "category_id", "acquirer_catalog", "variable_schema"],
            consumer="kb-service.extract_signals.sop",
        )

    acquirer_catalog_text = "\n".join(f"- {k}: {v}" for k, v in ACQUIRER_CATALOG.items())
    variable_schema_text = ", ".join(merged_vars)
    # SOP 复用同一 Prompt：以 tree_json 当作 steps_text 输入
    prompt = prompt_template.format(
        title=doc.title or "",
        problem_description="",
        alert_info="",
        steps_text=tree_text,
        root_cause="",
        solution="",
        category_id=doc.category_id or "",
        acquirer_catalog=acquirer_catalog_text,
        variable_schema=variable_schema_text,
    )

    llm_result = await _call_llm(prompt)
    raw_signals = llm_result.get("signals", [])
    if not isinstance(raw_signals, list):
        raise HTTPException(status_code=500, detail="LLM 未返回 signals 数组")
    validated, rejected = _validate_and_collect_signals(raw_signals, f"sop:{sop_id}")

    await _persist_signals(db_manager, "sop_document", sop_id, validated)
    logger.info(event="extract_signals_sop_done", sop_id=sop_id,
                total=len(raw_signals), validated=len(validated), rejected=len(rejected))
    return {
        "success": True, "sop_id": sop_id,
        "signals_count": len(validated), "rejected_count": len(rejected),
        "signals": validated, "rejected": rejected,
    }


class ExtractSignalsResponse(BaseModel):
    success: bool
    kbd_id: int | None = None
    sop_id: int | None = None
    signals_count: int
    rejected_count: int = 0
    signals: list[dict[str, Any]] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/kbd/{kbd_id}/extract-signals", response_model=ExtractSignalsResponse)
async def extract_signals_kbd(request: Request, kbd_id: int):
    """关键信号分级抽取（KBD）：从 kbd_entry 自然语言章节抽取结构化 signals_json 并写回。

    异步模式（默认，与 VISION reanalyze-images 对齐）：
      - 立即返回 202 + job_id（避免 HTTP 长连接阻塞/超时）
      - 后台 asyncio.create_task 执行 LLM 抽取
      - 客户端通过 GET /kbd/{kbd_id}/extract-signals/status?job_id=xxx 轮询状态

    向后兼容：?sync=true 直接同步返回完整结果（供 admin-ui 单条快速调试）
    """
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(event="extract_signals_kbd_request", kbd_id=kbd_id, trace_id=trace_id)

    # 向后兼容：?sync=true 直接同步返回（供 debug / 详情页内联重新提交）
    sync_mode = request.query_params.get("sync", "").lower() == "true"
    if sync_mode:
        result = await extract_signals_for_kbd(_db_manager, kbd_id)
        return ExtractSignalsResponse(**result)

    # 异步：提交 Job（复用 SignalJobManager 状态机，与 Vision 完全对齐）
    async def _runner(k_id: int) -> dict[str, Any]:
        return await extract_signals_for_kbd(_db_manager, k_id)

    jm = get_signal_job_manager()
    job_id = await jm.submit(kbd_id, _runner, trace_id=trace_id)
    logger.info(event="extract_signals_kbd_submitted", kbd_id=kbd_id, job_id=job_id, trace_id=trace_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "success": True,
            "kbd_id": kbd_id,
            "job_id": job_id,
            "status": "pending",
            "message": "信号抽取任务已提交，请通过 GET /extract-signals/status 轮询进度",
        },
    )


@router.get("/kbd/{kbd_id}/extract-signals/status")
async def get_extract_signals_status(request: Request, kbd_id: int, job_id: str):
    """查询异步信号抽取 Job 状态（与 VISION reanalyze-images/status 对齐）。

    Returns:
        {job_id, kbd_id, status(pending|running|done|failed),
         total, done, failed, error, result, started_at, finished_at}
    """
    _check_auth(request)
    job = await get_signal_job_manager().get_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} 不存在（可能已重启丢失）")
    if job["kbd_id"] != kbd_id:
        raise HTTPException(status_code=400, detail="job_id 与 kbd_id 不匹配")
    if job.get("finished_at"):
        job["elapsed_s"] = round(job["finished_at"] - (job.get("started_at") or job["created_at"]), 1)
    return job


@router.post("/sop/{sop_id}/extract-signals", response_model=ExtractSignalsResponse)
async def extract_signals_sop(request: Request, sop_id: int) -> ExtractSignalsResponse:
    """关键信号分级抽取（SOP）：从 sop_document.tree_json 决策树提取诊断动作 → signals_json。"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    logger.info(event="extract_signals_sop_request", sop_id=sop_id)
    result = await extract_signals_for_sop(_db_manager, sop_id)
    # 把 sop_id 注入响应（模型字段）
    body = {**result, "sop_id": sop_id}
    body.pop("kbd_id", None)
    return ExtractSignalsResponse(**body)
