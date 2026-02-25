"""
Scheduler Service - 调度与生命周期管理
"""

import asyncio
from typing import Optional, Dict

from .k8s_client import K8sClient
from .pod_pool import PodPool
from shared.utils.logger import get_logger

logger = get_logger("scheduler-service")

class SchedulerService:
    """调度服务"""
    
    def __init__(self, k8s_client: K8sClient):
        self.k8s = k8s_client
        self.pool = PodPool(k8s_client)
        
        # 简单的内存映射 case_id -> pod_name
        # 在生产环境中应该使用 Redis
        self.allocations: Dict[str, str] = {}
        
    async def start(self):
        """启动服务，开始后台维护任务"""
        logger.info("Starting Scheduler Service background tasks")
        # 初始填充池子
        await self.pool.ensure_warm_pool()
        
    async def allocate_pod(self, case_id: str, trace_id: str) -> Optional[str]:
        """为工单分配Pod"""
        # 检查是否已有分配
        if case_id in self.allocations:
            pod_name = self.allocations[case_id]
            # 确认Pod还活着
            status = self.k8s.get_pod_status(pod_name)
            if status in ["Running", "Pending"]:
                logger.info(f"Reusing existing pod {pod_name} for case {case_id}")
                return pod_name
            else:
                # 已死，清理
                del self.allocations[case_id]
        
        # 从池中获取
        pod_name = await self.pool.acquire_pod(case_id)
        if pod_name:
            self.allocations[case_id] = pod_name
            logger.info(
                event="pod_allocated",
                message=f"Allocated pod {pod_name} for case {case_id}",
                case_id=case_id,
                pod_name=pod_name,
                trace_id=trace_id
            )
            return pod_name
            
        return None

    async def release_pod(self, case_id: str, trace_id: str) -> bool:
        """释放工单占用的Pod"""
        if case_id in self.allocations:
            pod_name = self.allocations[case_id]
            del self.allocations[case_id]
            
            logger.info(
                event="pod_released",
                message=f"Releasing pod {pod_name} for case {case_id}",
                case_id=case_id,
                pod_name=pod_name,
                trace_id=trace_id
            )
            
            await self.pool.release_pod(pod_name)
            return True
            
        return False

    async def get_pod_for_case(self, case_id: str) -> Optional[str]:
        """查询工单关联的Pod名称"""
        return self.allocations.get(case_id)

    def get_status(self) -> Dict:
        """获取服务状态"""
        pool_stats = self.pool.get_stats() if hasattr(self.pool, 'get_stats') else {}
        return {
            "allocated_cases": len(self.allocations),
            "pool": pool_stats
        }
