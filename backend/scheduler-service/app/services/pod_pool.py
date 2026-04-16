"""
Pod Pool - 多类型AI助手热备池管理 (v2.0)

变更记录:
- 实现 initialize() 方法，启动时扫描 K8s 集群同步已有 Pod
- 补全异步任务错误处理，避免 fire-and-forget 静默丢失异常
"""

import asyncio
import uuid
from collections import deque
from typing import Any

from shared.utils.logger import get_logger
from shared.utils.metrics import POD_POOL_ACTIVE, POD_POOL_IDLE

from .k8s_client import K8sClient

logger = get_logger("pod-pool")


def _safe_create_task(coro, *, name: str = "pool-task"):
    """创建带错误处理的异步任务，避免 fire-and-forget 静默丢失异常"""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_task_done_callback)
    return task


def _task_done_callback(task: asyncio.Task):
    """任务完成回调：记录未被捕获的异常"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(
            event="background_task_error",
            message=f"Background task '{task.get_name()}' failed: {exc}",
            error=str(exc)
        )


class PodPool:
    """单类型Pod池管理器"""

    def __init__(
        self,
        k8s_client: K8sClient,
        assistant_type: str,
        assistant_config: dict[str, Any]
    ):
        self.k8s = k8s_client
        self.assistant_type = assistant_type
        self.config = assistant_config
        self.warm_pool_size = assistant_config.get("warm_pool_size", 2)
        self.max_pool_size = assistant_config.get("max_pool_size", 10)

        # 内存中维护的空闲Pod名称列表
        self.idle_pods: deque = deque()
        self.active_pods: set[str] = set()

        # 后台任务锁
        self._lock = asyncio.Lock()

    async def initialize(self):
        """初始化: 扫描 K8s 集群中该类型的已有 Pod，按状态分类到 idle/active 集合"""
        labels = self.config.get("labels", {})
        label_selector = ",".join(f"{k}={v}" for k, v in labels.items())
        if not label_selector:
            label_selector = f"assistant-type={self.assistant_type}"

        try:
            existing_pods = self.k8s.list_pods(label_selector=label_selector)
            for pod in existing_pods:
                pod_name = pod.get("name", "")
                status = pod.get("status", "Unknown")
                assigned_case = pod.get("annotations", {}).get("case-id", "")

                if status in ("Running", "Pending"):
                    if assigned_case:
                        self.active_pods.add(pod_name)
                    else:
                        self.idle_pods.append(pod_name)
                elif status in ("Failed", "Unknown"):
                    # 清理异常 Pod
                    logger.warning(f"Cleaning up unhealthy pod {pod_name} (status={status})")
                    self.k8s.delete_pod(pod_name)

            logger.info(
                event="pool_initialized",
                message=f"Pool '{self.assistant_type}' synced from cluster",
                idle=len(self.idle_pods),
                active=len(self.active_pods),
            )
        except Exception as e:
            logger.warning(
                event="pool_init_skipped",
                message=f"Could not scan existing pods for '{self.assistant_type}': {e}",
                error=str(e)
            )
        finally:
            # 初始化后同步一次 Gauge，无论成功或跳过都反映当前实际状态
            self._update_metrics()

    async def ensure_warm_pool(self):
        """维护热备池大小"""
        async with self._lock:
            current_count = len(self.idle_pods) + len(self.active_pods)
            needed = self.warm_pool_size - len(self.idle_pods)

            if needed > 0:
                if current_count + needed > self.max_pool_size:
                    needed = self.max_pool_size - current_count

                if needed > 0:
                    logger.info(
                        f"Warming up {self.assistant_type} pool: creating {needed} new pods"
                    )
                    for _ in range(needed):
                        await self._create_warm_pod()

    async def _create_warm_pod(self):
        """创建一个新的热备Pod"""
        pod_name = f"{self.assistant_type}-pool-{uuid.uuid4().hex[:8]}"
        if self.k8s.create_pod(
            pod_name=pod_name,
            assistant_type=self.assistant_type,
            assistant_config=self.config
        ):
            self.idle_pods.append(pod_name)
            logger.info(f"Added warm pod {pod_name} to {self.assistant_type} pool")

    async def acquire_pod(self, case_id: str) -> str | None:
        """从池中获取一个Pod"""
        async with self._lock:
            if not self.idle_pods:
                # 池空了，尝试立即创建 (如果没达到上限)
                if len(self.active_pods) < self.max_pool_size:
                    pod_name = f"{self.assistant_type}-ondemand-{uuid.uuid4().hex[:8]}"
                    if self.k8s.create_pod(
                        pod_name=pod_name,
                        case_id=case_id,
                        assistant_type=self.assistant_type,
                        assistant_config=self.config
                    ):
                        self.active_pods.add(pod_name)
                        self._update_metrics()  # on-demand 创建后同步 Gauge
                        return pod_name
                return None

            # 从池中取一个
            pod_name = self.idle_pods.popleft()
            self.active_pods.add(pod_name)

            logger.info(f"Acquired pod {pod_name} for case {case_id} (type={self.assistant_type})")

            # 异步补充池子（带错误处理）
            _safe_create_task(self.ensure_warm_pool(), name=f"warm-{self.assistant_type}")

            self._update_metrics()
            return pod_name

    async def release_pod(self, pod_name: str):
        """释放Pod回池 (销毁后由 ensure_warm_pool 创建新的)"""
        async with self._lock:
            if pod_name in self.active_pods:
                self.active_pods.remove(pod_name)

            logger.info(f"Releasing (terminating) pod {pod_name} (type={self.assistant_type})")
            self.k8s.delete_pod(pod_name)

            # 异步补充（带错误处理）
            _safe_create_task(self.ensure_warm_pool(), name=f"warm-{self.assistant_type}")
            self._update_metrics()

    def get_stats(self) -> dict[str, Any]:
        return {
            "assistant_type": self.assistant_type,
            "idle": len(self.idle_pods),
            "active": len(self.active_pods),
            "total": len(self.idle_pods) + len(self.active_pods),
            "warm_pool_size": self.warm_pool_size,
            "max_pool_size": self.max_pool_size
        }

    def _update_metrics(self) -> None:
        """同步更新 Prometheus Gauge 指标，反映当前池状态"""
        POD_POOL_IDLE.labels(assistant_type=self.assistant_type).set(len(self.idle_pods))
        POD_POOL_ACTIVE.labels(assistant_type=self.assistant_type).set(len(self.active_pods))


class PodPoolManager:
    """多类型Pod池管理器 (v2.0)

    管理多个 PodPool 实例，每种 assistant_type 对应一个独立的池。
    """

    def __init__(self, k8s_client: K8sClient, assistant_registry: dict[str, Any]):
        self.k8s = k8s_client
        self.pools: dict[str, PodPool] = {}

        # 根据注册表初始化各类型的Pod池
        for assistant_type, config in assistant_registry.items():
            if config.get("enabled", True):
                self.pools[assistant_type] = PodPool(
                    k8s_client=k8s_client,
                    assistant_type=assistant_type,
                    assistant_config=config
                )
                logger.info(f"Initialized pod pool for assistant type: {assistant_type}")

    def get_pool(self, assistant_type: str) -> PodPool | None:
        """获取指定类型的Pod池"""
        return self.pools.get(assistant_type)

    async def initialize_all(self):
        """所有池扫描集群同步已有 Pod"""
        for pool in self.pools.values():
            await pool.initialize()

    async def ensure_all_warm_pools(self):
        """维护所有池的热备大小"""
        for pool in self.pools.values():
            await pool.ensure_warm_pool()

    def get_all_stats(self) -> dict[str, Any]:
        """获取所有池的统计信息"""
        return {
            atype: pool.get_stats()
            for atype, pool in self.pools.items()
        }

    def list_assistant_types(self) -> list:
        """列出所有已注册的助手类型"""
        return list(self.pools.keys())
