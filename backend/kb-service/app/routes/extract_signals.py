"""
KB Service - 关键信号分级抽取路由（Key-Signal Field-level Extraction）

POST /api/admin/kbd/{kbd_id}/extract-signals
  - 镜像 VISION reanalyze-images 模式：kb-service 服务端做 LLM + 直接写回 kbd_entry.signals_json
  - 从 KBD 自然语言章节（steps_text/root_cause/solution/problem_description/alert_info）
    抽取 producer(QKV)/consumer(QFK) 结构化关键信号
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
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger
from shared.utils.prompt_loader import StrictPromptLoader
from sqlalchemy import select, text

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
# QKV（生产者，3 方法）+ QFK（消费者，8 类）
ACQUIRER_CATALOG: dict[str, str] = {
    "qkv.alert": "前端信号-告警查询：acli alert get，产出 host/vm/target/alert_type/end 等",
    "qkv.task": "前端信号-任务查询：acli task get，产出 status/host/vm/errcode_tracing/request_id 等",
    "qkv.dialog": "前端信号-弹框/对话日志查询：acli dialog/log get",
    "qfk.log_keyword": "后端信号-日志关键字判定：在目标节点日志中匹配关键字（any/all + expected）",
    "qfk.service_status": "后端信号-服务状态判定：acli service {asv|anet|host} <name> status == expected",
    "qfk.vm_state": "后端信号-虚拟机状态：acli vm <sub_command>",
    "qfk.network_check": "后端信号-网络检查：acli network <sub_command>",
    "qfk.storage_state": "后端信号-存储状态：acli storage <sub_command>",
    "qfk.hardware_state": "后端信号-硬件状态：acli hardware <sub_command>",
    "qfk.platform_state": "后端信号-平台状态：acli platform <sub_command>",
    "qfk.system_metric": "后端信号-系统指标阈值判定：acli system <sub_command>，threshold 求值",
}

# ─── 默认变量池 schema（produces/requires 引用的变量名集合）───────────────────
DEFAULT_VARIABLE_SCHEMA: list[str] = [
    "HOST", "VM", "NODE_IP", "TARGET", "END", "ALERT_TYPE",
    "STATUS", "ERRCODE_TRACING", "REQUEST_ID",
]

VALID_CATEGORIES = {"frontend", "backend"}
VALID_MATCHER_TYPES = {"keyword", "state", "threshold", "json_path", "exists"}

# ADR-2：{{VAR}} 大写占位符正则（单一真相源；运行期校验用）
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\}\}")
# 用于检测"任何双花括号占位符"（含非法大小写）以报错
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^}]*)\}\}")


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
    """校验单条信号：类别/acquirer/占位符/变量引用合法性。"""
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
            validated.append(s)
        else:
            rejected.append({"signal": s, "reason": err})
            logger.warning("extract_signals 信号被丢弃 source=%s reason=%s", source_id, err)
    return validated, rejected


async def _persist_signals(db_manager: DatabaseManager, table: str, source_id: int,
                          signals: list[dict[str, Any]]) -> None:
    """通用写回：signals_json 列。table ∈ {'kbd_entry', 'sop_document'}。"""
    async with db_manager.async_session_factory() as session:
        await session.execute(
            text(f"UPDATE {table} SET signals_json = :sj::jsonb, updated_at = NOW() WHERE id = :id"),
            {"sj": json.dumps(signals, ensure_ascii=False), "id": source_id},
        )
        await session.commit()


async def extract_signals_for_kbd(db_manager: DatabaseManager, kbd_id: int) -> dict[str, Any]:
    """KBD 路径：读取 kbd_entry 8 章节 → LLM 抽取 → 校验 → 写回 signals_json。"""
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
        root_cause=(entry.root_cause or "")[:1500],
        solution=(entry.solution or "")[:1500],
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
async def extract_signals_kbd(request: Request, kbd_id: int) -> ExtractSignalsResponse:
    """关键信号分级抽取（KBD）：从 kbd_entry 自然语言章节抽取结构化 signals_json 并写回。"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    logger.info(event="extract_signals_kbd_request", kbd_id=kbd_id)
    result = await extract_signals_for_kbd(_db_manager, kbd_id)
    return ExtractSignalsResponse(**result)


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
