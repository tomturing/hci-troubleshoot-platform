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

import copy
import json
import os
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from jsonschema import ValidationError
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.schemas.acquirer_args import SAFE_LOG_FILE_PATTERN, validate_acquire_args
from shared.schemas.signal_generation import build_signal_generation_metadata
from shared.schemas.signal_output import sync_signal_requires
from shared.schemas.signal_schema import validate_signals_json
from shared.utils.prompt_loader import StrictPromptLoader
from sqlalchemy import select, text

from app.services.kbd_mutation_guard import PublishedKbdMutationError, require_mutable_kbd
from app.services.safe_pipeline_converter import (
    SafePipelineConversionError,
    apply_safe_pipeline_to_signal,
    convert_safe_pipeline,
)
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
# 是否启用思维链（与 classify.py / vision_processor.py 统一由 LLM_ENABLE_THINKING 控制，默认关闭。
# 推理模型（glm-5.2 / deepseek-v4-flash）开启 thinking 时会先消耗大量 reasoning token，
# 导致 json_object 正文被挤出 max_tokens 预算 —— 这正是「LLM 响应格式错误」的根因。）
LLM_ENABLE_THINKING = os.environ.get("LLM_ENABLE_THINKING", "false").lower() in ("1", "true", "yes", "on")

# Prompt 名称（system_prompt 表热加载，admin-ui 可在线编辑）
# 2026-07-23：升级为 kbd_extract_signals_v2 —— LLM 直接产出 v2 嵌套结构，
# 移除「v1 扁平 → migrate → v2」的中间归约环节（链路质效提升，详见 PR 说明）。
_EXTRACT_PROMPT_NAME = "kbd_extract_signals_v2"

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
    "qkv_dialog": "弹框复合取值：在当前主控 today 与 today/vt 日志检索弹框文本，产出 END/REQUEST_ID/HOST",
    "qfk_log": (
        "统一日志判定：/sf/log 下 whitebox/blackbox/vn-blackbox/pod 均由 acli log get 获取；"
        "/sf/data/local 仅允许 request_id 辅助关联，"
        "Catalog 推断 path/parser，支持 keyword/regex/state/threshold/delta/trend/exists"
    ),
    "qfk_service": "服务状态：领域含 asv(vt)/anet(vn)/asan(vs)/host；当前运行时按 acli capability probe 执行",
    "qfk_system": "后端信号-系统检查和操作：acli system <command>（如 lsof/ps/lsblk/iostat/smartctl），threshold/keyword/json_path 求值",
    "qfk_vm": "后端信号-虚拟机相关操作：acli vm <command>，state/json_path/exists 求值",
    "qfk_network": "后端信号-网络相关操作：acli network <command>，state/json_path/exists 求值",
    "qfk_storage": "后端信号-存储相关操作：acli storage <command>（如 asan disk list），state/json_path/exists 求值",
    "qfk_hardware": "后端信号-硬件相关操作：acli hardware <command>，state/json_path/exists 求值",
    "qfk_platform": "后端信号-平台相关操作：acli platform <command>，state/json_path/exists 求值",
}

# ─── 默认变量池 schema（produces/requires 引用的变量名集合）───────────────────
DEFAULT_VARIABLE_SCHEMA: list[str] = [
    "HOST",
    "VM",
    "NODE_IP",
    "TARGET",
    "END",
    "ALERT_TYPE",
    "STATUS",
    "ERRCODE_TRACING",
    "REQUEST_ID",
]

VALID_CATEGORIES = {"frontend", "backend"}
VALID_MATCHER_TYPES = {"keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"}
VALID_VARIABLE_TYPES = {"string", "integer", "number", "boolean", "array"}

# ADR-2：{{VAR}} 大写占位符正则（单一真相源；运行期校验用）
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\}\}")
# 用于检测"任何双花括号占位符"（含非法大小写）以报错
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{([^}]*)\}\}")

# 字段级溯源：抽取方法标识（写入每条信号的 provenance.method）
EXTRACTION_METHOD = "llm_field_level_v2"
# 低置信阈值：校准后置信度低于此值标 needs_review（镜像 classify 的 low_confidence，§3）
NEEDS_REVIEW_CONFIDENCE_THRESHOLD = 0.5

# 合法信号来源章节（LLM 自报 source_section 必须落在此集合，否则回退推断）。
# 治本约束：只有「诊断叙事字段」可作为信号来源；根因(root_cause)与解决方案(solution)
# 是抽取 OUTPUT（确认后返回给用户），绝不作为信号抽取输入，避免把"处置动作"误抽成诊断信号。
_VALID_SOURCE_SECTIONS = {
    "title",
    "problem_description",
    "alert_info",
    "steps_text",
}


