"""
ReAct 端到端集成测试

覆盖场景：
1. 低风险工具（risk_level=1）自动执行，SSE 事件序列：thinking → tool_executing → message
2. 高风险工具（risk_level=2）触发 confirm_request SSE 事件，不自动执行
3. 工具执行后 tool_audit_log 表有对应记录

所有测试使用 mock adapter，不依赖外部 SCP/SSH 服务。
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

# 复用 conftest 中的 app 和 fixture

# ─── Mock 工具返回数据 ──────────────────────────────────────────────────────────

MOCK_SCP_VM_LIST = {
    "total": 2,
    "vms": [
        {"id": "vm-001", "name": "test-vm-1", "status": "running", "host_name": "node-1"},
        {"id": "vm-002", "name": "test-vm-2", "status": "stopped", "host_name": "node-2"},
    ],
}

MOCK_ACLI_SYSTEM_TOP = {
    "cpu_usage": 45.2,
    "mem_total_mb": 32768,
    "mem_free_mb": 8192,
    "mem_usage_percent": 75.0,
}

MOCK_ACLI_SERVICE_RESTART = {
    "result": "success",
    "service": "exporter",
    "message": "服务已重启",
}

# ─── Mock LLM Response 类 ──────────────────────────────────────────────────────


def make_llm_response(content="", tool_calls=None, finish_reason="stop"):
    """创建 LLMResponse 对象"""
    from app.core.glm_client import LLMResponse, ToolCall

    tc_objects = [
        ToolCall(id=tc["id"], name=tc["name"], args=tc["args"])
        for tc in tool_calls
    ] if tool_calls else []

    return LLMResponse(
        content=content,
        tool_calls=tc_objects,
        finish_reason=finish_reason,
    )


# ─── SSE 事件收集辅助函数 ──────────────────────────────────────────────────────


async def collect_sse_events(response) -> list[dict]:
    """
    从 SSE 响应流中收集所有事件，解析为结构化列表。

    返回格式：
    [
        {"type": "thinking", "data": {...}},
        {"type": "tool_executing", "data": {...}},
        {"type": "message", "data": {"content": "..."}},
        ...
    ]
    """
    events = []
    current_event = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or line.startswith(":"):
            # 跳过空行和注释行
            continue

        if line.startswith("event:"):
            # 事件类型行，如：event: thinking
            event_type = line.split(":", 1)[1].strip()
            current_event = {"type": event_type, "data": None}
            events.append(current_event)

        elif line.startswith("data:"):
            # 数据行，如：data: {"content": "..."}
            data_str = line.split(":", 1)[1].strip()
            if data_str == "[DONE]":
                # 流结束标记
                events.append({"type": "done", "data": None})
                break
            try:
                data = json.loads(data_str)
                # 如果有当前事件，更新其 data
                if current_event is not None and current_event["data"] is None:
                    current_event["data"] = data
                    current_event = None  # 重置
                else:
                    # 没有 event 行的 data，作为 message 处理
                    events.append({"type": "message", "data": data})
            except json.JSONDecodeError:
                # 非 JSON 数据，直接存储字符串
                if current_event is not None and current_event["data"] is None:
                    current_event["data"] = {"raw": data_str}
                    current_event = None
                else:
                    events.append({"type": "message", "data": {"raw": data_str}})

    return events


# ─── 测试用例 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
class TestReactE2E:
    """ReAct 端到端集成测试"""

    BASE_URL = "http://test"

    @pytest.fixture(autouse=True)
    async def setup_test_case(self, setup_test_cases):
        """每个测试前准备测试工单数据"""
        # 使用 conftest 中的 setup_test_cases fixture
        pass

    async def _create_test_conversation(self, client, case_id: str = "Q202602220005"):
        """创建测试对话，返回 conversation_id"""
        try:
            response = await client.post(
                f"{self.BASE_URL}/api/conversations/",
                params={"case_id": case_id},
                headers={"X-Trace-ID": f"test-react-{uuid.uuid4()}"},
            )
        except Exception as e:
            pytest.skip(f"创建对话失败，可能数据库不可用：{e}")

        if response.status_code != 201:
            pytest.skip(f"创建对话返回 {response.status_code}: {response.text}")

        return response.json()["conversation_id"]

    async def test_react_low_risk_tool_auto_executes(self, async_client):
        """
        测试低风险工具（risk_level=1）自动执行流程。

        SSE 事件序列应为：
        1. thinking（思考中）
        2. tool_executing（工具执行通知）
        3. message（AI 文本回复）
        4. done（流结束）
        """
        # 1. 创建对话
        conv_id = await self._create_test_conversation(async_client)

        # 2. Mock GLM 返回工具调用（get_vm_list，risk_level=1）
        call_count = 0

        async def mock_glm_chat(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一次调用：返回工具调用
                return make_llm_response(
                    content="",
                    tool_calls=[{"id": "call_001", "name": "get_vm_list", "args": {"limit": 10}}],
                    finish_reason="tool_calls",
                )
            else:
                # 第二次调用：返回最终回复
                return make_llm_response(
                    content="已查询到 2 台虚拟机，其中 test-vm-1 正在运行，test-vm-2 已停止。",
                    tool_calls=[],
                    finish_reason="stop",
                )

        # 3. Mock ToolRouter.execute 返回
        mock_tool_result = MOCK_SCP_VM_LIST

        # 捕获审计调用
        audit_calls = []

        async def mock_audit_write(**kwargs):
            audit_calls.append(kwargs)

        # 检查 ReAct 是否可用
        if not async_client.app.state.glm_client or not async_client.app.state.tool_router:
            pytest.skip("ReAct 组件未初始化，跳过测试")

        with patch.object(async_client.app.state.glm_client, 'chat', new=AsyncMock(side_effect=mock_glm_chat)):
            with patch.object(async_client.app.state.tool_router, 'execute', new=AsyncMock(return_value=mock_tool_result)):
                with patch.object(async_client.app.state._audit_service, 'write', new=AsyncMock(side_effect=mock_audit_write)):
                    # 发送消息
                    response = await async_client.post(
                        f"{self.BASE_URL}/api/conversations/{conv_id}/message",
                        json={
                            "role": "user",
                            "content": "请帮我查询虚拟机列表",
                            "case_id": "Q202602220005",
                        },
                        headers={"X-Trace-ID": f"test-react-{uuid.uuid4()}"},
                    )

                    assert response.status_code == 200, f"请求失败：{response.text}"

                    # 收集 SSE 事件
                    events = await collect_sse_events(response)

                    # 验证事件序列
                    event_types = [e["type"] for e in events]
                    assert "thinking" in event_types, f"缺少 thinking 事件，实际事件：{event_types}"
                    assert "tool_executing" in event_types, f"缺少 tool_executing 事件，实际事件：{event_types}"

                    # 验证有文本回复
                    message_events = [e for e in events if e.get("data") and "content" in e.get("data", {})]
                    assert len(message_events) > 0, "缺少 AI 文本回复"

                    # 验证 audit service 被调用
                    assert len(audit_calls) > 0, "审计服务未被调用"

    async def test_react_high_risk_triggers_confirm_request(self, async_client, db_session):
        """
        测试高风险工具（risk_level=2）触发 confirm_request SSE 事件。

        预期行为：
        1. thinking 事件
        2. confirm_request 事件（不自动执行，等待用户确认）
        3. 用户确认前不发出 tool_executing 事件
        """
        # 1. 创建对话
        conv_id = await self._create_test_conversation(async_client)

        # 2. Mock GLM 返回工具调用（acli_service_restart，risk_level=2）
        call_count = 0

        async def mock_glm_chat(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_llm_response(
                    content="",
                    tool_calls=[{
                        "id": "call_002",
                        "name": "acli_service_restart",
                        "args": {"service_name": "exporter", "node_ip": "192.168.1.100"},
                    }],
                    finish_reason="tool_calls",
                )
            else:
                # 用户取消后的回复
                return make_llm_response(
                    content="您已取消重启服务的操作。",
                    tool_calls=[],
                    finish_reason="stop",
                )

        # 检查 ReAct 是否可用
        if not async_client.app.state.glm_client or not async_client.app.state.tool_router:
            pytest.skip("ReAct 组件未初始化，跳过测试")

        with patch.object(async_client.app.state.glm_client, 'chat', new=AsyncMock(side_effect=mock_glm_chat)):
            with patch.object(async_client.app.state.tool_router, 'execute', new=AsyncMock()) as mock_execute:
                # Mock confirm_service 返回 False（用户取消/超时）
                with patch.object(async_client.app.state.confirm_service, 'request_confirm',
                                 new=AsyncMock(return_value=False)) as mock_confirm:
                    # 发送消息
                    response = await async_client.post(
                        f"{self.BASE_URL}/api/conversations/{conv_id}/message",
                        json={
                            "role": "user",
                            "content": "请重启 exporter 服务",
                            "case_id": "Q202602220005",
                        },
                        headers={"X-Trace-ID": f"test-react-{uuid.uuid4()}"},
                    )

                    assert response.status_code == 200, f"请求失败：{response.text}"

                    # 收集 SSE 事件
                    events = await collect_sse_events(response)
                    event_types = [e["type"] for e in events]

                    # 验证 confirm_request 事件存在
                    assert "confirm_request" in event_types, f"缺少 confirm_request 事件，实际事件：{event_types}"

                    # 验证 confirm_service 被调用
                    assert mock_confirm.called, "确认服务未被调用"

                    # 验证用户取消前工具未被执行
                    assert mock_execute.call_count == 0, "高风险工具在用户确认前不应自动执行"

                    # 验证 confirm_request 事件包含必要字段
                    confirm_event = next((e for e in events if e["type"] == "confirm_request"), None)
                    assert confirm_event is not None
                    assert confirm_event.get("data") is not None
                    data = confirm_event["data"]
                    assert data.get("tool_name") == "acli_service_restart"
                    assert data.get("risk_level") == 2

    async def test_tool_audit_log_written_after_execution(self, async_client, db_session):
        """
        测试工具执行后 tool_audit_log 表有对应记录。

        验证点：
        1. 工具执行完成后，DB 中存在对应的审计记录
        2. 审计记录包含 tool_name、session_id、risk_level、duration_ms 等字段
        """
        # 1. 创建对话
        conv_id = await self._create_test_conversation(async_client)

        # 2. Mock GLM 返回工具调用
        call_count = 0

        async def mock_glm_chat(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_llm_response(
                    content="",
                    tool_calls=[{
                        "id": "call_003",
                        "name": "acli_system_top",
                        "args": {"node_ip": "192.168.1.100"},
                    }],
                    finish_reason="tool_calls",
                )
            else:
                return make_llm_response(
                    content="系统资源使用情况：CPU 使用率 45.2%，内存使用率 75%。",
                    tool_calls=[],
                    finish_reason="stop",
                )

        mock_tool_result = MOCK_ACLI_SYSTEM_TOP

        # 检查 ReAct 是否可用
        if not async_client.app.state.glm_client or not async_client.app.state.tool_router:
            pytest.skip("ReAct 组件未初始化，跳过测试")

        # 导入真实审计服务和模型
        from app.models.tool_audit_log import ToolAuditLog
        from app.services.audit_service import AuditService
        from sqlalchemy import select

        # 使用真实 DB session 创建审计服务
        audit_calls = []

        async def capture_and_write_audit(**kwargs):
            audit_calls.append(kwargs)
            # 实际写入 DB
            from sqlalchemy import insert
            await db_session.execute(insert(ToolAuditLog).values(**kwargs))
            await db_session.commit()

        with patch.object(async_client.app.state.glm_client, 'chat', new=AsyncMock(side_effect=mock_glm_chat)):
            with patch.object(async_client.app.state.tool_router, 'execute', new=AsyncMock(return_value=mock_tool_result)):
                # 使用真实审计服务写入 DB
                real_audit = AuditService(db=db_session)
                # 但我们需要 capture 调用，所以 patch write 方法
                with patch.object(real_audit, 'write', new=AsyncMock(side_effect=capture_and_write_audit)):
                    async_client.app.state._audit_service = real_audit

                    # 发送消息
                    response = await async_client.post(
                        f"{self.BASE_URL}/api/conversations/{conv_id}/message",
                        json={
                            "role": "user",
                            "content": "请查询系统资源使用情况",
                            "case_id": "Q202602220005",
                        },
                        headers={"X-Trace-ID": f"test-react-{uuid.uuid4()}"},
                    )

                    assert response.status_code == 200, f"请求失败：{response.text}"

                    # 消费 SSE 流
                    async for _ in response.aiter_lines():
                        pass

                    # 等待一小段时间确保异步写入完成
                    import asyncio
                    await asyncio.sleep(0.5)

                    # 查询 DB 验证审计记录
                    result = await db_session.execute(
                        select(ToolAuditLog).where(
                            ToolAuditLog.session_id == str(conv_id)
                        )
                    )
                    audit_logs = result.scalars().all()

                    # 验证至少有一条审计记录
                    assert len(audit_logs) > 0, "tool_audit_log 表应有审计记录"

                    # 验证审计记录字段
                    log = audit_logs[0]
                    assert log.tool_name == "acli_system_top"
                    assert log.risk_level == 1
                    assert log.session_id == str(conv_id)
                    assert log.duration_ms is not None
                    assert log.started_at is not None
                    assert log.completed_at is not None

    async def test_react_full_cycle_with_confirm(self, async_client, db_session):
        """
        测试完整的 ReAct 循环：用户确认后继续执行。

        流程：
        1. thinking → confirm_request
        2. 用户确认（通过 Redis）
        3. tool_executing
        4. 最终 AI 回复
        """
        # 1. 创建对话
        conv_id = await self._create_test_conversation(async_client)

        # 2. Mock GLM 返回
        call_count = 0

        async def mock_glm_chat(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_llm_response(
                    content="",
                    tool_calls=[{
                        "id": "call_004",
                        "name": "acli_network_nic_up",
                        "args": {"nic_name": "eth0", "node_ip": "192.168.1.100"},
                    }],
                    finish_reason="tool_calls",
                )
            else:
                return make_llm_response(
                    content="网卡 eth0 已成功启用。",
                    tool_calls=[],
                    finish_reason="stop",
                )

        mock_tool_result = {"result": "success", "nic": "eth0"}

        # 检查 ReAct 是否可用
        if not async_client.app.state.glm_client or not async_client.app.state.tool_router:
            pytest.skip("ReAct 组件未初始化，跳过测试")

        with patch.object(async_client.app.state.glm_client, 'chat', new=AsyncMock(side_effect=mock_glm_chat)):
            with patch.object(async_client.app.state.tool_router, 'execute', new=AsyncMock(return_value=mock_tool_result)):
                # Mock confirm_service 返回 True（用户确认）
                with patch.object(async_client.app.state.confirm_service, 'request_confirm',
                                 new=AsyncMock(return_value=True)) as mock_confirm:
                    # 发送消息
                    response = await async_client.post(
                        f"{self.BASE_URL}/api/conversations/{conv_id}/message",
                        json={
                            "role": "user",
                            "content": "请启用 eth0 网卡",
                            "case_id": "Q202602220005",
                        },
                        headers={"X-Trace-ID": f"test-react-{uuid.uuid4()}"},
                    )

                    assert response.status_code == 200, f"请求失败：{response.text}"

                    # 收集 SSE 事件
                    events = await collect_sse_events(response)
                    event_types = [e["type"] for e in events]

                    # 验证完整事件序列
                    assert "thinking" in event_types, f"缺少 thinking 事件，实际：{event_types}"
                    assert "confirm_request" in event_types, f"缺少 confirm_request 事件，实际：{event_types}"
                    assert "tool_executing" in event_types, f"缺少 tool_executing 事件，实际：{event_types}"

                    # 验证确认服务被调用
                    assert mock_confirm.called
                    assert mock_confirm.return_value is True
