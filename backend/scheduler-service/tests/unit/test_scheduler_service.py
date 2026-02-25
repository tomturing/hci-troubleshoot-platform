"""
Unit Tests for Scheduler Service
"""

import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import sys
import os

# Add backend/scheduler-service to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
# Add backend to path (for shared)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from app.services.scheduler_service import SchedulerService

class TestSchedulerService(unittest.IsolatedAsyncioTestCase):
    
    def setUp(self):
        self.mock_k8s = MagicMock()
        # Mock PodPool inside SchedulerService
        # Since SchedulerService instantiates PodPool in __init__, we need to patch it or mock it after init
        # Easier to mock K8sClient passed to it, and mock pool methods attached to service.pool
        
        self.service = SchedulerService(self.mock_k8s)
        self.service.pool = MagicMock()
        self.service.pool.acquire_pod = AsyncMock()
        self.service.pool.release_pod = AsyncMock()
        
    async def test_allocate_pod_reuse(self):
        """Test reusing allocated pod"""
        case_id = "test-case-1"
        pod_name = "existing-pod"
        trace_id = "trace-1"
        
        self.service.allocations[case_id] = pod_name
        self.mock_k8s.get_pod_status.return_value = "Running"
        
        result = await self.service.allocate_pod(case_id, trace_id)
        
        self.assertEqual(result, pod_name)
        self.service.pool.acquire_pod.assert_not_called()
        
    async def test_allocate_pod_new(self):
        """Test allocating new pod from pool"""
        case_id = "test-case-2"
        pod_name = "new-pod"
        trace_id = "trace-2"
        
        self.service.pool.acquire_pod.return_value = pod_name
        
        result = await self.service.allocate_pod(case_id, trace_id)
        
        self.assertEqual(result, pod_name)
        self.assertEqual(self.service.allocations[case_id], pod_name)
        self.service.pool.acquire_pod.assert_called_once_with(case_id)
        
    async def test_release_pod(self):
        """Test releasing pod"""
        case_id = "test-case-3"
        pod_name = "released-pod"
        trace_id = "trace-3"
        
        self.service.allocations[case_id] = pod_name
        
        result = await self.service.release_pod(case_id, trace_id)
        
        self.assertTrue(result)
        self.assertNotIn(case_id, self.service.allocations)
        self.service.pool.release_pod.assert_called_once_with(pod_name)

if __name__ == '__main__':
    unittest.main()
