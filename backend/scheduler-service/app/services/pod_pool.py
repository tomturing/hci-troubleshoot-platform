"""
Pod Pool - Warm Standby Pool Management
"""

import asyncio
import uuid
from typing import List, Optional, Set, Dict
from collections import deque

from .k8s_client import K8sClient
from shared.utils.logger import get_logger
from app.config import settings

logger = get_logger("pod-pool")

class PodPool:
    """OpenClaw Pod池管理器"""
    
    def __init__(self, k8s_client: K8sClient):
        self.k8s = k8s_client
        self.warm_pool_size = settings.WARM_POOL_SIZE
        self.max_pool_size = settings.MAX_POOL_SIZE
        
        # 内存中维护的空闲Pod名称列表 (实际上应该结合K8s状态，这里简化处理)
        # 在真实生产中，应该通过Label Selector查询 "status=idle" 的Pod
        self.idle_pods: deque = deque()
        self.active_pods: Set[str] = set()
        
        # 后台任务锁
        self._lock = asyncio.Lock()

    async def initialize(self):
        """初始化: 同步当前集群状态"""
        # TODO: 查询现有Pod并分类为 active/idle
        # 目前简单假设重启后为空
        pass

    async def ensure_warm_pool(self):
        """维护热备池大小"""
        async with self._lock:
            current_count = len(self.idle_pods) + len(self.active_pods)
            needed = self.warm_pool_size - len(self.idle_pods)
            
            if needed > 0:
                if current_count + needed > self.max_pool_size:
                    needed = self.max_pool_size - current_count
                
                if needed > 0:
                    logger.info(f"Warming up pool: creating {needed} new pods")
                    for _ in range(needed):
                        await self._create_warm_pod()

    async def _create_warm_pod(self):
        """创建一个新的热备Pod"""
        pod_name = f"openclaw-pool-{uuid.uuid4().hex[:8]}"
        # 创建时不带 case_id，表示为空闲
        if self.k8s.create_pod(pod_name):
            self.idle_pods.append(pod_name)
            logger.info(f"Added warm pod {pod_name} to pool")

    async def acquire_pod(self, case_id: str) -> Optional[str]:
        """从池中获取一个Pod"""
        async with self._lock:
            if not self.idle_pods:
                # 池空了，尝试立即创建 (如果没达到上限)
                if len(self.active_pods) < self.max_pool_size:
                     pod_name = f"openclaw-ondemand-{uuid.uuid4().hex[:8]}"
                     if self.k8s.create_pod(pod_name, case_id=case_id):
                         self.active_pods.add(pod_name)
                         return pod_name
                return None
            
            # 从池中取一个
            pod_name = self.idle_pods.popleft()
            self.active_pods.add(pod_name)
            
            # TODO: 更新Pod标签，绑定case_id (K8s API patch)
            # self.k8s.patch_pod_label(pod_name, "case-id", case_id)
            
            logger.info(f"Acquired pod {pod_name} for case {case_id}")
            
            # 触发异步补充池子
            asyncio.create_task(self.ensure_warm_pool())
            
            return pod_name

    async def release_pod(self, pod_name: str):
        """释放Pod回池 (或销毁)"""
        async with self._lock:
            if pod_name in self.active_pods:
                self.active_pods.remove(pod_name)
                
            # 策略: 如果池子满了，由cleanup任务销毁; 或者直接销毁
            # 简单策略: 总是销毁，保持纯净环境? 
            # 优化策略: 清除Context后复用? OpenClaw不支持清除Context API?
            # 假设OpenClaw是带状态的，复用需要清除Session。
            # 如果使用 create_namespaced_pod，通常是短生命周期。
            # 为了MVP稳定性，我们可以选择销毁旧Pod，并让ensure_warm_pool创建新的洁净Pod。
            
            logger.info(f"Releasing (terminating) pod {pod_name}")
            self.k8s.delete_pod(pod_name)
            
            # 触发补充 (创建新的洁净Pod)
            asyncio.create_task(self.ensure_warm_pool())

    def get_stats(self) -> Dict[str, int]:
        return {
            "idle": len(self.idle_pods),
            "active": len(self.active_pods),
            "total": len(self.idle_pods) + len(self.active_pods)
        }