def _format_image_evidence(images_json: Any, *, max_chars: int = 12000) -> str:
    """将截图 Evidence IR 规整为 Signal Proposal 的只读输入。

    新链路只把 observed_facts/fields/OCR 原文作为可生成信号的事实。模型 DESCRIPTION、
    regions[].inferences 与 legacy desc 在结构上不进入 Signal LLM，只保留质量状态供门禁；
    完整推断仍在 images_json 和管理端可见。这样“推断不得进入运行参数”不是 Prompt 约定，
    而是输入数据最小化形成的硬边界。
    """

    formatted: list[dict[str, Any]] = []
    for item in images_json or []:
        if not isinstance(item, dict) or item.get("seq") is None:
            continue
        seq = int(item["seq"])
        evidence = item.get("evidence")
        if isinstance(evidence, dict):
            regions: list[dict[str, Any]] = []
            for region in evidence.get("regions") or []:
                if not isinstance(region, dict):
                    continue
                region_id = str(region.get("region_id") or f"img_{seq}:r_0")
                regions.append({
                    "source_ref": f"img:{seq}/region:{region_id}",
                    "surface": region.get("surface"),
                    "evidence_type": region.get("evidence_type"),
                    "text_lines": region.get("text_lines") or [],
                    "fields": region.get("fields") or {},
                    "observed_facts": region.get("observed_facts") or [],
                })
            quality = evidence.get("quality") or {}
            formatted.append({
                "source_ref": f"img:{seq}",
                "section": item.get("section") or evidence.get("section") or "steps_text",
                "context_before": item.get("context_before") or evidence.get("context_before") or "",
                "context_after": item.get("context_after") or evidence.get("context_after") or "",
                "regions": regions,
                "quality": {
                    "status": quality.get("status"),
                    "needs_review": quality.get("needs_review", False),
                    "inference_status": quality.get("inference_status"),
                    "inference_needs_review": quality.get("inference_needs_review", False),
                    "inference_issues": quality.get("inference_issues") or [],
                },
            })
        elif (item.get("desc") or "").strip():
            formatted.append({
                "source_ref": f"img:{seq}",
                "section": item.get("section") or "steps_text",
                "legacy_evidence_unavailable": True,
                "quality": {"status": "needs_review", "needs_review": True},
            })
    payload = json.dumps(formatted, ensure_ascii=False, separators=(",", ":"))
    return payload[:max_chars] if payload else "[]"

# ─── 写操作子命令词表（处置/变更动作，不得自动执行）────────────────────────
# 这是"诊断只读"原则的硬边界：凡 command 命中以下动词的后端信号，一律标记为
# phase=solution / require_human_confirm=True，执行层绝不自动运行，必须人工授权。
WRITE_OP_SUB_COMMANDS: set[str] = {
    "start",
    "stop",
    "shutdown",
    "restart",
    "suspend",
    "resume",
    "migrate",
    "clone",
    "reset",
    "reboot",
    "delete",
    "remove",
    "del",
    "rm",
    "format",
    "wipe",
    "destroy",
    "enable",
    "disable",
    "kill",
    "killall",
    "pkill",
    "up",
    "down",
    "set",
    "create",
    "add",
    "modify",
    "update",
}
# 破坏性（不可逆）子命令 → risk=3（block）
_DESTRUCTIVE_SUB_COMMANDS: set[str] = {
    "delete",
    "remove",
    "del",
    "rm",
    "format",
    "wipe",
    "destroy",
}


def _read_signal_fields(signal: dict[str, Any]) -> tuple[str, dict, str, list, list, Any, dict]:
    """从 v2 嵌套信号统一读取结构化字段（单一真相源）。

    v1 扁平中间态（signal_category/acquirer/acquirer_args/matcher/produces/requires
    顶层字段）已彻底下线，本函数只读取 v2 契约的 acquire/orchestrate/provenance/match 段。
    返回 ``(tool, args, category, produces, requires, matcher, provenance)``。
    """
    acquire = signal.get("acquire") or {}
    tool = acquire.get("tool", "")
    args = acquire.get("args") or {}
    provenance = signal.get("provenance") or {}
    orchestrate = signal.get("orchestrate") or {}
    cat = provenance.get("category")
    if cat is None:
        cat = "backend" if str(tool).startswith("qfk_") else "frontend"
    produces = orchestrate.get("produces") or []
    requires = orchestrate.get("requires") or []
    matcher = signal.get("match") or None
    return tool, args, cat, produces, requires, matcher, provenance


def _is_write_op_signal(signal: dict[str, Any]) -> bool:
    """判断一条后端信号是否为写操作/处置动作（基于 acquire.tool + command 词表）。

    仅 backend（qfk_*）信号可能携带写操作；前端生产者（qkv_*）均为只读查询。
    command 形如 'kill -9 240132' / 'ps auxf' / 'start'，按空白与 | / 切词后命中词表即判写操作。
    """
    tool, args, cat, _, _, _, _ = _read_signal_fields(signal)
    if cat != "backend" or not str(tool).startswith("qfk_"):
        return False
    sub = str(args.get("command", "") or "")
    tokens = re.split(r"[\s|/]+", sub.strip())
    return any(tok in WRITE_OP_SUB_COMMANDS for tok in tokens)


def _write_op_risk(signal: dict[str, Any]) -> int:
    """写操作信号的风险等级：破坏性子命令=3(block)，其余写操作=2(confirm)。"""
    _, args, _, _, _, _, _ = _read_signal_fields(signal)
    sub = str(args.get("command", "") or "").lower()
    tokens = set(re.split(r"[\s|/]+", sub))
    if tokens & _DESTRUCTIVE_SUB_COMMANDS:
        return 3
    return 2


def _signal_quality_score(signal: dict[str, Any]) -> float:
    """结构质量分(0-1)：占位符/变量/判定契约合法性。

    经过 _validate_signal 的信号已保证占位符大写、requires 在 schema 内，
    故此处仅对"判定/产出契约完整性"打分，作为置信度校准的结构质量因子。
    """
    _, _, cat, produces, requires, matcher, _ = _read_signal_fields(signal)
    score = 0.3  # 占位符大写 + 变量合法性基础分（已校验通过）
    if cat == "backend":
        score += 0.3 if requires else 0.3  # backend 允许无 requires
        score += 0.4 if (matcher or produces) else 0.0
    else:  # frontend
        score += 0.3 if requires else 0.3
        score += 0.4 if produces else 0.0
    return min(1.0, score)


