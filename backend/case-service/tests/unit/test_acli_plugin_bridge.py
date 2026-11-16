"""服务端 bridge 下发客户端单测：仅 exec 协议、已登录会话目标、凭据零泄露、结构化失败码。

同步测试 + asyncio.run；fake WS 注入，不依赖真实 bridge / websockets / pytest-asyncio。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.services.acli_plugin_bridge import (
    AcliPluginBridgeClient,
    BridgeDeployError,
    bridge_origin,
    build_exec_message,
    resolve_bridge_url,
)

BRIDGE_URL = "ws://terminal-bridge:9999"
CASE_ID = "htp-acli-test-0001"
NODE_IP = "172.28.24.1"
EXEC_ID = "exec-0001"
TRACE_ID = "trace-0001"
TESTKEY = "TESTKEY-placeholder"  # 占位：只用于断言凭据不出现在任何下发/回执里


class FakeWS:
    def __init__(self, incoming: list[dict], *, hang: bool = False, raise_timeout: bool = False):
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.closed = False
        self.hang = hang
        self.raise_timeout = raise_timeout

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self.incoming:
            await asyncio.sleep(0)
            return json.dumps(self.incoming.pop(0))
        if self.raise_timeout:
            raise TimeoutError
        if self.hang:
            await asyncio.Event().wait()
        raise AssertionError("fake ws 无更多消息")

    async def close(self) -> None:
        self.closed = True


def _client(fake: FakeWS, **kwargs):
    async def connector(url: str, **_kwargs):
        return fake
    return AcliPluginBridgeClient(connector=connector, **kwargs)


def _deploy(client: AcliPluginBridgeClient, *, command: str = "printf '%b' x > /tmp/x.sh && bash /tmp/x.sh"):
    return asyncio.run(client.deploy(
        bridge_url=BRIDGE_URL,
        case_id=CASE_ID,
        node_ip=NODE_IP,
        command=command,
        exec_id=EXEC_ID,
        trace_id=TRACE_ID,
        timeout=60,
    ))


def _exec_result(**overrides):
    payload = {
        "type": "exec_result",
        "exec_id": EXEC_ID,
        "stdout": "ACP_RESULT_BEGIN\n{}\nACP_RESULT_END\n",
        "stderr": "",
        "exit_code": 0,
    }
    payload.update(overrides)
    return payload


def test_deploy_only_sends_exec_and_never_credentials():
    fake = FakeWS([
        {"type": "bridge_hello", "version": "1"},  # 噪声：应被跳过
        _exec_result(),
    ])
    result = _deploy(_client(fake))

    assert [msg["type"] for msg in fake.sent] == ["ssh_exec_process"]
    assert "ssh_connect" not in [msg["type"] for msg in fake.sent]
    assert "ssh_disconnect" not in [msg["type"] for msg in fake.sent]
    exec_msg = fake.sent[0]
    assert exec_msg["case_id"] == CASE_ID
    assert exec_msg["node_ip"] == NODE_IP
    assert exec_msg["exec_id"] == EXEC_ID
    assert exec_msg["trace_id"] == TRACE_ID
    assert exec_msg["container"] == "host"
    assert exec_msg["command"].startswith("printf '%b' ")
    for field in ("password", "private_key", "passphrase", "credential", "auth_type", "ssh_port"):
        assert field not in exec_msg
    assert TESTKEY not in json.dumps(fake.sent)
    assert TESTKEY not in json.dumps(result)
    assert result["exit_code"] == 0
    assert fake.closed is True


def test_exec_result_is_matched_by_exec_id():
    fake = FakeWS([
        _exec_result(exec_id="other-exec", stdout="WRONG", exit_code=1),
        _exec_result(stdout="RIGHT"),
    ])
    assert _deploy(_client(fake))["stdout"] == "RIGHT"


def test_ssh_error_becomes_structured_code_without_credentials():
    fake = FakeWS([{"type": "ssh_error", "message": "会话不可用"}])
    with pytest.raises(BridgeDeployError) as exc:
        _deploy(_client(fake))
    assert exc.value.code == "ssh_connect_failed"
    assert "password" not in str(exc.value)
    assert "private_key" not in str(exc.value)
    assert all("password" not in msg and "private_key" not in msg for msg in fake.sent)
    assert fake.closed is True


def test_timeout_becomes_structured_code():
    fake = FakeWS([_exec_result(exec_id="ignored")], raise_timeout=True)
    with pytest.raises(BridgeDeployError) as exc:
        _deploy(_client(fake))
    assert exc.value.code == "bridge_timeout"


def test_connect_failure_becomes_structured_code():
    async def broken_connector(url: str, **_kwargs):
        raise OSError("connection refused")

    with pytest.raises(BridgeDeployError) as exc:
        _deploy(AcliPluginBridgeClient(connector=broken_connector))
    assert exc.value.code == "bridge_connect_failed"


@pytest.mark.parametrize(("case_id", "node_ip"), [("", NODE_IP), (CASE_ID, ""), ("", "")])
def test_deploy_requires_logged_in_session(case_id, node_ip):
    connected = []

    async def connector(url: str, **kwargs):
        connected.append(url)
        return FakeWS([])

    client = AcliPluginBridgeClient(connector=connector)
    with pytest.raises(BridgeDeployError) as exc:
        asyncio.run(client.deploy(
            bridge_url=BRIDGE_URL, case_id=case_id, node_ip=node_ip,
            command="true", exec_id=EXEC_ID, trace_id=TRACE_ID, timeout=5,
        ))
    assert exc.value.code == "session_missing"
    assert connected == []  # 校验发生在建连之前


def test_list_sessions_parses_rows_and_drops_credentials():
    fake = FakeWS([{
        "type": "list_sessions_result",
        "sessions": [
            {
                "case_id": "case-a", "node_ip": "172.28.24.1",
                "host": "HCI-01", "key": "case-a@172.28.24.1",
                "password": TESTKEY, "private_key": TESTKEY,
            },
            {"case_id": None, "node_ip": "172.28.24.2"},
            "not-a-dict",
        ],
    }])
    rows = asyncio.run(_client(fake).list_sessions(bridge_url=BRIDGE_URL, trace_id=TRACE_ID))

    assert fake.sent == [{"type": "list_sessions", "trace_id": TRACE_ID}]
    assert rows == [
        {"case_id": "case-a", "node_ip": "172.28.24.1", "host": "HCI-01", "key": "case-a@172.28.24.1"},
        {"case_id": "", "node_ip": "172.28.24.2", "host": "", "key": ""},
    ]
    assert TESTKEY not in json.dumps(rows)
    assert fake.closed is True


def test_list_sessions_timeout_becomes_structured_code():
    fake = FakeWS([], raise_timeout=True)
    with pytest.raises(BridgeDeployError) as exc:
        asyncio.run(_client(fake).list_sessions(bridge_url=BRIDGE_URL, trace_id=TRACE_ID))
    assert exc.value.code == "bridge_timeout"


def test_list_sessions_ssh_error_becomes_structured_code():
    fake = FakeWS([{"type": "ssh_error", "message": "会话列表读取失败"}])
    with pytest.raises(BridgeDeployError) as exc:
        asyncio.run(_client(fake).list_sessions(bridge_url=BRIDGE_URL, trace_id=TRACE_ID))
    assert exc.value.code == "ssh_connect_failed"
    assert fake.sent == [{"type": "list_sessions", "trace_id": TRACE_ID}]


def test_bridge_origin_is_same_origin_and_keeps_port():
    assert bridge_origin("ws://terminal-bridge:9999") == "http://terminal-bridge:9999"
    assert bridge_origin("wss://bridge.example.com") == "https://bridge.example.com"
    with pytest.raises(BridgeDeployError):
        resolve_bridge_url("", "ws://")


def test_deploy_connects_with_same_origin():
    captured = {}
    fake = FakeWS([_exec_result()])

    async def connector(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return fake

    _deploy(AcliPluginBridgeClient(connector=connector))
    assert captured["url"] == BRIDGE_URL
    assert captured["origin"] == "http://terminal-bridge:9999"
    assert captured["open_timeout"] == 15.0


def test_resolve_bridge_url_uses_default_when_no_override():
    assert resolve_bridge_url("", BRIDGE_URL) == BRIDGE_URL
    with pytest.raises(BridgeDeployError) as exc:
        resolve_bridge_url("", "http://not-ws")
    assert exc.value.code == "bridge_url_invalid"


def test_exec_message_has_no_credential_fields():
    msg = build_exec_message(
        case_id=CASE_ID, node_ip=NODE_IP, exec_id=EXEC_ID,
        command="true", trace_id=TRACE_ID, timeout=60,
    )
    assert msg["type"] == "ssh_exec_process"
    assert msg["case_id"] == CASE_ID
    assert msg["node_ip"] == NODE_IP
    assert msg["exec_id"] == EXEC_ID
    assert msg["trace_id"] == TRACE_ID
    assert msg["container"] == "host"
    assert msg["timeout"] == 60
    for field in ("password", "private_key", "passphrase", "auth_type", "credential_ref", "ssh_port"):
        assert field not in msg
