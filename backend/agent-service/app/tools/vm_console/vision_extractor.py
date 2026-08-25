"""在线 Vision Extractor：把控制台截图派生为结构化视觉观察。

设计文档 §3.5 / §6.2 / §6.3：
- 只描述可观察事实，不给出根因结论；低置信度/冲突/失败降级 unknown/unavailable；
- 合规策略门禁：默认不向视觉模型发图（VM_CONSOLE_VISION_ALLOWED=false →
  VISION_UNAVAILABLE_BY_POLICY），保留图片不绕过策略；
- Langfuse/日志不记录原图、OCR 全文或敏感摘要，只记录制品 ID、哈希、尺寸、
  模型/Prompt 版本、状态、耗时与错误分类。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any

from shared.observability.logger import get_logger
from shared.vision.vm_console_observation import (
    DISPLAY_STATE_VOCABULARY,
    VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION,
    VM_CONSOLE_MODEL_REVISION,
    VM_CONSOLE_PROMPT_REVISION,
    VmConsoleObservation,
    unavailable_observation,
)

logger = get_logger("vm-console-vision")

# 合规策略门禁：默认关闭；开启前必须确认模型端点位于合规区域并具备数据处理授权。
VISION_ALLOWED_ENV = "VM_CONSOLE_VISION_ALLOWED"
# 派生 PNG 的像素/尺寸上限（隔离派生，防止解码炸弹）。
MAX_PNG_PIXELS = 4096 * 4096
MAX_PNG_SIDE = 8192

_OBSERVATION_PROMPT = f"""你是虚拟机控制台画面观察器。只描述画面中**可见**的事实，不推测根因。

display_state 必须从受限词表中选择：{sorted(DISPLAY_STATE_VOCABULARY)}。
- kernel_panic：可见 "Kernel panic" 等内核恐慌文本
- bsod：Windows 蓝屏死机画面
- booting：启动过程画面（GRUB、systemd 启动日志等）
- login_prompt：登录提示符
- desktop：正常桌面
- black_screen：全黑或近黑画面
- installer_error：安装器错误画面
- application_error：应用层错误弹窗/文本
- no_signal：无信号提示
- unknown：无法确定

仅输出 JSON，字段：
{{"display_state": "...", "summary": "一句话可见现象描述", "ocr_text": ["关键可见文本"], "visible_indicators": ["..."], "confidence": 0.0-1.0, "needs_human_review": bool}}

置信度不足、画面模糊或内容冲突时，display_state 必须为 unknown 并降低 confidence。"""


def vision_allowed() -> bool:
    return os.environ.get(VISION_ALLOWED_ENV, "false").lower() in ("1", "true", "yes", "on")


def derive_png_from_ppm(ppm_bytes: bytes) -> tuple[bytes, int, int]:
    """PPM → PNG 受控派生：隔离进程执行，限制像素/边长/超时（§6.2）。"""

    from shared.vision.png_derive import derive_png_isolated

    return derive_png_isolated(ppm_bytes, max_pixels=MAX_PNG_PIXELS, max_side=MAX_PNG_SIDE)


def _parse_model_payload(raw_text: str) -> dict[str, Any] | None:
    """从模型输出提取 JSON；容忍 ```json 围栏。"""

    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        # 退化：截取第一个 {...} 块。
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(text[start : end + 1])
                return payload if isinstance(payload, dict) else None
            except json.JSONDecodeError:
                return None
    return None


async def extract_observation(
    ppm_bytes: bytes,
    *,
    artifact_id: str,
    trace_id: str | None = None,
) -> VmConsoleObservation:
    """执行视觉提取；任何失败都降级为 unavailable，不声称"没有故障"。"""

    start = time.monotonic()
    if not vision_allowed():
        logger.info(
            "vm_console_vision_policy_denied",
            artifact_id=artifact_id,
            trace_id=trace_id,
            reason=VISION_ALLOWED_ENV + " 未开启",
        )
        return unavailable_observation(
            artifact_id, summary="视觉观察不可用：合规策略未允许发送图片到视觉模型（VISION_UNAVAILABLE_BY_POLICY）"
        )

    try:
        png_bytes, width, height = await asyncio.to_thread(derive_png_from_ppm, ppm_bytes)
    except Exception as exc:
        logger.warning("vm_console_vision_derive_failed", artifact_id=artifact_id, error=str(exc))
        return unavailable_observation(artifact_id, summary=f"图片派生失败：{exc}")

    base_url = (os.environ.get("LLM_VISION_BASE_URL") or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
    api_key = os.environ.get("LLM_VISION_API_KEY") or os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("VISION_MODEL", "kimi-k2.5")
    if not base_url or not api_key:
        return unavailable_observation(artifact_id, summary="视觉端点未配置（VISION_UNAVAILABLE_BY_POLICY）")

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        timeout_seconds = float(os.environ.get("LLM_VISION_TIMEOUT", "120.0"))
        image_b64 = base64.b64encode(png_bytes).decode()
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _OBSERVATION_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }
            ],
            timeout=timeout_seconds,
            max_tokens=2048,
        )
        raw_text = response.choices[0].message.content if response.choices else ""
    except Exception as exc:
        logger.warning(
            "vm_console_vision_call_failed",
            artifact_id=artifact_id,
            error=str(exc),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return unavailable_observation(artifact_id, summary="视觉模型调用失败（VISION_UNAVAILABLE_BY_POLICY）")

    payload = _parse_model_payload(raw_text or "")
    if payload is None:
        return unavailable_observation(artifact_id, summary="视觉模型输出无法解析（VISION_UNCERTAIN）")

    # 词表外值在 VmConsoleObservation 校验器内强制降级 unknown。
    try:
        observation = VmConsoleObservation(
            observation_status="observed",
            display_state=str(payload.get("display_state") or "unknown"),
            summary=str(payload.get("summary") or "")[:500],
            ocr_text=[str(item)[:200] for item in (payload.get("ocr_text") or [])][:20],
            visible_indicators=[str(item)[:64] for item in (payload.get("visible_indicators") or [])][:20],
            confidence=float(payload.get("confidence", 0.0)),
            needs_human_review=bool(payload.get("needs_human_review", False)),
            artifact_id=artifact_id,
            model_revision=VM_CONSOLE_MODEL_REVISION,
            prompt_revision=VM_CONSOLE_PROMPT_REVISION,
            display_state_vocabulary_revision=VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION,
        )
    except Exception as exc:
        return unavailable_observation(artifact_id, summary=f"视觉结果 Schema 非法：{exc}")

    # 低置信度降级：不声称确定状态，交由人工/受控重试。
    if observation.confidence < 0.5 and observation.display_state != "unknown":
        observation = observation.model_copy(
            update={"display_state": "unknown", "needs_human_review": True}
        )

    # 可观测性：只记录元数据，绝不记录原图/OCR 全文/敏感摘要。
    logger.info(
        "vm_console_vision_completed",
        artifact_id=artifact_id,
        display_state=observation.display_state,
        confidence=observation.confidence,
        model_revision=observation.model_revision,
        prompt_revision=observation.prompt_revision,
        vocabulary_revision=observation.display_state_vocabulary_revision,
        png_bytes=len(png_bytes),
        width=width,
        height=height,
        duration_ms=int((time.monotonic() - start) * 1000),
        trace_id=trace_id,
    )
    return observation
