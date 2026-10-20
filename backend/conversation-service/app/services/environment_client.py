"""
Environment Client - 与 case-service 交互获取环境上下文的 HTTP 客户端
"""

from typing import Any

import httpx
from shared.models.schemas import EnvironmentContextResponse
from shared.observability.logger import get_logger
from shared.security.signature import sign_client_identity

logger = get_logger("environment-client")


class EnvironmentClient:
    """调用 case-service 的 Environment API 获取环境上下文数据。

    case-service 只信任 api-gateway 用共享密钥（INTERNAL_API_TOKEN）HMAC 签名注入的
    X-Client-ID（见 case-service/app/security/auth.py：`身份签名缺失或无效` 401）。
    本客户端直连 case-service（绕过网关），因此必须自签：对每次请求用归属工单的
    client_id + 同一 INTERNAL_API_TOKEN 生成签名头，否则环境上下文恒 401、env_context
    为空（真 live E2E 实锤：告警里的 host 读不进 Agent，根因采集永久 BLOCKED）。
    """

    def __init__(
        self,
        base_url: str,
        timeout_sec: float = 5.0,
        signing_secret: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        # 与 case-service 同值的共享密钥；缺失时不签名（退化为历史行为，不静默伪造）。
        self._signing_secret = (signing_secret or "").strip()
        # 仅为可测性保留（对齐 SimulationContextClient）；生产为 None 走默认传输。
        self._transport = transport

    def _identity_headers(self, client_id: str | None) -> dict[str, str]:
        """按归属工单的 client_id 生成 HMAC 签名头；无 client_id/无密钥时返回空（不签名）。"""
        if not client_id or not self._signing_secret:
            return {}
        return sign_client_identity(client_id, self._signing_secret)

    async def get_context_info(self, case_id: str, client_id: str | None = None) -> EnvironmentContextResponse | None:
        """
        获取 S0 阶段 Prompt 构建所需的环境上下文。

        调用 GET /api/environments/case/{case_id}/context

        Args:
            case_id: 工单 ID

        Returns:
            EnvironmentContextResponse | None:
                - 成功时返回包含 env_info、alert_logs、task_logs 的响应
                - 失败时返回 None（遵循超时容忍原则，不抛异常）

        超时容忍：单个接口失败不阻塞 AI 对话，仅记录 warning。
        """
        url = f"{self.base_url}/api/environments/case/{case_id}/context"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec, transport=self._transport) as client:
                resp = await client.get(url, headers=self._identity_headers(client_id))

            if resp.status_code == 200:
                data = resp.json()
                context = EnvironmentContextResponse(**data)
                logger.info(
                    event="environment_context_loaded",
                    message=f"成功获取环境上下文: {case_id}",
                    case_id=case_id,
                    env_info_keys=list(context.env_info.keys()),
                    alert_count=len(context.alert_logs),
                    task_count=len(context.task_logs),
                )
                return context

            # 404 表示工单无环境数据（可能是无 SSH 创建的工单）
            if resp.status_code == 404:
                logger.info(
                    event="environment_context_empty",
                    message=f"工单无环境数据: {case_id}",
                    case_id=case_id,
                )
                return None

            logger.warning(
                event="environment_context_failed",
                message=f"获取环境上下文失败: HTTP {resp.status_code}",
                case_id=case_id,
                status_code=resp.status_code,
                response=resp.text[:200],
            )
            return None

        except httpx.TimeoutException:
            logger.warning(
                event="environment_context_timeout",
                message=f"获取环境上下文超时 ({self.timeout_sec}s)",
                case_id=case_id,
            )
            return None

        except Exception as exc:
            logger.warning(
                event="environment_context_exception",
                message=f"获取环境上下文异常: {exc}",
                case_id=case_id,
                error=str(exc),
            )
            return None

    async def get_raw_environments(self, case_id: str, client_id: str | None = None) -> dict[str, Any] | None:
        """
        获取工单所有环境原始数据并组织成 dict。

        调用 GET /api/environments/case/{case_id}

        Args:
            case_id: 工单 ID

        Returns:
            dict | None: 成功时返回包含按 env_type 映射的原始 env_data 字典，失败时返回 None
        """
        url = f"{self.base_url}/api/environments/case/{case_id}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec, transport=self._transport) as client:
                resp = await client.get(url, headers=self._identity_headers(client_id))

            if resp.status_code == 200:
                data = resp.json()  # EnvironmentListResponse
                items = data.get("items", [])
                raw_dict = {}
                for item in items:
                    env_type = item.get("env_type")
                    env_data = item.get("env_data")
                    if env_type and env_data is not None:
                        raw_dict[env_type] = env_data
                logger.info(
                    event="raw_environments_loaded",
                    message=f"成功获取原始环境数据: {case_id}",
                    case_id=case_id,
                    env_types=list(raw_dict.keys()),
                )
                return raw_dict

            if resp.status_code == 404:
                logger.info(
                    event="raw_environments_empty",
                    message=f"工单无环境数据: {case_id}",
                    case_id=case_id,
                )
                return None

            logger.warning(
                event="raw_environments_failed",
                message=f"获取原始环境数据失败: HTTP {resp.status_code}",
                case_id=case_id,
                status_code=resp.status_code,
                response=resp.text[:200],
            )
            return None

        except httpx.TimeoutException:
            logger.warning(
                event="raw_environments_timeout",
                message=f"获取原始环境数据超时 ({self.timeout_sec}s)",
                case_id=case_id,
            )
            return None

        except Exception as exc:
            logger.warning(
                event="raw_environments_exception",
                message=f"获取原始环境数据异常: {exc}",
                case_id=case_id,
                error=str(exc),
            )
            return None