def _infer_source(signal: dict[str, Any]) -> str:
    """字段级溯源：推断信号主要来自哪个输入章节（provenance.source_section）。

    治本约束：信号只能来自诊断叙事字段（标题/问题描述/告警/有效排查步骤）；
    根因与解决方案不再作为信号来源，故一律回退到 steps_text。
    """
    prov = signal.get("provenance") or {}
    ss = (prov.get("source_section") or "").strip()
    if ss in _VALID_SOURCE_SECTIONS:
        return ss
    return "steps_text"


def _calibrate_confidence(signal: dict[str, Any], quality: float) -> float:
    """字段级置信度校准（基于信号质量与证据充分性）。

    校准 = base × (0.5·结构质量 + 0.5·证据充分性)，并钳制到 [0.05, 0.99]。
    - base：LLM 自评估 confidence（v2 取自 provenance.confidence，遗留取顶层 confidence），缺失取 0.7
    - 证据充分性：backend 必须有 match、frontend 必须有 produces，否则降级
    """
    prov = signal.get("provenance") or {}
    llm_conf = prov.get("confidence")
    try:
        llm_conf = float(llm_conf) if llm_conf is not None else None
    except (TypeError, ValueError):
        llm_conf = None
    if llm_conf is None or not (0.0 <= llm_conf <= 1.0):
        llm_conf = 0.7

    _, _, cat, produces, requires, matcher, _ = _read_signal_fields(signal)
    evidence = (1.0 if (matcher or produces) else 0.3) if cat == "backend" else 1.0 if produces else 0.4

    calibrated = llm_conf * (0.5 * quality + 0.5 * evidence)
    return round(max(0.05, min(0.99, calibrated)), 3)


# ─── 说明/关键字 兜底纠错（根治历史 bug：LLM 把"信号说明"错填进 resource_keyword）────
# 历史上 LLM 易把检查动作的自然语言标题（如「镜像文件占用检查」）写进 resource_keyword
# （UI 的"关键字"字段），导致 instruction 留空、说明错显为关键字。此处做防御性兜底：
# 当 acquire.args.instruction 缺失、而 resource_keyword 读起来像说明性长句时，迁回 instruction。
_DESCRIPTION_VERBS = frozenset({
    "检查", "确认", "占用", "查看", "判断", "核对", "检测", "查询", "分析",
    "定位", "获取", "导出", "验证", "排查", "统计", "罗列", "列举",
})


def _looks_descriptive(text: Any) -> bool:
    """判断一段文本是否像自然语言说明（含检查/确认/占用等动作动词），而非资源标识符。"""
    if not isinstance(text, str) or not text.strip():
        return False
    return any(v in text for v in _DESCRIPTION_VERBS)


def _clean_signal_description(signal: dict[str, Any]) -> None:
    """兜底纠错（就地修改）：把错填进"关键字"字段的说明性文本迁回 instruction。

    覆盖两类 LLM 错填（均把动作标题/说明误当关键字）：
    1) ``resource_keyword`` 实为说明（日志/服务类 QFK 的"资源/主题"字段）：
       迁回 instruction 并清空（可选字段，清空不破坏 v2 契约）。
    2) ``match.pattern`` 实为说明性长句（QFK 的"关键字"字段；qfk_system 无 resource_keyword
       时 LLM 易把"镜像文件占用检查"类动作标题写进 match.pattern）：迁回 instruction，
       pattern 清空并标记 ``provenance.needs_review=True``，交由人工补精确匹配串
       （精确串无法凭空反推，故不臆造，仅兜底纠位 + 标记复核）。

    说明：本函数在 ``_validate_signal`` 之后、落库之前执行；resource_keyword 此时已能通过
    strict schema 校验，故此处仅负责纠错（含 qfk_system 因 schema 对齐后纳入的 resource_keyword）。
    """
    acquire = signal.get("acquire") or {}
    args = acquire.get("args") or {}
    desc = (args.get("instruction") or "").strip()

    # 1) resource_keyword 实为说明 → 迁回 instruction
    if not desc:
        rk = args.get("resource_keyword")
        if _looks_descriptive(rk):
            args["instruction"] = str(rk).strip()
            args.pop("resource_keyword", None)
            desc = args["instruction"]
            logger.warning("extract_signals 兜底纠错：resource_keyword 实为信号说明，已迁移至 instruction: %s", rk)

    # 2) match.pattern 实为说明性长句 → 迁回 instruction（QFK 的"关键字"字段）
    #    仅当 instruction 仍缺失时处理；短匹配串（如"镜像占用"/"docker"/"overlay2"）即便含
    #    动词也视为真实匹配串，绝不误清空；仅「含动作动词 且 长度≥6」的说明性长句才迁移，
    #    并清空 pattern、标记 needs_review=True，交由人工补精确匹配串。
    if not desc:
        matcher = signal.get("match")
        if isinstance(matcher, dict):
            pattern = matcher.get("pattern")
            if (
                isinstance(pattern, str)
                and _looks_descriptive(pattern)
                and len(pattern.strip()) >= 6
            ):
                args["instruction"] = pattern.strip()
                matcher["pattern"] = ""
                signal.setdefault("provenance", {})["needs_review"] = True
                logger.warning(
                    "extract_signals 兜底纠错：match.pattern 实为信号说明，已迁移至 instruction 并标记 needs_review: %s",
                    pattern,
                )


