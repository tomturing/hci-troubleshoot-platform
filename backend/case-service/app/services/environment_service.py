"""
Environment Service - 环境数据业务逻辑层

提供环境数据的 CRUD 操作，以及 S0 阶段 Prompt 构建所需的上下文信息。
"""

from shared.models.schemas import (
    EnvironmentContextResponse,
    EnvironmentCreate,
    EnvironmentListResponse,
    EnvironmentResponse,
    EnvType,
)
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

from ..models.environment import Environment
from ..repositories.environment_repo import EnvironmentRepository

logger = get_logger("environment-service")


class EnvironmentService:
    """环境数据业务服务"""

    def __init__(self, repository: EnvironmentRepository):
        self.repository = repository

    async def create_environment(self, env_create: EnvironmentCreate) -> EnvironmentResponse:
        """创建环境数据"""
        trace_id = get_current_trace_id()

        environment = Environment(
            case_id=env_create.case_id,
            env_type=env_create.env_type.value,
            env_data=env_create.env_data,
            trace_id=trace_id,
        )

        # 仅在客户端传入 collected_at 时才赋值，否则让 DB 默认值生效
        if env_create.collected_at is not None:
            environment.collected_at = env_create.collected_at

        created_env = await self.repository.create(environment)

        logger.info(
            event="environment_created",
            message=f"Created environment data for case {env_create.case_id}",
            case_id=env_create.case_id,
            env_type=env_create.env_type.value,
            data_size=len(str(env_create.env_data)),
        )

        return EnvironmentResponse.model_validate(created_env)

    async def get_environments_by_case(self, case_id: str) -> EnvironmentListResponse:
        """获取工单所有环境数据"""
        envs = await self.repository.get_by_case_id(case_id)
        return EnvironmentListResponse(
            items=[EnvironmentResponse.model_validate(e) for e in envs],
            total=len(envs),
        )

    async def get_environment_by_type(self, case_id: str, env_type: EnvType) -> EnvironmentResponse | None:
        """获取工单指定类型环境数据"""
        env = await self.repository.get_by_case_and_type(case_id, env_type.value)
        if not env:
            return None
        return EnvironmentResponse.model_validate(env)

    async def build_context_info(self, case_id: str) -> EnvironmentContextResponse:
        """
        构建 S0 阶段 Prompt 构建所需的 context_info 字典

        返回格式与 PromptBuilder._segment_s0_context_info 期望的结构一致：
        {
            "env_info": { ... },
            "alert_logs": [ ... ],
            "task_logs": [ ... ],
        }

        缺失字段填充默认值，不抛异常（超时容忍原则）
        """
        context_info = EnvironmentContextResponse()

        try:
            # 获取集群基本信息
            cluster_env = await self.repository.get_by_case_and_type(case_id, EnvType.CLUSTER.value)
            if cluster_env:
                context_info.env_info = self._extract_env_info(cluster_env.env_data)

            # 获取告警列表
            alert_env = await self.repository.get_by_case_and_type(case_id, EnvType.ALERT.value)
            if alert_env:
                context_info.alert_logs = self._extract_alert_logs(alert_env.env_data)

            # 获取任务列表
            task_env = await self.repository.get_by_case_and_type(case_id, EnvType.TASK.value)
            if task_env:
                context_info.task_logs = self._extract_task_logs(task_env.env_data)

        except Exception as e:
            logger.warning(
                event="context_info_build_error",
                message=f"构建环境上下文失败：{e}",
                case_id=case_id,
                error=str(e),
            )

        return context_info

    def _extract_env_info(self, env_data: dict) -> dict:
        """提取环境基本信息（标准化格式）"""
        return {
            "hci_version": env_data.get("hci_version", "未知"),
            "cluster_name": env_data.get("cluster_name", "未知"),
            "host_count": env_data.get("host_count", "未知"),
            "storage_type": env_data.get("storage_type", "未知"),
            "network_config": env_data.get("network_config", "未知"),
        }

    def _extract_alert_logs(self, env_data: dict) -> list[dict]:
        """提取告警日志列表（标准化格式）"""
        alerts = env_data.get("alerts", [])
        return [
            {
                "level": a.get("level", "INFO"),
                "time": a.get("trigger_time", ""),
                "content": a.get("content", ""),
                "source": a.get("source", ""),
            }
            for a in alerts[:10]  # 最多 10 条
        ]

    def _extract_task_logs(self, env_data: dict) -> list[dict]:
        """提取任务日志列表（标准化格式）"""
        tasks = env_data.get("tasks", [])
        return [
            {
                "status": t.get("status", "unknown"),
                "time": t.get("start_time", ""),
                "name": t.get("name", ""),
                "error": t.get("error_msg", ""),
            }
            for t in tasks[:5]  # 最多 5 条失败任务
        ]
