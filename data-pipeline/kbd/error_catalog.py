"""面向操作者的稳定错误码与中文说明。

底层异常文本会随 httpx/OpenAI SDK 版本变化，不能作为 CLI、JSONL 或重试脚本的契约。
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class HumanError:
    code: str
    message: str
    retryable: bool


def humanize_error(exc: Exception) -> HumanError:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return HumanError("LLM_TIMEOUT", "调用超时；服务或模型在规定时间内未返回。", True)
    if isinstance(exc, httpx.TransportError):
        return HumanError("KB_SERVICE_UNAVAILABLE", "无法连接 kb-service 或连接中断。", True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return HumanError("LLM_RATE_LIMITED", "触发限流；系统将遵守退避时间后重试。", True)
        if 500 <= status < 600:
            return HumanError("KB_SERVICE_ERROR", f"kb-service 返回 {status} 服务端错误。", True)
        if status in (401, 403):
            return HumanError("SOURCE_AUTH_FAILED", "认证失败；请检查内部 Token 或访问权限。", False)
        return HumanError("KB_SERVICE_REQUEST_INVALID", f"kb-service 返回 {status} 请求错误。", False)
    text = str(exc).lower()
    if "json" in text:
        return HumanError("LLM_INVALID_JSON", "模型返回内容不是可用的结构化 JSON。", True)
    if "job" in text and ("不存在" in str(exc) or "重启" in str(exc)):
        return HumanError("JOB_NOT_FOUND_AFTER_RESTART", "异步任务状态丢失，kb-service 可能已重启。", True)
    return HumanError("PIPELINE_UNEXPECTED", "发生未分类异常；请使用 run_id 和 trace_id 查看 JSONL 详细日志。", False)
