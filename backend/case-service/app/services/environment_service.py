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

    async def upsert_environment(self, case_id: str, env_type: EnvType, env_data: dict, collected_at=None) -> EnvironmentResponse:
        """幂等 upsert 环境数据（有则更新，无则创建）"""
        trace_id = get_current_trace_id()

        env, created = await self.repository.upsert_by_case_and_type(
            case_id=case_id,
            env_type=env_type.value,
            env_data=env_data,
            collected_at=collected_at,
        )

        # 如果是新建，设置 trace_id
        if created:
            env.trace_id = trace_id
            await self.repository.session.flush()
            await self.repository.session.refresh(env)

        action = "created" if created else "updated"
        logger.info(
            event=f"environment_{action}",
            message=f"Upsert environment data for case {case_id} ({env_type.value}): {action}",
            case_id=case_id,
            env_type=env_type.value,
            data_size=len(str(env_data)),
            created=created,
        )

        return EnvironmentResponse.model_validate(env)

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
        """
        提取环境基本信息（标准化格式）

        acli platform info get 输出经前端 parseClusterOutput 解析后的字段：
        - hci_version: 版本号行（如 "6.10.0_R2"）
        - name: [cluster] section 的 name= 值（集群名）
        - mcastaddr: [cluster] section 的 mcastaddr= 值（组播地址，体现网络配置）
        - host_count / storage_type：当前 acli platform info get 不提供，保留"未知"
        """
        return {
            "hci_version": env_data.get("hci_version", "未知"),
            "cluster_name": env_data.get("name", "未知"),       # [cluster] name= 字段
            "host_count": env_data.get("host_count", "未知"),   # 当前采集命令不含此字段
            "storage_type": env_data.get("storage_type", "未知"),
            "network_config": env_data.get("mcastaddr", "未知"),  # 组播地址体现网络配置
        }

    def _extract_alert_logs(self, env_data: dict) -> list[dict]:
        """
        提取告警日志列表（标准化格式）

        acli --formatter json alert list 实际字段：
          urgent_type: 1=紧急, 0=普通（整数）   →  level: "CRITICAL"/"WARNING"
          end:         Unix 时间戳              →  time（转为可读字符串）
          target:      告警对象
          type:        事件类型
          description: 告警描述（截断 300 字符）
          host:        主机名
          vm:          虚拟机名（可选，不存在则不写入）
        """
        alerts = env_data.get("alerts", [])

        def map_level(urgent_type) -> str:
            """urgent_type 1=紧急→CRITICAL，0=普通→WARNING"""
            if urgent_type in (1, "1"):
                return "CRITICAL"
            if urgent_type in (0, "0"):
                return "WARNING"
            return "WARNING"

        def fmt_ts(ts) -> str:
            """Unix 时间戳转可读字符串"""
            if not ts:
                return ""
            try:
                from datetime import datetime, timezone
                return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return str(ts)

        result = []
        for a in alerts[:10]:  # 最多 10 条
            item: dict = {
                "level": map_level(a.get("urgent_type")),
                "time": fmt_ts(a.get("end", "")),
                "target": a.get("target", ""),
                "type": a.get("type", ""),
                "description": (a.get("description", "") or "")[:300],
                "host": a.get("host", ""),
            }
            if a.get("vm"):  # vm 字段仅在存在时写入
                item["vm"] = a["vm"]
            result.append(item)
        return result

    def _extract_task_logs(self, env_data: dict) -> list[dict]:
        """
        提取任务日志列表（标准化格式）

        acli --formatter json task list 实际字段：
          status:          3=失败, 2=完成（整数）    →  status: "失败"/"完成"
          type:            任务行为/类型名称          →  type
          end:             Unix 时间戳               →  time（转为可读字符串）
          host:            主机名
          vm:              虚拟机名（可选，不存在则不写入）
          target:          操作对象
          description:     错误描述（截断 300 字符）
          errcode_tracing: 错误码
          request_id:      trace_id
        """
        tasks = env_data.get("tasks", [])

        def map_status(status) -> str:
            """status 整数 3=失败，2=完成"""
            mapping = {3: "失败", "3": "失败", 2: "完成", "2": "完成"}
            return mapping.get(status, str(status) if status is not None else "未知")

        def fmt_ts(ts) -> str:
            """Unix 时间戳转可读字符串"""
            if not ts:
                return ""
            try:
                from datetime import datetime, timezone
                return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return str(ts)

        result = []
        for t in tasks[:10]:  # 最多 10 条
            item: dict = {
                "status": map_status(t.get("status")),
                "type": t.get("type", ""),
                "time": fmt_ts(t.get("end", "")),
                "host": t.get("host", ""),
                "target": t.get("target", ""),
                "description": (t.get("description", "") or "")[:300],
                "errcode_tracing": t.get("errcode_tracing", ""),
                "trace_id": t.get("request_id", ""),
            }
            if t.get("vm"):  # vm 字段仅在存在时写入
                item["vm"] = t["vm"]
            result.append(item)
        return result
