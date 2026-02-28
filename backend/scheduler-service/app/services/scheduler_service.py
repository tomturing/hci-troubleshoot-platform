"""
Scheduler Service - 多类型AI助手调度与生命周期管理 (v2.0)
"""

import asyncio
from typing import Optional, Dict, Any, Tuple

from .k8s_client import K8sClient
from .pod_pool import PodPoolManager
from shared.utils.logger import get_logger
from app.config import settings

logger = get_logger("scheduler-service")


class SchedulerService:
    """调度服务 (v2.0: 支持多类型AI助手)"""
    
    def __init__(self, k8s_client: K8sClient):
        self.k8s = k8s_client
        self.pool_manager = PodPoolManager(
            k8s_client=k8s_client,
            assistant_registry=settings.assistant_registry
        )
        
        # 内存映射: case_id -> (pod_name, assistant_type)
        # 在生产环境中应该使用 Redis
        self.allocations: Dict[str, Tuple[str, str]] = {}
        
    async def start(self):
        """启动服务，开始后台维护任务"""
        logger.info("Starting Scheduler Service background tasks (v2.0)")
        await self.pool_manager.ensure_all_warm_pools()
        
    async def allocate_pod(
        self,
        case_id: str,
        assistant_type: str = "openclaw"
    ) -> Optional[str]:
        """为工单分配指定类型的Pod"""
        # 检查是否已有分配
        if case_id in self.allocations:
            pod_name, existing_type = self.allocations[case_id]
            if existing_type == assistant_type:
                # 同类型，检查Pod还活着
                status = self.k8s.get_pod_status(pod_name)
                if status in ["Running", "Pending"]:
                    logger.info(f"Reusing existing {assistant_type} pod {pod_name} for case {case_id}")
                    return pod_name
            # 类型不同或Pod已死，清理
            del self.allocations[case_id]
        
        # 获取对应类型的Pod池
        pool = self.pool_manager.get_pool(assistant_type)
        if not pool:
            logger.error(f"No pool found for assistant type: {assistant_type}")
            return None
        
        # 从池中获取
        pod_name = await pool.acquire_pod(case_id)
        if pod_name:
            self.allocations[case_id] = (pod_name, assistant_type)
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
        if case_id in self.allocations:
            pod_name, assistant_type = self.allocations[case_id]
            del self.allocations[case_id]
            
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

    async def get_pod_for_case(self, case_id: str) -> Optional[str]:
        """查询工单关联的Pod名称"""
        allocation = self.allocations.get(case_id)
        return allocation[0] if allocation else None

    def get_allocation_info(self, case_id: str) -> Optional[Dict[str, str]]:
        """查询工单分配详情（含类型）"""
        allocation = self.allocations.get(case_id)
        if allocation:
            return {"pod_name": allocation[0], "assistant_type": allocation[1]}
        return None

    def get_endpoint_for_case(self, case_id: str) -> Optional[str]:
        """根据 case_id 解析 Pod endpoint (http://<pod_ip>:<port>)。"""
        info = self.get_allocation_info(case_id)
        if not info:
            return None
        pod_name = info["pod_name"]
        assistant_type = info["assistant_type"]
        pod_ip = self.k8s.get_pod_ip(pod_name)
        if not pod_ip:
            return None
        cfg = settings.assistant_registry.get(assistant_type, {})
        port = cfg.get("port", 18789)
        return f"http://{pod_ip}:{port}"

    def get_status(self) -> Dict:
        """获取服务状态"""
        return {
            "allocated_cases": len(self.allocations),
            "pools": self.pool_manager.get_all_stats()
        }
    
    def get_available_assistants(self) -> list:
        """获取可用的AI助手列表"""
        result = []
        for atype, config in settings.assistant_registry.items():
            if config.get("enabled", True):
                pool = self.pool_manager.get_pool(atype)
                stats = pool.get_stats() if pool else {}
                result.append({
                    "type": atype,
                    "name": config.get("name", atype),
                    "description": config.get("description", ""),
                    "enabled": True,
                    "pool_stats": stats
                })
        return result
