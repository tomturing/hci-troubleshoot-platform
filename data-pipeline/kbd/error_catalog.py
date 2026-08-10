"""面向操作者的稳定错误码与中文说明。

底层异常文本会随 httpx/OpenAI SDK 版本变化，不能作为 CLI、JSONL 或重试脚本的契约。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class HumanError:
    code: str
    message: str
    retryable: bool
    detail: str | None = None
    action: str | None = None


class JobFailureError(RuntimeError):
    """服务端异步任务的结构化失败，不在异常包装时丢失 job_id 和真实原因。"""

    def __init__(self, stage: str, job_id: str, detail: str) -> None:
        self.stage = stage
        self.job_id = job_id
        self.detail = _safe_detail(detail)
        super().__init__(f"{stage} Job {job_id} 失败：{self.detail}")


def _safe_detail(value: object, *, limit: int = 600) -> str:
    """压平并截断底层原因，同时遮蔽最常见的凭据形态。"""

    text = " ".join(str(value or "").split())
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1***", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|password)\s*[:=]\s*)[^\s,;]+",
        r"\1***",
        text,
    )
    if not text:
        return "服务端未返回具体原因"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _wrapped_error_is_retryable(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in (
        "429", "too many requests", "限流", "timeout", "timed out", "超时",
        "connection", " 500", " 502", " 503", " 504", "service unavailable",
    ))


def _http_response_detail(exc: httpx.HTTPStatusError) -> str:
    """优先提取 API 返回的 detail/message，避免只显示一个 HTTP 状态码。"""

    try:
        payload = exc.response.json()
        if isinstance(payload, dict):
            for key in ("detail", "message", "error", "msg"):
                value = payload.get(key)
                if value:
                    return _safe_detail(value)
        elif payload:
            return _safe_detail(payload)
    except (ValueError, TypeError):
        pass
    return _safe_detail(exc)


def humanize_error(exc: Exception) -> HumanError:
    if isinstance(exc, JobFailureError):
        label = "识图" if exc.stage == "Vision" else "关键信号抽取"
        retryable = _wrapped_error_is_retryable(exc.detail)
        action = (
            "确认服务或模型恢复后，使用 --failed 只重试失败任务。"
            if retryable
            else "根据上面的具体原因修复输入或配置后，使用 --failed 重试。"
        )
        return HumanError(
            f"{exc.stage.upper()}_JOB_FAILED",
            f"{label}任务失败（job_id={exc.job_id}）：{exc.detail}",
            retryable,
            detail=exc.detail,
            action=action,
        )
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return HumanError(
            "LLM_TIMEOUT", "调用超时；服务或模型在规定时间内未返回。", True,
            detail=_safe_detail(exc), action="确认服务负载后，使用 --failed 只重试失败任务。",
        )
    if isinstance(exc, httpx.TransportError):
        return HumanError(
            "KB_SERVICE_UNAVAILABLE", "无法连接 kb-service 或连接中断。", True,
            detail=_safe_detail(exc), action="检查 kb-service 地址和健康状态后重试。",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            detail = _http_response_detail(exc)
            return HumanError("LLM_RATE_LIMITED", f"触发限流：{detail}", True,
                              detail=detail, action="等待 Provider 限流窗口恢复后使用 --failed 重试。")
        if 500 <= status < 600:
            detail = _http_response_detail(exc)
            return HumanError("KB_SERVICE_ERROR", f"kb-service 返回 {status} 服务端错误：{detail}", True,
                              detail=detail, action="确认 kb-service 恢复后使用 --failed 重试。")
        if status in (401, 403):
            detail = _http_response_detail(exc)
            return HumanError("SOURCE_AUTH_FAILED", f"认证失败：{detail}", False,
                              detail=detail, action="修正 INTERNAL_API_TOKEN 或访问权限后重试。")
        detail = _http_response_detail(exc)
        return HumanError("KB_SERVICE_REQUEST_INVALID", f"kb-service 返回 {status} 请求错误：{detail}", False,
                          detail=detail, action="检查请求参数和 KBD 当前状态后重试。")
    text = str(exc).lower()
    # Vision/Signal 异步 Job 会把 Provider HTTP 错误包装成 RuntimeError；
    # 仍须从稳定的错误文本中恢复可操作的限流/服务端语义，不能退化为 PIPELINE_UNEXPECTED。
    if "429" in text or "too many requests" in text or "限流" in text:
        return HumanError("LLM_RATE_LIMITED", "LLM Provider 触发限流；本轮重试仍未成功。", True,
                          detail=_safe_detail(exc), action="等待 Provider 限流窗口恢复后使用 --failed 重试。")
    if any(token in text for token in (" 500", " 502", " 503", " 504", "service unavailable", "服务端错误")):
        return HumanError("KB_SERVICE_ERROR", "kb-service 或 Provider 返回暂态服务端错误。", True,
                          detail=_safe_detail(exc), action="确认服务恢复后使用 --failed 重试。")
    if "json" in text:
        return HumanError("LLM_INVALID_JSON", "模型返回内容不是可用的结构化 JSON。", True,
                          detail=_safe_detail(exc), action="检查 Prompt/模型输出后使用 --failed 重试。")
    if "job" in text and ("不存在" in str(exc) or "重启" in str(exc)):
        return HumanError("JOB_NOT_FOUND_AFTER_RESTART", "异步任务状态丢失，kb-service 可能已重启。", True,
                          detail=_safe_detail(exc), action="确认 kb-service 稳定后使用 --failed 重试。")
    detail = _safe_detail(exc)
    return HumanError(
        "PIPELINE_UNEXPECTED",
        f"发生未分类的 {type(exc).__name__}：{detail}",
        _wrapped_error_is_retryable(detail),
        detail=detail,
        action="先依据上述原始原因排查；完整堆栈在同一 run_id 的 .log 文本日志中。",
    )
