"""
关键信号抽取路由（Signal Service）

提供 KBD/SOP 文本的关键信号抽取 HTTP 接口，供 kb-service 远程调用：
  POST /api/v1/signals/extract  { text, mode: "single" | "batch" }  → list[signal_dict]

设计要点：
- Prompt 由 system_prompt 表按 {text} 占位符加载（StrictPromptLoader），
  每次请求实时读取 → 管理员在 admin-ui 修改 Prompt 后下一次调用即生效（热加载）。
- LLM 客户端来自 AgentRouter 启动时注入的 AIAssistantRegistry（与推理引擎共用）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from shared.clients import AIAssistantRegistry
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.utils.prompt_loader import (
    PromptLoadError,
    PromptValidationError,
    StrictPromptLoader,
)

from app.tools.signal.base import KeySignal
from app.tools.signal.extractor import SignalExtractionError, SignalExtractor

router = APIRouter(prefix="/api/v1/signals", tags=["signals"])
logger = get_logger("signal-service")

# 由 main.py lifespan 注入
_db_manager: DatabaseManager | None = None
_ai_registry: AIAssistantRegistry | None = None


def set_signal_dependencies(db: DatabaseManager, ai_registry: AIAssistantRegistry) -> None:
    """在应用启动时注入数据库管理器与 AI 注册表（lifespan 调用）"""
    global _db_manager, _ai_registry
    _db_manager = db
    _ai_registry = ai_registry


def _check_internal_auth(request: Request) -> None:
    """验证内部服务 Token（防止越权访问）"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


class SignalExtractRequest(BaseModel):
    """信号抽取请求体"""

    text: str
    mode: str = "batch"  # single | batch
    prompt_name: str | None = None  # 显式指定 SIG 子 Prompt（前端/后端分别抽取）


@router.post("/extract")
async def extract_signals(request: Request, body: SignalExtractRequest) -> list[dict[str, Any]]:
    """从自然语言文本抽取关键信号（multiple signals 数组）"""
    _check_internal_auth(request)

    if not _db_manager:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    if not _ai_registry:
        raise HTTPException(status_code=503, detail="AI 注册表未就绪")

    client = _ai_registry.get_client()
    if client is None:
        raise HTTPException(status_code=503, detail="AI 助手客户端未注册")

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")

    mode = body.mode if body.mode in ("single", "batch") else "batch"
    # 显式指定 prompt_name（前端/后端细分）时优先；否则按 mode 回退
    prompt_name = body.prompt_name or (
        SignalExtractor.BATCH_PROMPT_NAME if mode == "batch" else SignalExtractor.PROMPT_NAME
    )
    # 指定了具体子 Prompt 或 batch 模式，统一按数组返回（前端/后端 Prompt 均输出数组）
    use_batch = body.prompt_name is not None or mode == "batch"

    # 每次请求从 system_prompt 表加载 Prompt（热加载：编辑即生效）
    try:
        async with _db_manager.async_session_factory() as session:
            prompt_template = await StrictPromptLoader.load_and_validate(
                session, prompt_name, ["text"]
            )
    except (PromptLoadError, PromptValidationError) as e:
        logger.error("signal_prompt_load_failed", prompt_name=prompt_name, error=str(e))
        raise HTTPException(status_code=500, detail=f"加载信号抽取 Prompt 失败: {e}") from e

    try:
        if use_batch:
            signals: list[KeySignal] = await SignalExtractor.extract_batch_from_text(
                body.text, prompt_template=prompt_template, client=client
            )
        else:
            sig = await SignalExtractor.extract_from_text(
                body.text, prompt_template=prompt_template, client=client
            )
            signals = [sig]
    except SignalExtractionError as e:
        raise HTTPException(status_code=502, detail=f"信号抽取失败: {e}") from e

    return [s.extract() for s in signals]