def _enrich_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """为单条 v2 信号补充字段级溯源与置信度校准，全部写入 v2 段（不产生顶层冗余字段）。

    写入目标（均落于 v2 契约允许字段，保证保存时 validate_signals_json 通过）：
    - provenance.method: 抽取方法标识（固定 llm_field_level_v2）
    - provenance.source_section: 主要来源章节（字段级溯源）
    - provenance.confidence: 校准后置信度(0-1)
    - provenance.needs_review: 低置信/歧义标 needs_review
    - provenance.category: 角色（frontend/backend，缺失时按 tool 派生）
    - provenance.risk: 风险等级 1/2/3
    - review.require_human_confirm: 是否必须人工授权后才可执行
    - orchestrate.phase: diagnostic(诊断只读) / solution(处置动作)
    """
    # 说明/关键字 兜底纠错（先于溯源/置信度注入，确保落库的 instruction 正确）
    _clean_signal_description(signal)

    quality = _signal_quality_score(signal)
    acquire = signal.setdefault("acquire", {})
    tool = acquire.get("tool", "")
    provenance = signal.setdefault("provenance", {})
    orchestrate = signal.setdefault("orchestrate", {})
    review = signal.setdefault("review", {})

    # category 派生补齐（v2 契约要求枚举值）
    cat = provenance.get("category") or ("backend" if str(tool).startswith("qfk_") else "frontend")
    provenance["category"] = cat

    # 字段级溯源
    provenance["method"] = EXTRACTION_METHOD
    provenance["source_section"] = _infer_source(signal)

    # 置信度校准
    provenance["confidence"] = _calibrate_confidence(signal, quality)

    # 低置信/歧义信号标 needs_review（镜像 classify 的 low_confidence，§3）；
    # 同时保留 _clean_signal_description 在迁移 match.pattern 时标记的 needs_review（人工补精确匹配串）
    needs_review = bool(provenance["confidence"] < NEEDS_REVIEW_CONFIDENCE_THRESHOLD) or bool(
        provenance.get("needs_review")
    )
    provenance["needs_review"] = needs_review

    # 写操作/处置动作信号：标记为人⼯授权，绝不自动执行（诊断只读原则）
    if _is_write_op_signal(signal):
        orchestrate["phase"] = "solution"
        review["require_human_confirm"] = True
        provenance["risk"] = _write_op_risk(signal)
    else:
        # 人工确认是执行授权策略，不等价于处置动作。只读诊断即使需要确认，
        # 仍必须保留在诊断证据图中；只有确定性写操作词表可以把 phase 升为 solution。
        orchestrate["phase"] = orchestrate.get("phase", "diagnostic")
        review.setdefault("require_human_confirm", False)
        provenance["risk"] = provenance.get(
            "risk", 2 if review.get("require_human_confirm") else 1
        )

    if "id" not in signal:
        signal["id"] = "sig_001"
    return signal


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
    """校验单条 v2 嵌套信号。

    写操作拦截：对 backend（qfk_*）信号，若 acquire.args.command 命中写操作词表，
    则就地标记 review.require_human_confirm=True / orchestrate.phase=solution / provenance.risk，
    使其被排除在自动执行之外，交由人工授权。
    v1 扁平格式（acquirer/acquirer_args/signal_category/matcher 顶层字段）已彻底下线，
    缺失 acquire 段的信号将被直接拒绝（不再经 migrate 兜底归一）。
    """
    acquire = signal.get("acquire") or {}
    tool = acquire.get("tool")
    if not tool:
        return False, "信号缺少 acquire.tool 段"
    if tool not in ACQUIRER_CATALOG:
        return False, f"acquire.tool 不在采集器词表内: {tool}"

    # ── v2 契约校验（RFC §4.4 / §6.1）：acquire.args 机器强制门禁 ──
    args = acquire.get("args") or {}
    ok, err = validate_acquire_args(tool, args)
    if not ok:
        return False, f"acquire.args 校验失败: {err}"

    # category
    prov = signal.get("provenance") or {}
    cat = prov.get("category")
    if cat is None:
        cat = "backend" if str(tool).startswith("qfk_") else "frontend"
    if cat not in VALID_CATEGORIES:
        return False, f"provenance.category 非法: {cat}"

    # produces/requires 变量命名与可见性（均取自 v2 orchestrate 段）
    orchestrate = signal.get("orchestrate") or {}
    produces = orchestrate.get("produces") or []
    requires = orchestrate.get("requires") or []

    # 占位符大写校验（acquire.args + 文本提取条件）
    try:
        _validate_obj_placeholders(args)
        _validate_obj_placeholders(produces)
    except ValueError as e:
        return False, str(e)

    for p in produces:
        name = p.get("name", "")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            return False, f"produces 变量名非全大写: {name}"
    for r in requires:
        if r not in available_vars:
            return False, f"requires 变量不在 schema 内: {r}"

    # backend 的判定与产出变量严格二选一；写操作拦截
    matcher = signal.get("match")
    if cat == "backend":
        has_match = isinstance(matcher, dict)
        has_produces = any(isinstance(item, dict) and item.get("name") for item in produces)
        if has_match == has_produces:
            return False, "backend 信号必须且只能配置 match 或 orchestrate.produces 之一"
        if has_match and matcher.get("type") not in VALID_MATCHER_TYPES:
            return False, f"backend 信号 match.type 非法: {matcher.get('type')}"
        if _is_write_op_signal(signal):
            signal.setdefault("review", {})["require_human_confirm"] = True
            signal.setdefault("orchestrate", {})["phase"] = "solution"
            signal.setdefault("provenance", {})["risk"] = _write_op_risk(signal)
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
    """调用 LLM API（json_object 模式），返回解析后的 dict。

    2026-07-23 修复：原 max_tokens=2000 对推理模型（glm-5.2 / deepseek-v4-flash）不足——
    思维链 reasoning_content 会先耗尽 token 预算，使 message.content 为空、
    json.loads("") 报「LLM 响应格式错误」。现统一：
      1) 对齐 classify.py / vision_processor.py，显式关闭思维链（enable_thinking=false）；
      2) max_tokens 提升至 8192，为长 KBD 文档 / 长输出预留余量；
      3) 对空 content 做显式防御，避免把「被 token 截断」暴露成模糊的 JSON 解析失败。
    """
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
            max_tokens=8192,
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": LLM_ENABLE_THINKING},
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            # 防御：模型在 max_tokens 内仅产出思维链、未给出 JSON 正文（finish_reason=length）
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            logger.error(
                "extract_signals LLM 未返回 JSON 正文: finish_reason=%s model=%s",
                finish_reason,
                LLM_MODEL,
            )
            raise HTTPException(
                status_code=502,
                detail=f"LLM 未返回有效 JSON 内容（finish_reason={finish_reason}，可能因思维链耗尽 token 预算）",
            )
        return json.loads(content)
    except json.JSONDecodeError as e:
        logger.error("extract_signals LLM 响应 JSON 解析失败: %s", e)
        raise HTTPException(status_code=500, detail="LLM 响应格式错误") from e
    except Exception as e:
        logger.error("extract_signals LLM 调用失败: %s", e)
        raise HTTPException(status_code=503, detail=f"LLM API 调用失败: {e}") from e


