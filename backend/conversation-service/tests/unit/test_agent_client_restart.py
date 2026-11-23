"""AgentClient 上游中断语义单测。

agent-service 被换副本（SIGKILL/OOM/连接重置）时，agent_client 原本把所有异常都报成
``{"type": "error"}``，用户于是看到「推理请求异常」这种错误归因。这里锁定新语义：
传输层中断 → ``server_restart``（调用方让前端重连），其余异常仍是 ``error``。
"""

import httpx
import pytest
from app.services.agent_client import AgentClient


class _FailingStream:
    """模拟 ``client.stream(...)`` 上下文在建立阶段抛传输异常。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args) -> bool:
        return False


async def _collect(client: AgentClient) -> list[dict]:
    events = []
    async for event in client.stream(
        assistant_type="ops-agent",
        session_id="sess-1",
        case_id="Q202601010001",
        user_id="user-1",
        messages=[{"role": "user", "content": "hi"}],
    ):
        events.append(event)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (httpx.ConnectError("connection refused"), "ConnectError"),
        (httpx.RemoteProtocolError("peer closed connection"), "RemoteProtocolError"),
        (httpx.ReadError("connection reset"), "ReadError"),
    ],
)
async def test_transport_interruption_reports_server_restart(monkeypatch, exc, expected_reason):
    client = AgentClient("http://agent-service:8005", internal_token="t")
    monkeypatch.setattr(httpx.AsyncClient, "stream", lambda self, *a, **k: _FailingStream(exc))

    events = await _collect(client)

    assert events == [{"type": "server_restart", "reason": expected_reason}]


@pytest.mark.asyncio
async def test_non_transport_error_stays_error(monkeypatch):
    """解析/鉴权类异常不能伪装成重启，否则前端会无意义地重连两次。"""
    client = AgentClient("http://agent-service:8005", internal_token="t")
    monkeypatch.setattr(
        httpx.AsyncClient,
        "stream",
        lambda self, *a, **k: _FailingStream(ValueError("bad payload")),
    )

    events = await _collect(client)

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "bad payload" in events[0]["message"]