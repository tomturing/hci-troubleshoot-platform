"""
AI Assistant Client - 多类型AI助手客户端抽象层 (v2.0)

支持通过 Protocol + Factory 模式扩展多种AI助手后端。
当前实现: OpenClaw (OpenAI兼容API)
"""

import json
import os
from collections.abc import AsyncGenerator
from ipaddress import ip_address
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
from shared.observability.logger import get_logger
from shared.utils.exceptions import AIStreamError, ErrorCode

logger = get_logger("ai-client")


@runtime_checkable
class AIAssistantClient(Protocol):
    """AI助手客户端协议 — 所有助手后端必须实现此接口"""

    async def chat_completion_stream(
        self, messages: list[dict[str, str]], user_id: str, pod_endpoint: str | None = None, model: str = ""
    ) -> AsyncGenerator[str, None]:
        """流式对话补全"""
        ...

    async def check_health(self) -> bool:
        """健康检查"""
        ...

    async def close(self) -> None:
        """关闭客户端资源"""
        ...


class OpenClawAssistant:
    """
    OpenClaw AI助手客户端 (OpenAI兼容)
    使用 /v1/chat/completions 端点
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        provider_api_key: str | None = None,
        default_model: str = "glm-5",
        assistant_type: str = "htp-agent",
    ):
        self.base_url = base_url.rstrip("/")
        # api_key: LLM API 鉴权密钥
        self.api_key = api_key
        # provider_api_key: 外部 LLM 提供商鉴权（优先级: 构造参数 > 环境变量 > api_key 兜底）
        self.provider_api_key = provider_api_key or os.environ.get("LLM_API_KEY") or api_key
        self.default_model = default_model
        self.assistant_type = assistant_type
        # 流式 LLM 响应可能较慢，读超时通过环境变量 AI_CLIENT_READ_TIMEOUT_SEC 调整（默认 120s）
        _read_timeout = float(os.environ.get("AI_CLIENT_READ_TIMEOUT_SEC", "120.0"))
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=_read_timeout, write=10.0, pool=10.0))

    @staticmethod
    def _is_internal_gateway_endpoint(endpoint: str) -> bool:
        """识别内部 claw 网关端点（Service DNS / Pod IP / 本机代理）。"""
        host = (urlparse(endpoint).hostname or "").lower()
        if not host:
            return False
        if host in {"localhost", "127.0.0.1", "host.docker.internal", "openclaw", "productionclaw", "learningclaw"}:
            return True
        if host.endswith(".svc") or host.endswith(".svc.cluster.local"):
            return True
        try:
            return ip_address(host).is_private
        except ValueError:
            return False

    def _resolve_auth_token(self, endpoint: str) -> str | None:
        """内部 gateway 使用 api_key；外部模型提供商使用 provider_api_key。"""
        if self._is_internal_gateway_endpoint(endpoint):
            return self.api_key or self.provider_api_key
        return self.provider_api_key or self.api_key

    async def chat_completion_stream(
        self, messages: list[dict[str, str]], user_id: str, pod_endpoint: str | None = None, model: str = ""
    ) -> AsyncGenerator[str, None]:
        """
        调用OpenClaw Chat Completions API (流式)

        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            user_id: 用户ID (映射为OpenClaw Session Key)
            model: 模型名称

        Yields:
            str: AI响应内容片段
        """
        headers = {
            "Content-Type": "application/json",
        }

        payload = {"model": model or self.default_model, "messages": messages, "stream": True, "user": user_id}

        # 先尝试 scheduler 分配的实例端点，若在首 token 前发生可恢复断流，再回退到稳定 base_url 重试一次。
        endpoints_to_try: list[str] = []
        first_endpoint = (pod_endpoint or self.base_url).rstrip("/")
        endpoints_to_try.append(first_endpoint)
        fallback_endpoint = self.base_url.rstrip("/")
        if fallback_endpoint not in endpoints_to_try:
            endpoints_to_try.append(fallback_endpoint)

        # ZhipuAI v4 API 路径为 /chat/completions（无 /v1/），OpenAI 兴容接口为 /v1/chat/completions
        # 根据 endpoint 类型动态选择正确路径，而不是依赖单一环境变量
        _default_internal_path = "/v1/chat/completions"  # OpenAI 兴容接口（OpenClaw Pod）
        _default_external_path = "/chat/completions"  # ZhipuAI v4 API

        last_error: Exception | None = None
        for idx, endpoint in enumerate(endpoints_to_try, start=1):
            # 根据 endpoint 类型选择正确的 completions path
            if self._is_internal_gateway_endpoint(endpoint):
                _completions_path = os.environ.get("AI_COMPLETIONS_PATH", _default_internal_path)
            else:
                # 外部 API（智谱）固定使用 /chat/completions，环境变量可覆盖
                _completions_path = os.environ.get("AI_COMPLETIONS_PATH_EXTERNAL", _default_external_path)
            url = f"{endpoint}{_completions_path}"
            got_first_token = False
            token = self._resolve_auth_token(endpoint)
            request_headers = dict(headers)
            if token:
                request_headers["Authorization"] = f"Bearer {token}"

            logger.info(
                event="ai_request",
                message="Sending request to OpenClaw",
                url=url,
                user_id=user_id,
                assistant_type=self.assistant_type,
                attempt=idx,
                auth_mode="gateway_token" if self._is_internal_gateway_endpoint(endpoint) else "provider_api_key",
            )

            try:
                async with self.client.stream("POST", url, json=payload, headers=request_headers) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        error_text = error_body.decode("utf-8", errors="ignore")
                        logger.error(
                            event="ai_error",
                            message=f"OpenClaw returned status {response.status_code}",
                            status=response.status_code,
                            body=error_text,
                            attempt=idx,
                        )

                        # 解析错误详情并抛出结构化异常
                        error_detail = self._parse_ai_error(response.status_code, error_text)
                        raise AIStreamError(
                            code=error_detail["code"],
                            message=error_detail["message"],
                            detail=error_detail["detail"],
                        )

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue

                        data_str = line[6:]  # 跳过 "data: "
                        if data_str.strip() == "[DONE]":
                            # 收到 [DONE] 但没有任何实际内容 → 上游服务可能静默失败
                            if not got_first_token:
                                logger.warning(
                                    event="ai_empty_response",
                                    message="AI 服务返回空响应（可能 rate limit 或服务异常）",
                                    url=url,
                                    attempt=idx,
                                )
                                # 空响应：跳出 SSE 循环，让代码进入流结束检查
                                # （流结束检查会尝试 fallback endpoint 或抛出错误）
                                break
                            return  # 正常结束，有内容

                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                got_first_token = True
                                yield content
                        except json.JSONDecodeError:
                            continue

                # 流结束后检查是否有实际内容
                if not got_first_token:
                    logger.warning(
                        event="ai_stream_no_content",
                        message="AI 流结束但无任何内容",
                        url=url,
                        attempt=idx,
                    )
                    if idx < len(endpoints_to_try):
                        continue  # 尝试 fallback endpoint（endpoint 循环的下一个迭代）
                    else:
                        raise AIStreamError(
                            code=ErrorCode.AI_RATE_LIMITED,
                            message="AI 服务返回空响应，可能是请求频率超限或账户余额不足",
                            detail="status=200, stream ended without content",
                        )

                return  # 成功获取内容，退出 endpoint 循环
            except AIStreamError:
                # AIStreamError 直接透传，不包装
                raise
            except Exception as e:
                last_error = e
                retriable = self._is_retriable_stream_error(e)
                can_retry = (not got_first_token) and retriable and idx < len(endpoints_to_try)

                logger.error(
                    event="ai_exception",
                    message="Error calling OpenClaw API",
                    error=str(e),
                    attempt=idx,
                    retriable=retriable,
                    got_first_token=got_first_token,
                    will_retry=can_retry,
                )

                if can_retry:
                    continue
                # 将未捕获的异常转换为结构化错误
                error_detail = self._parse_generic_error(e)
                raise AIStreamError(
                    code=error_detail["code"],
                    message=error_detail["message"],
                    detail=error_detail["detail"],
                ) from e

        if last_error:
            # 所有端点都失败，抛出结构化错误
            error_detail = self._parse_generic_error(last_error)
            raise AIStreamError(
                code=error_detail["code"],
                message=error_detail["message"],
                detail=error_detail["detail"],
            )

    def _parse_ai_error(self, status_code: int, error_body: str) -> dict:
        """
        解析 AI 服务返回的错误响应，生成用户友好的错误信息

        Args:
            status_code: HTTP 状态码
            error_body: 错误响应体

        Returns:
            dict: {code, message, detail} 用于构造 AIStreamError
        """
        # 尝试解析 JSON 错误体
        error_info = {}
        try:
            parsed = json.loads(error_body)
            if "error" in parsed:
                error_info = parsed["error"]
        except json.JSONDecodeError:
            error_info = {"message": error_body[:200]}

        raw_message = error_info.get("message", "")
        error_type = error_info.get("type", "")

        # 根据状态码和错误类型生成友好消息
        if status_code == 401:
            return {
                "code": ErrorCode.AI_AUTH_FAILED,
                "message": "AI 服务认证失败，请检查 API 密钥配置",
                "detail": f"status=401, type={error_type}, message={raw_message}",
            }
        elif status_code == 429:
            # Rate limit - 提取具体原因
            friendly_msg = "AI 服务请求频率超限"
            if "余额不足" in raw_message or "充值" in raw_message:
                friendly_msg = "AI 服务账户余额不足，请充值后重试"
            elif "rate limit" in raw_message.lower():
                friendly_msg = "AI 服务请求过于频繁，请稍后重试"
            return {
                "code": ErrorCode.AI_RATE_LIMITED,
                "message": friendly_msg,
                "detail": f"status=429, type={error_type}, message={raw_message}",
            }
        elif status_code == 404:
            return {
                "code": ErrorCode.AI_UNAVAILABLE,
                "message": "AI 服务接口不存在，请检查配置",
                "detail": f"status=404, body={error_body[:200]}",
            }
        elif status_code == 400:
            return {
                "code": ErrorCode.AI_UPSTREAM_ERROR,
                "message": f"AI 服务请求参数错误：{raw_message}",
                "detail": f"status=400, type={error_type}, message={raw_message}",
            }
        elif status_code >= 500:
            return {
                "code": ErrorCode.AI_UPSTREAM_ERROR,
                "message": "AI 上游服务故障，请稍后重试",
                "detail": f"status={status_code}, type={error_type}, message={raw_message}",
            }
        else:
            return {
                "code": ErrorCode.AI_UPSTREAM_ERROR,
                "message": f"AI 服务返回错误（{status_code}）：{raw_message[:100]}",
                "detail": f"status={status_code}, type={error_type}, message={raw_message}",
            }

    def _parse_generic_error(self, exc: Exception) -> dict:
        """
        解析通用异常，生成用户友好的错误信息

        Args:
            exc: 原始异常

        Returns:
            dict: {code, message, detail} 用于构造 AIStreamError
        """
        exc_type = type(exc).__name__
        exc_message = str(exc)

        # HTTP 状态错误
        if isinstance(exc, httpx.HTTPStatusError):
            return self._parse_ai_error(exc.response.status_code, exc.response.text)

        # 连接超时
        if isinstance(exc, httpx.TimeoutException):
            return {
                "code": ErrorCode.AI_TIMEOUT,
                "message": "AI 服务响应超时，请稍后重试",
                "detail": f"type={exc_type}, message={exc_message}",
            }

        # 连接错误
        if isinstance(exc, httpx.ConnectError):
            return {
                "code": ErrorCode.AI_UNAVAILABLE,
                "message": "无法连接到 AI 服务，请检查网络或服务状态",
                "detail": f"type={exc_type}, message={exc_message}",
            }

        # 其他网络错误
        if isinstance(exc, httpx.RequestError):
            return {
                "code": ErrorCode.AI_UNAVAILABLE,
                "message": f"AI 服务网络错误：{exc_message[:100]}",
                "detail": f"type={exc_type}, message={exc_message}",
            }

        # 未知错误
        return {
            "code": ErrorCode.INTERNAL_ERROR,
            "message": f"AI 服务内部错误：{exc_message[:100]}",
            "detail": f"type={exc_type}, message={exc_message}",
        }

    @staticmethod
    def _is_retriable_stream_error(exc: Exception) -> bool:
        """判定是否属于可通过切换端点重试的流式瞬态错误。"""
        message = str(exc).lower()
        retriable_signatures = (
            "incomplete chunked read",
            "peer closed connection",
            "read timeout",
            "remoteprotocolerror",
            "connection reset",
        )
        return any(sig in message for sig in retriable_signatures)

    async def check_health(self) -> bool:
        """检查OpenClaw服务健康状态"""
        _completions_path = os.environ.get("AI_COMPLETIONS_PATH", "/v1/chat/completions")
        url = f"{self.base_url}{_completions_path}"
        headers = {
            "Content-Type": "application/json",
        }
        token = self._resolve_auth_token(self.base_url)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = await self.client.post(url, json={}, headers=headers)
            if response.status_code in (200, 400, 422):
                return True
            if response.status_code in (401, 403, 404, 405):
                return False
            return False
        except Exception:
            return False

    async def close(self):
        """关闭客户端"""
        await self.client.aclose()


class AIAssistantRegistry:
    """AI助手客户端注册表 (v2.0)

    管理多种AI助手客户端实例。通过 assistant_type 查找对应的客户端。
    """

    def __init__(self):
        self._clients: dict[str, AIAssistantClient] = {}
        self._default_type: str = "openclaw"

    def register(self, assistant_type: str, client: AIAssistantClient, *, is_default: bool = False) -> None:
        """注册AI助手客户端。is_default=True 时将其设为降级首选助手。"""
        self._clients[assistant_type] = client
        if is_default:
            self._default_type = assistant_type
        logger.info(f"Registered AI assistant client: {assistant_type}")

    def set_default_type(self, assistant_type: str) -> None:
        """设置默认（降级）助手类型。"""
        self._default_type = assistant_type

    def get_default_type(self) -> str:
        """返回当前默认助手类型，若默认类型未注册则返回第一个已注册的类型。"""
        if self._default_type in self._clients:
            return self._default_type
        # 降级：取第一个注册的类型，避免硬编码 key 与实际注册不一致
        if self._clients:
            return next(iter(self._clients))
        return self._default_type  # 注册表为空时保留原值（兜底，不会被正常使用）

    def get_client(self, assistant_type: str | None = None) -> AIAssistantClient | None:
        """获取指定类型的AI助手客户端"""
        atype = assistant_type or self._default_type
        client = self._clients.get(atype)
        if not client:
            logger.warning(f"No AI assistant client found for type: {atype}")
        return client

    def list_types(self) -> list[str]:
        """列出所有已注册的助手类型"""
        return list(self._clients.keys())

    async def close_all(self) -> None:
        """关闭所有客户端"""
        for atype, client in self._clients.items():
            try:
                await client.close()
                logger.info(f"Closed AI assistant client: {atype}")
            except Exception as e:
                logger.error(f"Error closing AI assistant client {atype}: {e}")
        self._clients.clear()

    async def health_check_all(self) -> dict[str, bool]:
        """对所有注册客户端执行健康检查"""
        results = {}
        for atype, client in self._clients.items():
            try:
                results[atype] = await client.check_health()
            except Exception:
                results[atype] = False
        return results


def create_openclaw_client(
    base_url: str,
    api_key: str | None = None,
    provider_api_key: str | None = None,
    default_model: str = "openclaw",
    assistant_type: str = "htp-agent",
) -> OpenClawAssistant:
    """工厂函数: 创建OpenClaw助手客户端"""
    return OpenClawAssistant(
        base_url=base_url,
        api_key=api_key,
        provider_api_key=provider_api_key,
        default_model=default_model,
        assistant_type=assistant_type,
    )