def _normalize_contract_variables(proposed: Any) -> dict[str, dict[str, str]]:
    """只接受显式大写变量名和封闭类型，拒绝 LLM 自造变量元数据。"""
    if not isinstance(proposed, dict) or not isinstance(proposed.get("variables"), dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for name, spec in proposed["variables"].items():
        variable_name = str(name).strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", variable_name) or not isinstance(spec, dict):
            continue
        variable_type = str(spec.get("type") or "")
        if variable_type not in VALID_VARIABLE_TYPES:
            continue
        normalized[variable_name] = {"type": variable_type}
        if isinstance(spec.get("description"), str) and spec["description"].strip():
            normalized[variable_name]["description"] = spec["description"].strip()
    return normalized


def _normalize_config_file_read(signal: dict[str, Any]) -> bool:
    """把误路由到 ``qfk_log`` 的只读 ``/sf/cfg`` 文件采集转为 system cat。

    ``qfk_log`` 的最小权限边界只能覆盖日志目录，不能为了读取配置文件而放宽
    ``ALLOWED_LOG_PATH_PREFIXES``。但 HCI 案例经常把 ``cat /sf/cfg/*.ini`` 描述成
    “查看文件”，模型因文件后缀而误选日志工具。这里仅规范化满足以下全部条件的
    明确只读意图：

    - 工具是 ``qfk_log``；
    - path 位于 ``/sf/cfg`` 内，且没有 ``.``/``..`` 或非法路径段；
    - file 是安全 basename；
    - 没有日志专属的 time_window/resource_keyword 语义。

    其他越界路径继续由共享参数契约拒绝，不能借此把任意文件读取伪装成日志采集。
    返回值用于测试和审计，调用方仍原地消费规范化后的 signal。
    """

    acquire = signal.get("acquire")
    if not isinstance(acquire, dict) or acquire.get("tool") != "qfk_log":
        return False
    args = acquire.get("args")
    if not isinstance(args, dict) or args.get("time_window") or args.get("resource_keyword"):
        return False

    path = str(args.get("path") or "").rstrip("/")
    file_name = str(args.get("file") or "")
    if path != "/sf/cfg" and not path.startswith("/sf/cfg/"):
        return False
    path_parts = path.split("/")[3:]
    if any(
        part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
        for part in path_parts
    ):
        return False
    if file_name in {"", ".", ".."} or not re.fullmatch(SAFE_LOG_FILE_PATTERN, file_name):
        return False

    system_args = {
        key: args[key]
        for key in ("host", "timeout", "instruction")
        if key in args
    }
    system_args.update({"command": "cat", "resource_keyword": f"{path}/{file_name}"})
    acquire["tool"] = "qfk_system"
    acquire["args"] = system_args
    review = signal.setdefault("review", {})
    if isinstance(review, dict):
        note = "确定性工具路由：/sf/cfg 安全配置文件只读采集由 qfk_log 归一为 qfk_system cat"
        previous = str(review.get("notes") or "").strip()
        review["notes"] = f"{previous}；{note}" if previous else note
    return True


def _normalize_derived_file_assertions(raw_signals: list[Any]) -> int:
    """让 matcher 复用产出文件内容的只读 acquisition，而不是 ``cat`` 内容变量。

    LLM 有时先用 ``cat /safe/file`` 产出 ``CONFIG_TEXT``，随后又生成
    ``cat {{CONFIG_TEXT}}`` 来做关键字判定。后者在结构和变量 DAG 上都合法，但把
    “文件内容”误当成“文件路径”，现场必然不可执行。对于同一候选集合内可证明来源
    唯一的 ``qfk_system cat`` 产出，本函数把下游 matcher 的 acquisition 参数替换为
    上游只读参数。Compiler 随后会把它们合并成一次采集，并分别运行 matcher。

    只处理 resource_keyword 为单一 ``{{VAR}}``、上游是明确 ``cat`` 且下游有 matcher
    的封闭形态；其他变量语义不做猜测。返回规范化数量供测试和审计。
    """

    producers: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for signal in raw_signals:
        if not isinstance(signal, dict):
            continue
        acquire = signal.get("acquire") or {}
        args = acquire.get("args") or {}
        if acquire.get("tool") != "qfk_system" or str(args.get("command") or "").strip() != "cat":
            continue
        resource = str(args.get("resource_keyword") or "")
        if not resource or _PLACEHOLDER_RE.fullmatch(resource):
            continue
        for produced in (signal.get("orchestrate") or {}).get("produces") or []:
            name = str(produced.get("name") or "") if isinstance(produced, dict) else ""
            if not name:
                continue
            if name in producers:
                ambiguous.add(name)
            else:
                producers[name] = copy.deepcopy(args)

    normalized = 0
    for signal in raw_signals:
        if not isinstance(signal, dict) or not isinstance(signal.get("match"), dict):
            continue
        acquire = signal.get("acquire") or {}
        args = acquire.get("args") or {}
        if acquire.get("tool") != "qfk_system" or str(args.get("command") or "").strip() != "cat":
            continue
        match = _PLACEHOLDER_RE.fullmatch(str(args.get("resource_keyword") or ""))
        variable = match.group(1) if match else ""
        if not variable or variable in ambiguous or variable not in producers:
            continue
        acquire["args"] = copy.deepcopy(producers[variable])
        review = signal.setdefault("review", {})
        if isinstance(review, dict):
            note = f"确定性采集复用：matcher 直接复用 {variable} 的安全 cat acquisition"
            previous = str(review.get("notes") or "").strip()
            review["notes"] = f"{previous}；{note}" if previous else note
        normalized += 1
    return normalized


def _validate_and_collect_signals(
    raw_signals: list,
    source_id: Any,
    external_variables: set[str] | None = None,
) -> tuple[list, list]:
    """对 LLM 返回的 v2 信号列表做校验，返回 (validated, rejected)。

    v2 直出：LLM 直接产出 v2 嵌套结构，链路全程以 v2 契约处理。
    仅接受 v2 嵌套格式（含 acquire 段）；旧 v1 扁平格式（含 signal_category 而无 acquire 段）
    已彻底下线，此类输入将被直接拒绝。

    Args:
        raw_signals: LLM 返回的 signal dict 列表
        source_id: 来源标识（用于日志：kbd_id / sop_id）
    """
    # 先在整个 Candidate 集合上做跨信号规范化，再逐条校验。这使下游
    # matcher 可以复用上游安全采集，也避免放宽 qfk_log 的 path 边界。
    for signal in raw_signals:
        if isinstance(signal, dict):
            _normalize_config_file_read(signal)
    _normalize_derived_file_assertions(raw_signals)

    available_vars = set(DEFAULT_VARIABLE_SCHEMA) | set(external_variables or set())
    for s in raw_signals:
        if isinstance(s, dict):
            orch = s.get("orchestrate") or {}
            for p in orch.get("produces") or []:
                name = p.get("name", "")
                if re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                    available_vars.add(name)

    validated: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for s in raw_signals:
        if not isinstance(s, dict):
            rejected.append({"signal": s, "reason": "信号非对象"})
            continue
        if "acquire" not in s:
            rejected.append({"signal": s, "reason": "信号缺少 acquire 段（v1 扁平格式已不再支持）"})
            continue
        try:
            apply_safe_pipeline_to_signal(s)
            sync_signal_requires(s)
        except SafePipelineConversionError as exc:
            s.setdefault("provenance", {})["needs_review"] = True
            s.setdefault("review", {})["notes"] = str(exc)
            rejected.append({"signal": s, "reason": str(exc)})
            logger.warning("extract_signals 安全管道转换失败 source=%s reason=%s", source_id, exc)
            continue
        ok, err = _validate_signal(s, available_vars)
        if ok:
            # 字段级溯源 + 置信度校准（写入 v2 段：provenance.* / review.* / orchestrate.*）
            enriched = _enrich_signal(s)
            try:
                validate_signals_json({"schema_version": 2, "signals": [enriched]})
            except ValidationError as exc:
                rejected.append({"signal": enriched, "reason": f"v2 契约校验失败: {exc.message}"})
                logger.warning("extract_signals 信号被契约拒绝 source=%s reason=%s", source_id, exc.message)
                continue
            validated.append(enriched)
        else:
            rejected.append({"signal": s, "reason": err})
            logger.warning("extract_signals 信号被丢弃 source=%s reason=%s", source_id, err)

    return validated, rejected


def _build_verification_contract(
    signals: list[dict[str, Any]],
    proposed: Any,
    *,
    case_id: str,
) -> dict[str, Any] | None:
    """把 LLM 的证据策略 Proposal 收敛为封闭、可编译的最小案例契约。"""

    if not signals:
        return None
    known_ids = {str(signal.get("id")) for signal in signals if signal.get("id")}
    by_role: dict[str, list[str]] = {role: [] for role in ("must", "should", "exclude", "context")}
    for signal in signals:
        signal_id = str(signal.get("id") or "")
        role = str(signal.get("role") or "").lower()
        phase = str((signal.get("orchestrate") or {}).get("phase") or "diagnostic")
        # 处置信号永远只是案例上下文，不能成为确认根因所需的 must/should/exclude。
        if phase == "solution":
            role = "context"
        elif role not in by_role:
            role = "must"
        if signal_id:
            by_role[role].append(signal_id)

    proposed = proposed if isinstance(proposed, dict) else {}
    policy = proposed.get("evidence_policy") if isinstance(proposed.get("evidence_policy"), dict) else {}
    assigned: set[str] = set()
    normalized: dict[str, list[str]] = {}
    for role in ("must", "should", "exclude", "context"):
        requested = policy.get(role)
        source = requested if isinstance(requested, list) else by_role[role]
        values = []
        for item in source:
            signal_id = str(item)
            if signal_id in known_ids and signal_id not in assigned:
                values.append(signal_id)
                assigned.add(signal_id)
        normalized[role] = values
    # 未被 Proposal 覆盖的诊断信号按信号自身 role/保守 must 归入契约，禁止静默丢证据。
    for role in ("must", "should", "exclude", "context"):
        for signal_id in by_role[role]:
            if signal_id not in assigned:
                normalized[role].append(signal_id)
                assigned.add(signal_id)
    if not normalized["must"]:
        # 自动诊断没有必要证据就不能确认；选择首个非 solution 信号作为保守 must。
        fallback = next(
            (
                str(signal.get("id"))
                for signal in signals
                if signal.get("id") and str(signal.get("id")) not in normalized["context"]
            ),
            None,
        )
        if fallback:
            for role in ("should", "exclude"):
                if fallback in normalized[role]:
                    normalized[role].remove(fallback)
            normalized["must"].append(fallback)

    try:
        minimum_should = max(0, int(policy.get("minimum_should", 0)))
    except (TypeError, ValueError):
        minimum_should = 0
    minimum_should = min(minimum_should, len(normalized["should"]))
    scope = proposed.get("scope") if isinstance(proposed.get("scope"), dict) else {}
    allowed_scope = {
        key: list(value)
        for key, value in scope.items()
        if key in {"products", "versions", "components", "topology_constraints"}
        and isinstance(value, list)
    }
    variables = _normalize_contract_variables(proposed)
    produced = {
        str(item.get("name"))
        for signal in signals
        if str((signal.get("orchestrate") or {}).get("phase") or "diagnostic") != "solution"
        for item in ((signal.get("orchestrate") or {}).get("produces") or [])
        if isinstance(item, dict) and item.get("name")
    }
    required = {
        str(name)
        for signal in signals
        for name in ((signal.get("orchestrate") or {}).get("requires") or [])
        if str(name).strip()
    }
    # 平台内建 env_context 变量仅在确实被引用且没有 Producer 时进入 Contract；
    # 自定义变量必须由 Proposal 显式声明类型，否则前面的 Signal 校验会拒绝。
    for name in sorted(required - produced):
        if name in DEFAULT_VARIABLE_SCHEMA:
            variables.setdefault(name, {"type": "string"})
    return {
        "schema_version": 1,
        "case_id": case_id,
        "scope": allowed_scope,
        "variables": variables,
        "evidence_policy": {
            **normalized,
            "minimum_should": minimum_should,
            "on_missing_must": "inconclusive",
        },
    }


def _signals_to_v2(
    signals: list[dict[str, Any]],
    verification_contract: dict[str, Any] | None = None,
    generation_metadata: dict[str, Any] | None = None,
    rejected_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """持久化为 v2 数组级文档（RFC §7）：{schema_version, signals}。

    v2 直出路径下，signals 已是通过 _validate_signal 校验、并经 _enrich_signal 写入
    v2 衍生段（provenance/review/orchestrate）的嵌套结构，无需再做 migrate 兜底归一。
    """
    doc: dict[str, Any] = {"schema_version": 2, "signals": signals}
    if rejected_candidates:
        doc["rejected_candidates"] = [
            {
                "candidate": item.get("signal"),
                "reason": str(item.get("reason") or "未提供拒绝原因"),
            }
            for item in rejected_candidates
        ]
    if verification_contract is not None:
        doc["verification_contract"] = verification_contract
    if generation_metadata is not None:
        doc["generation_metadata"] = generation_metadata
    return doc


async def _persist_signals(
    db_manager: DatabaseManager,
    table: str,
    source_id: int,
    signals: list[dict[str, Any]],
    verification_contract: dict[str, Any] | None = None,
    generation_metadata: dict[str, Any] | None = None,
    rejected_candidates: list[dict[str, Any]] | None = None,
) -> None:
    """通用写回：signals_json 列。table ∈ {'kbd_entry', 'sop_document'}。

    持久化形态为 v2 数组级文档（RFC §7）：{schema_version, signals}。
    LLM 已直出 v2，抽取链路全程以 v2 契约处理；`_signals_to_v2` 直接包装 v2 文档。
    """
    doc = _signals_to_v2(
        signals,
        verification_contract,
        generation_metadata,
        rejected_candidates,
    )
    validate_signals_json(doc)
    async with db_manager.async_session_factory() as session:
        if table == "kbd_entry":
            entry = await require_mutable_kbd(session, source_id, for_update=True)
            entry.signals_json = doc
        else:
            await session.execute(
                text(f"UPDATE {table} SET signals_json = CAST(:sj AS jsonb), updated_at = NOW() WHERE id = :id"),
                {"sj": json.dumps(doc, ensure_ascii=False), "id": source_id},
            )
        await session.commit()


async def extract_signals_for_kbd(db_manager: DatabaseManager, kbd_id: int) -> dict[str, Any]:
    """KBD 路径：读取 kbd_entry 章节 → LLM 抽取 → 校验 → 写回 signals_json。

    治本约束：抽取输入仅含「诊断叙事字段」（title/problem_description/alert_info/steps_text）。
    root_cause 与 solution 是抽取 OUTPUT（确认后返回给用户），不再作为信号抽取来源，
    从根上杜绝把"处置动作"（如 acli vm start / kill -9）误抽成诊断信号。
    """

    async with db_manager.async_session_factory() as session:
        try:
            entry = await require_mutable_kbd(session, kbd_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishedKbdMutationError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "KBD_MAINTENANCE_WORKING_REQUIRED", "message": str(exc)},
            ) from exc

        # StrictPromptLoader 会审计并 commit；在此之前复制所有字段，避免 session 关闭后
        # 访问 expired ORM 属性触发 async lazy-load / MissingGreenlet。
        entry_data = {
            "title": entry.title or "",
            "problem_description": entry.problem_description or "",
            "alert_info": entry.alert_info or "",
            "steps_text": entry.steps_text or "",
            "category_id": entry.category_id or entry.ai_category_id or "",
            "images_json": [
                dict(item) for item in (entry.images_json or []) if isinstance(item, dict)
            ],
        }

        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            _EXTRACT_PROMPT_NAME,
            [
                "title",
                "problem_description",
                "alert_info",
                "steps_text",
                "root_cause",
                "solution",
                "category_id",
                "acquirer_catalog",
                "variable_schema",
                "image_evidence",
            ],
            consumer="kb-service.extract_signals.kbd",
        )

    acquirer_catalog_text = "\n".join(f"- {k}: {v}" for k, v in ACQUIRER_CATALOG.items())
    variable_schema_text = ", ".join(DEFAULT_VARIABLE_SCHEMA)
    image_evidence_text = _format_image_evidence(entry_data["images_json"])
    prompt = prompt_template.format(
        title=entry_data["title"],
        problem_description=entry_data["problem_description"][:2000],
        alert_info=entry_data["alert_info"][:1000],
        steps_text=entry_data["steps_text"][:4000],
        # 治本：根因/解决方案不参与信号抽取，置空传入（模板占位符仍存在，避免 StrictPromptLoader 校验失败）
        root_cause="",
        solution="",
        category_id=entry_data["category_id"],
        acquirer_catalog=acquirer_catalog_text,
        variable_schema=variable_schema_text,
        image_evidence=image_evidence_text,
    )

    llm_result = await _call_llm(prompt)
    raw_signals = llm_result.get("signals", [])
    if not isinstance(raw_signals, list):
        raise HTTPException(status_code=500, detail="LLM 未返回 signals 数组")
    proposed_contract = llm_result.get("verification_contract")
    external_variables = set(_normalize_contract_variables(proposed_contract))
    validated, rejected = _validate_and_collect_signals(
        raw_signals,
        f"kbd:{kbd_id}",
        external_variables,
    )
    verification_contract = _build_verification_contract(
        validated,
        proposed_contract,
        case_id=str(kbd_id),
    )
    generation_metadata = build_signal_generation_metadata(
        source=entry_data,
        prompt_template=prompt_template,
        model_id=LLM_MODEL,
    )

    try:
        await _persist_signals(
            db_manager,
            "kbd_entry",
            kbd_id,
            validated,
            verification_contract,
            generation_metadata,
            rejected,
        )
    except PublishedKbdMutationError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "KBD_MAINTENANCE_WORKING_REQUIRED", "message": str(exc)},
        ) from exc
    logger.info(
        event="extract_signals_kbd_done",
        kbd_id=kbd_id,
        total=len(raw_signals),
        validated=len(validated),
        rejected=len(rejected),
    )
    return {
        "success": True,
        "kbd_id": kbd_id,
        "signals_count": len(validated),
        "rejected_count": len(rejected),
        "signals": validated,
        "rejected": rejected,
        "verification_contract": verification_contract,
        "generation_metadata": generation_metadata,
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
        var_names = (
            sorted({(v.get("name") if isinstance(v, dict) else None) or "" for v in var_schema})
            if var_schema
            else DEFAULT_VARIABLE_SCHEMA
        )
        # 与 KBD 共用 schema（追加 SOP 已声明的变量名）
        merged_vars = sorted(
            set(DEFAULT_VARIABLE_SCHEMA) | {n for n in var_names if re.fullmatch(r"[A-Z][A-Z0-9_]*", n)}
        )

        prompt_template = await StrictPromptLoader.load_and_validate(
            session,
            _EXTRACT_PROMPT_NAME,
            [
                "title",
                "problem_description",
                "alert_info",
                "steps_text",
                "root_cause",
                "solution",
                "category_id",
                "acquirer_catalog",
                "variable_schema",
                "image_evidence",
            ],
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
        image_evidence="[]",
    )

    llm_result = await _call_llm(prompt)
    raw_signals = llm_result.get("signals", [])
    if not isinstance(raw_signals, list):
        raise HTTPException(status_code=500, detail="LLM 未返回 signals 数组")
    validated, rejected = _validate_and_collect_signals(raw_signals, f"sop:{sop_id}")

    # SOP 执行由 tree_json 决策树拥有裁决契约，不生成 KBD Case Verification Contract。
    await _persist_signals(
        db_manager,
        "sop_document",
        sop_id,
        validated,
        rejected_candidates=rejected,
    )
    logger.info(
        event="extract_signals_sop_done",
        sop_id=sop_id,
        total=len(raw_signals),
        validated=len(validated),
        rejected=len(rejected),
    )
    return {
        "success": True,
        "sop_id": sop_id,
        "signals_count": len(validated),
        "rejected_count": len(rejected),
        "signals": validated,
        "rejected": rejected,
    }


class ExtractSignalsResponse(BaseModel):
    success: bool
    kbd_id: int | None = None
    sop_id: int | None = None
    signals_count: int
    rejected_count: int = 0
    signals: list[dict[str, Any]] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    verification_contract: dict[str, Any] | None = None


class SafePipelineRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)


class SafePipelineResponse(BaseModel):
    command: str
    extract: dict[str, Any]
    removed_segments: list[str] = Field(default_factory=list)


@router.post("/kbd/tools/convert-safe-pipeline", response_model=SafePipelineResponse)
async def convert_safe_pipeline_api(request: Request, body: SafePipelineRequest) -> SafePipelineResponse:
    """预览 grep/awk/cut 安全子集到结构化文本提取规则的转换，不执行命令。"""
    _check_auth(request)
    try:
        result = convert_safe_pipeline(body.command)
    except SafePipelineConversionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SafePipelineResponse(
        command=result.command,
        extract=result.extract,
        removed_segments=result.removed_segments,
    )


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
