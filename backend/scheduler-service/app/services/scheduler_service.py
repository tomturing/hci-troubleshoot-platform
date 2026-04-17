"""
Scheduler Service - 多类型AI助手调度与生命周期管理 (v2.0)

变更记录:
- 使用 Redis Hash 替代内存 Dict 持久化 Pod 分配状态
- 支持服务重启后自动恢复分配映射
"""

import json

from shared.database.redis import RedisManager
from shared.utils.logger import get_logger

from app.config import settings

from .k8s_client import K8sClient
from .pod_pool import PodPoolManager

logger = get_logger("scheduler-service")

# Redis Hash 键名，存储 case_id -> JSON(pod_name, assistant_type) 的映射
REDIS_ALLOCATIONS_KEY = "scheduler:allocations"


class SchedulerService:
    """调度服务 (v2.0: 支持多类型AI助手，Redis 持久化分配状态)"""

    def __init__(self, k8s_client: K8sClient, redis_manager: RedisManager):
        self.k8s = k8s_client
        self.redis = redis_manager
        self.pool_manager = PodPoolManager(
            k8s_client=k8s_client,
            assistant_registry=settings.assistant_registry
        )

    async def start(self):
        """启动服务，初始化 Pod 池并开始后台维护任务"""
        logger.info("Starting Scheduler Service background tasks (v2.0)")
        # 先让各池同步集群现有 Pod
        await self.pool_manager.initialize_all()
        # 再补充热备池
        await self.pool_manager.ensure_all_warm_pools()
        # H-3：对账 Redis 分配记录与 K8s 实际 Pod 状态（清除孤立分配）
        await self.reconcile_allocations()

    async def reconcile_allocations(self) -> None:
        """
        对账：将 Redis 分配记录与 K8s 实际 Pod 状态对齐（H-3 加固）

        孤立分配（Redis 有记录但 Pod 不存在）→ 清理 Redis 记录
        正常分配保留不动。
        """
        all_allocations = await self._get_all_allocations()
        if not all_allocations:
            logger.info(event="reconcile_skip", message="无活跃分配记录，跳过对账")
            return

        orphan_count = 0
        for case_id, (pod_name, _assistant_type) in list(all_allocations.items()):
            pod_status = self.k8s.get_pod_status(pod_name)
            if pod_status is None:
                # Pod 不存在，清除孤立分配记录
                logger.warning(
                    event="reconcile_orphan_allocation",
                    message="发现孤立分配（Pod 不存在），清理 Redis 记录",
                    case_id=case_id,
                    pod_name=pod_name,
                )
                await self._del_allocation(case_id)
                orphan_count += 1

        logger.info(
            event="reconcile_done",
            message=f"对账完成，共检查 {len(all_allocations)} 条分配，清理孤立分配 {orphan_count} 条",
            total=len(all_allocations),
            orphans_cleaned=orphan_count,
        )

    # ────────── Redis 分配映射操作 ──────────

    async def _get_allocation(self, case_id: str) -> tuple[str, str] | None:
        """从 Redis 获取 case_id 的分配信息 -> (pod_name, assistant_type)"""
        raw = await self.redis.hget(REDIS_ALLOCATIONS_KEY, case_id)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return (data["pod_name"], data["assistant_type"])
        except (json.JSONDecodeError, KeyError):
            # 数据损坏，清理
            await self.redis.hdel(REDIS_ALLOCATIONS_KEY, case_id)
            return None

    async def _set_allocation(self, case_id: str, pod_name: str, assistant_type: str):
        """写入分配信息到 Redis"""
        value = json.dumps({"pod_name": pod_name, "assistant_type": assistant_type})
        await self.redis.hset(REDIS_ALLOCATIONS_KEY, case_id, value)

    async def _del_allocation(self, case_id: str):
        """删除分配信息"""
        await self.redis.hdel(REDIS_ALLOCATIONS_KEY, case_id)

    async def _get_all_allocations(self) -> dict[str, tuple[str, str]]:
        """获取所有分配信息"""
        raw_map = await self.redis.hgetall(REDIS_ALLOCATIONS_KEY)
        result = {}
        for case_id, raw in raw_map.items():
            try:
                data = json.loads(raw)
                result[case_id] = (data["pod_name"], data["assistant_type"])
            except (json.JSONDecodeError, KeyError):
                continue
        return result

    # ────────── 核心调度方法 ──────────

    async def allocate_pod(
        self,
        case_id: str,
        assistant_type: str = "openclaw"
    ) -> str | None:
        """为工单分配指定类型的Pod"""
        # 检查是否已有分配
        existing = await self._get_allocation(case_id)
        if existing:
            pod_name, existing_type = existing
            if existing_type == assistant_type:
                # 同类型，检查Pod还活着
                status = self.k8s.get_pod_status(pod_name)
                if status in ["Running", "Pending"]:
                    logger.info(f"Reusing existing {assistant_type} pod {pod_name} for case {case_id}")
                    return pod_name
            # 类型不同或Pod已死，清理
            await self._del_allocation(case_id)

        # 获取对应类型的Pod池
        pool = self.pool_manager.get_pool(assistant_type)
        if not pool:
            logger.error(f"No pool found for assistant type: {assistant_type}")
            return None

        # 从池中获取
        pod_name = await pool.acquire_pod(case_id)
        if pod_name:
            await self._set_allocation(case_id, pod_name, assistant_type)
            logger.info(
                event="pod_allocated",
                message=f"Allocated {assistant_type} pod {pod_name} for case {case_id}",
                case_id=case_id,
                pod_name=pod_name,
                assistant_type=assistant_type
            )
            return pod_name

        return None

    async def release_pod(self, case_id: str) -> bool:
        """释放工单占用的Pod"""
        existing = await self._get_allocation(case_id)
        if existing:
            pod_name, assistant_type = existing
            await self._del_allocation(case_id)

            logger.info(
                event="pod_released",
                message=f"Releasing {assistant_type} pod {pod_name} for case {case_id}",
                case_id=case_id,
                pod_name=pod_name,
                assistant_type=assistant_type
            )

            pool = self.pool_manager.get_pool(assistant_type)
            if pool:
                await pool.release_pod(pod_name)
            else:
                # 池被移除，直接删除Pod
                self.k8s.delete_pod(pod_name)
            return True

        return False

    async def get_pod_for_case(self, case_id: str) -> str | None:
        """查询工单关联的Pod名称"""
        allocation = await self._get_allocation(case_id)
        return allocation[0] if allocation else None

    async def get_allocation_info(self, case_id: str) -> dict[str, str] | None:
        """查询工单分配详情（含类型）"""
        allocation = await self._get_allocation(case_id)
        if allocation:
            return {"pod_name": allocation[0], "assistant_type": allocation[1]}
        return None

    def get_endpoint_for_case_sync(self, pod_name: str, assistant_type: str) -> str | None:
        """根据 pod_name 解析 Pod endpoint (http://<pod_ip>:<port>)。"""
        pod_ip = self.k8s.get_pod_ip(pod_name)
        if not pod_ip:
            return None
        cfg = settings.assistant_registry.get(assistant_type, {})
        port = cfg.get("port", 18789)
        return f"http://{pod_ip}:{port}"

    async def get_status(self) -> dict:
        """获取服务状态"""
        all_allocs = await self._get_all_allocations()
        return {
            "allocated_cases": len(all_allocs),
            "pools": self.pool_manager.get_all_stats()
        }

    def get_available_assistants(self) -> list:
        """获取可用的AI助手列表（向后兼容：返回简单列表格式）"""
        return self.get_available_assistants_response()["assistants"]

    def get_available_assistants_response(self) -> dict:
        """获取可用AI助手列表及显示决策（v2.1 结构化响应）"""
        assistants = []
        available_count = 0
        default_assistant = None

        for atype, config in settings.assistant_registry.items():
            if config.get("enabled", True):
                pool = self.pool_manager.get_pool(atype)
                stats = pool.get_stats() if pool else {}

                # 判断可用性：直连模式(warm_pool_size=0)始终可用，Pod模式需要有空闲Pod
                is_direct_mode = config.get("warm_pool_size", 0) == 0 and config.get("max_pool_size", 0) == 0
                available = is_direct_mode or stats.get("idle_count", 0) > 0

                assistant_info = {
                    "type": atype,
                    "display_name": config.get("display_name") or config.get("name") or atype,
                    "description": config.get("description", ""),
                    "capabilities": config.get("capabilities", []),
                    "available": available,
                    "is_default": config.get("is_default", False),
                    "pool_stats": stats,
                }
                assistants.append(assistant_info)

                if available:
                    available_count += 1
                    if default_assistant is None or assistant_info["is_default"]:
                        default_assistant = atype

        # 计算 show_selector
        mode = settings.get_show_selector_mode()
        if mode == "true":
            show_selector = True
        elif mode == "false":
            show_selector = False
        else:  # auto - 多于1个可用助手时显示
            show_selector = available_count > 1

        return {
            "assistants": assistants,
            "show_selector": show_selector,
            "default_assistant": default_assistant,
            "selector_mode": mode,
        }
