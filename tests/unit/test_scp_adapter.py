"""
SCPAdapter 单元测试（使用 respx mock HTTP 调用）

覆盖：
  - get_active_alerts 正常响应解析
  - 连接超时时返回降级响应
  - get_vm_list name_filter 过滤逻辑
  - get_failed_tasks 过滤失败任务状态
  - execute() 统一分发逻辑
"""

import os
import sys

# 确保 app 指向 conversation-service
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                     "backend", "conversation-service"))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

import pytest

try:
    import httpx
    import respx
    HAS_RESPX = True
except ImportError:
    HAS_RESPX = False

pytestmark = pytest.mark.skipif(not HAS_RESPX, reason="需要安装 respx: uv add respx --dev")


@pytest.fixture
def scp():
    from app.adapters.scp_adapter import SCPAdapter
    return SCPAdapter(base_url="http://scp.test", api_key="test-token")


class TestGetActiveAlerts:

    @pytest.mark.asyncio
    async def test_normal_response_is_parsed(self, scp):
        mock_data = {
            "code": 0,
            "data": {
                "data": [
                    {"id": "a1", "name": "CPU过高", "level": "major", "status": "active",
                     "message": "CPU 使用率超过 90%", "created_at": "2026-03-23T10:00:00Z"},
                    {"id": "a2", "name": "内存不足", "level": "critical", "status": "active",
                     "message": "可用内存低于 10%", "created_at": "2026-03-23T09:00:00Z"},
                ],
                "total": 2,
            },
        }
        with respx.mock:
            respx.get("http://scp.test/janus/20180725/alarms").mock(
                return_value=httpx.Response(200, json=mock_data)
            )
            result = await scp.get_active_alerts(limit=10)

        assert result["total"] == 2
        assert len(result["alarms"]) == 2
        assert result["alarms"][0]["name"] == "CPU过高"
        assert result["alarms"][1]["level"] == "critical"

    @pytest.mark.asyncio
    async def test_connect_timeout_returns_degraded_response(self, scp):
        with respx.mock:
            respx.get("http://scp.test/janus/20180725/alarms").mock(
                side_effect=httpx.ConnectError("连接超时")
            )
            result = await scp.get_active_alerts()

        assert result.get("_degraded") is True
        assert "message" in result

    @pytest.mark.asyncio
    async def test_limit_param_restricts_result_count(self, scp):
        """limit=1 时只返回 1 条告警"""
        mock_data = {
            "data": {
                "data": [
                    {"id": "a1", "name": "告警1", "level": "minor"},
                    {"id": "a2", "name": "告警2", "level": "minor"},
                    {"id": "a3", "name": "告警3", "level": "minor"},
                ]
            }
        }
        with respx.mock:
            respx.get("http://scp.test/janus/20180725/alarms").mock(
                return_value=httpx.Response(200, json=mock_data)
            )
            result = await scp.get_active_alerts(limit=1)

        assert len(result["alarms"]) == 1


class TestGetVMList:

    @pytest.mark.asyncio
    async def test_name_filter_filters_vms(self, scp):
        mock_data = {
            "data": {
                "data": [
                    {"id": "vm1", "name": "prod-web-01", "status": "running"},
                    {"id": "vm2", "name": "test-db-01", "status": "stopped"},
                    {"id": "vm3", "name": "prod-db-01", "status": "running"},
                ]
            }
        }
        with respx.mock:
            respx.get("http://scp.test/janus/20240725/servers").mock(
                return_value=httpx.Response(200, json=mock_data)
            )
            result = await scp.get_vm_list(name_filter="prod")

        assert result["total"] == 2
        names = [v["name"] for v in result["vms"]]
        assert "prod-web-01" in names
        assert "prod-db-01" in names
        assert "test-db-01" not in names

    @pytest.mark.asyncio
    async def test_no_filter_returns_all(self, scp):
        mock_data = {
            "data": {
                "data": [
                    {"id": "vm1", "name": "vm-a"},
                    {"id": "vm2", "name": "vm-b"},
                ]
            }
        }
        with respx.mock:
            respx.get("http://scp.test/janus/20240725/servers").mock(
                return_value=httpx.Response(200, json=mock_data)
            )
            result = await scp.get_vm_list()

        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_timeout_returns_degraded(self, scp):
        with respx.mock:
            respx.get("http://scp.test/janus/20240725/servers").mock(
                side_effect=httpx.TimeoutException("超时")
            )
            result = await scp.get_vm_list()

        assert result.get("_degraded") is True


class TestGetFailedTasks:

    @pytest.mark.asyncio
    async def test_filters_failed_task_status(self, scp):
        mock_data = {
            "data": {
                "data": [
                    {"id": "t1", "name": "启动VM", "status": "success"},
                    {"id": "t2", "name": "关闭VM", "status": "failed", "error_message": "超时"},
                    {"id": "t3", "name": "迁移VM", "status": "error", "error_message": "存储不足"},
                ]
            }
        }
        with respx.mock:
            respx.get("http://scp.test/janus/20180725/tasks").mock(
                return_value=httpx.Response(200, json=mock_data)
            )
            result = await scp.get_failed_tasks()

        assert result["total_failed"] == 2
        assert result["tasks"][0]["status"] == "failed"


class TestExecuteDispatch:

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, scp):
        result = await scp.execute("unknown_tool", {})
        assert "error" in result
        assert "未实现" in result["error"]
