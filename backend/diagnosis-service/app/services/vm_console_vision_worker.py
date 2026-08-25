"""离线 vm_console_capture 证据包的视觉派生 Worker（设计文档 §3.4/§9.2）。

验包通过后在隔离 Worker 中执行：PPM → 受控 PNG 派生 → 视觉模型 → 派生
Evidence Item。派生产物以原始证据包、文件路径和 SHA-256 建立不可变关联；
**绝不回写或篡改客户上传的证据包**。

策略门禁与在线路径同源：默认不向视觉模型发图（VM_CONSOLE_VISION_ALLOWED），
拒绝时生成 unavailable 观察，保留图片不绕过策略。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from typing import Any

from shared.observability.logger import get_logger
from shared.observability.metrics import (
    VM_CONSOLE_CAPTURE_TOTAL,
    VM_CONSOLE_VISION_TOTAL,
    vm_console_confidence_band,
)
from shared.vision.vm_console_observation import (
    DISPLAY_STATE_VOCABULARY,
    VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION,
    VM_CONSOLE_MODEL_REVISION,
    VM_CONSOLE_PROMPT_REVISION,
    VmConsoleObservation,
    unavailable_observation,
)

logger = get_logger("vm-console-vision-worker")

VISION_ALLOWED_ENV = "VM_CONSOLE_VISION_ALLOWED"
MAX_PNG_PIXELS = 4096 * 4096
MAX_PNG_SIDE = 8192

_OBSERVATION_PROMPT = f"""你是虚拟机控制台画面观察器。只描述画面中**可见**的事实，不推测根因。

display_state 必须从受限词表中选择：{sorted(DISPLAY_STATE_VOCABULARY)}。
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
    text = str(raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(text[start : end + 1])
                return payload if isinstance(payload, dict) else None
            except json.JSONDecodeError:
                return None
    return None


async def extract_observation_from_ppm(
    ppm_bytes: bytes, *, artifact_ref: str, trace_id: str | None = None
) -> VmConsoleObservation:
    """对单个 PPM 执行视觉提取；失败一律降级 unavailable（不声称"没有故障"）。"""

    if not vision_allowed():
        return unavailable_observation(
            artifact_ref, summary="视觉观察不可用：合规策略未允许发送图片到视觉模型（VISION_UNAVAILABLE_BY_POLICY）"
        )
    try:
        png_bytes, _, _ = await asyncio.to_thread(derive_png_from_ppm, ppm_bytes)
    except Exception as exc:
        logger.warning("vm_console_worker_derive_failed", artifact_ref=artifact_ref, error=str(exc))
        return unavailable_observation(artifact_ref, summary=f"图片派生失败：{exc}")

    base_url = (os.environ.get("LLM_VISION_BASE_URL") or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
    api_key = os.environ.get("LLM_VISION_API_KEY") or os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("VISION_MODEL", "kimi-k2.5")
    if not base_url or not api_key:
        return unavailable_observation(artifact_ref, summary="视觉端点未配置（VISION_UNAVAILABLE_BY_POLICY）")

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
        logger.warning("vm_console_worker_call_failed", artifact_ref=artifact_ref, error=str(exc))
        return unavailable_observation(artifact_ref, summary="视觉模型调用失败（VISION_UNAVAILABLE_BY_POLICY）")

    payload = _parse_model_payload(raw_text or "")
    if payload is None:
        return unavailable_observation(artifact_ref, summary="视觉模型输出无法解析（VISION_UNCERTAIN）")

    try:
        observation = VmConsoleObservation(
            observation_status="observed",
            display_state=str(payload.get("display_state") or "unknown"),
            summary=str(payload.get("summary") or "")[:500],
            ocr_text=[str(item)[:200] for item in (payload.get("ocr_text") or [])][:20],
            visible_indicators=[str(item)[:64] for item in (payload.get("visible_indicators") or [])][:20],
            confidence=float(payload.get("confidence", 0.0)),
            needs_human_review=bool(payload.get("needs_human_review", False)),
            artifact_id=artifact_ref,
            model_revision=VM_CONSOLE_MODEL_REVISION,
            prompt_revision=VM_CONSOLE_PROMPT_REVISION,
            display_state_vocabulary_revision=VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION,
        )
    except Exception as exc:
        return unavailable_observation(artifact_ref, summary=f"视觉结果 Schema 非法：{exc}")

    if observation.confidence < 0.5 and observation.display_state != "unknown":
        observation = observation.model_copy(update={"display_state": "unknown", "needs_human_review": True})

    VM_CONSOLE_VISION_TOTAL.labels(
        state=observation.display_state,
        confidence_band=vm_console_confidence_band(observation.confidence),
        mode="offline",
    ).inc()
    VM_CONSOLE_CAPTURE_TOTAL.labels(stage="vision", status="ok", mode="offline").inc()

    # 可观测性：只记录元数据，绝不记录原图/OCR 全文/敏感摘要。
    logger.info(
        "vm_console_worker_completed",
        artifact_ref=artifact_ref,
        display_state=observation.display_state,
        confidence=observation.confidence,
        png_sha256=hashlib.sha256(png_bytes).hexdigest(),
        trace_id=trace_id,
    )
    return observation


def observation_to_structured_data(
    observation: VmConsoleObservation, *, source_sha256: str, bundle_id: str, source_path: str
) -> dict[str, Any]:
    """派生 Evidence Item 的 structured_data：观察 Schema + 不可变来源关联。

    OfflineEvidenceProvider 经既有 query_type=json 从 display_state/summary/
    confidence/artifact_id 提取 VM_CONSOLE_* 变量（与在线 produces path 同形）。
    """

    payload = observation.model_dump(mode="json")
    payload["derived_from"] = {
        "bundle_id": bundle_id,
        "source_path": source_path,
        "source_sha256": source_sha256,
    }
    return payload
