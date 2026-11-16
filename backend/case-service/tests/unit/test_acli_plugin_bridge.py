"""服务端 bridge 下发客户端单测：协议顺序、凭据不泄露、结构化失败码。

用同步测试 + ``asyncio.run``，避免依赖 pytest-asyncio；fake WS 注入使测试
不需要真实 bridge 或 websockets 包，同时能断言「页面/payload 里没有凭据」。
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
    build_ssh_connect_message,
    load_credentials,
    resolve_bridge_url,
)

PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nTESTKEY\n-----END OPENSSH PRIVATE KEY-----"
CREDENTIALS = json.dumps({"24": {"auth_type": "key", "username": "root", "private_key": PRIVATE_KEY}})


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
    return asyncio.run(
        client.deploy(
            bridge_url="ws://terminal-bridge:9999",
            node_ip="172.28.24.1",
            ssh_port=22,
            credential={"auth_type": "key", "username": "root", "private_key": PRIVATE_KEY},
            command=command,
            case_id="htp-acli-test-0001",
            exec_id="exec-0001",
            trace_id="trace-0001",
            timeout=60,
        )
    )


def test_deploy_sequence_and_credential_isolation():
    fake = FakeWS(
        [
            {"type": "bridge_hello", "version": "1"},
            {"type": "ssh_connected", "case_id": "htp-acli-test-0001"},
            {
                "type": "exec_result",
                "exec_id": "exec-0001",
                "stdout": "ACP_RESULT_BEGIN\n{}\nACP_RESULT_END\n",
                "stderr": "",
                "exit_code": 0,
            },
        ]
    )
    result = _deploy(_client(fake))

    assert [msg["type"] for msg in fake.sent[:2]] == ["ssh_connect", "ssh_exec_process"]
    connect_msg, exec_msg = fake.sent[0], fake.sent[1]
    assert connect_msg["host"] == "172.28.24.1"
    assert connect_msg["node_ip"] == "172.28.24.1"
    assert connect_msg["private_key"] == PRIVATE_KEY
    assert connect_msg["trace_id"] == "trace-0001"
    assert exec_msg["exec_id"] == "exec-0001"
    assert exec_msg["container"] == "host"
    assert exec_msg["command"].startswith("printf '%b' ")
    assert "private_key" not in exec_msg
    assert "password" not in exec_msg
    assert PRIVATE_KEY not in json.dumps(result)
    assert result["exit_code"] == 0
    assert fake.closed is True
    assert fake.sent[-1]["type"] == "ssh_disconnect"


def test_exec_result_is_matched_by_exec_id():
    fake = FakeWS(
        [
            {"type": "ssh_connected"},
            {"type": "exec_result", "exec_id": "other-exec", "stdout": "WRONG", "exit_code": 1},
            {"type": "exec_result", "exec_id": "exec-0001", "stdout": "RIGHT", "exit_code": 0},
        ]
    )
    assert _deploy(_client(fake))["stdout"] == "RIGHT"


def test_ssh_error_becomes_structured_code_without_credentials():
    fake = FakeWS([{"type": "ssh_error", "message": "认证失败"}])
    with pytest.raises(BridgeDeployError) as exc:
        _deploy(_client(fake))
    assert exc.value.code == "ssh_connect_failed"
    assert PRIVATE_KEY not in str(exc.value)


def test_timeout_becomes_structured_code():
    fake = FakeWS(
        [
            {"type": "ssh_connected"},
            {"type": "exec_result", "exec_id": "ignored", "stdout": "", "exit_code": 0},
        ],
        raise_timeout=True,
    )
    # 首条 exec_result 与 exec_id 不匹配，随后 recv 抛 TimeoutError。
    with pytest.raises(BridgeDeployError) as exc:
        _deploy(_client(fake))
    assert exc.value.code == "bridge_timeout"


def test_connect_failure_becomes_structured_code():
    async def broken_connector(url: str, **_kwargs):
        raise OSError("connection refused")

    client = AcliPluginBridgeClient(connector=broken_connector)
    with pytest.raises(BridgeDeployError) as exc:
        _deploy(client)
    assert exc.value.code == "bridge_connect_failed"


def test_bridge_origin_is_same_origin_and_keeps_port():
    assert bridge_origin("ws://terminal-bridge:9999") == "http://terminal-bridge:9999"
    assert bridge_origin("wss://bridge.example.com") == "https://bridge.example.com"
    with pytest.raises(BridgeDeployError):
        resolve_bridge_url("", "ws://")


def test_deploy_connects_with_same_origin():
    captured = {}
    fake = FakeWS(
        [
            {"type": "ssh_connected"},
            {"type": "exec_result", "exec_id": "exec-0001", "stdout": "ok", "exit_code": 0},
        ]
    )

    async def connector(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return fake

    _deploy(AcliPluginBridgeClient(connector=connector))
    assert captured["url"] == "ws://terminal-bridge:9999"
    assert captured["origin"] == "http://terminal-bridge:9999"


def test_load_credentials_key_and_password_prefix():
    key_credential = load_credentials(CREDENTIALS, env_code="24")
    assert key_credential["auth_type"] == "key"
    assert key_credential["username"] == "root"
    assert key_credential["private_key"] == PRIVATE_KEY

    password_json = json.dumps({"25": {"auth_type": "password", "password": "prefix-only"}})
    password_credential = load_credentials(password_json, credential_ref="25")
    assert password_credential["password"] == "prefix-only"
    assert "private_key" not in password_credential


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        ("", "credential_missing"),
        ("not-json", "credential_invalid"),
        ("[]", "credential_invalid"),
        ("{}", "credential_missing"),
        (json.dumps({"24": {"auth_type": "lease", "password": "x"}}), "credential_invalid"),
        (json.dumps({"24": {"auth_type": "key"}}), "credential_missing"),
        (json.dumps({"24": {"auth_type": "password", "password": "fullsangfornetwork"}}), "credential_invalid"),
    ],
)
def test_load_credentials_rejects_invalid_payloads(raw, expected_code):
    with pytest.raises(BridgeDeployError) as exc:
        load_credentials(raw, env_code="24")
    assert exc.value.code == expected_code


def test_load_credentials_from_mounted_secret_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(CREDENTIALS, encoding="utf-8")
    assert load_credentials("", env_code="24", credentials_file=str(path))["auth_type"] == "key"


def test_resolve_bridge_url_prefers_environment_override():
    assert resolve_bridge_url("", "ws://terminal-bridge:9999") == "ws://terminal-bridge:9999"
    assert resolve_bridge_url("ws://windows-host:9999", "ws://terminal-bridge:9999") == "ws://windows-host:9999"
    with pytest.raises(BridgeDeployError) as exc:
        resolve_bridge_url("http://not-ws", "")
    assert exc.value.code == "bridge_url_invalid"


def test_message_builders_never_append_ssh_password_suffix():
    password_credential = {"auth_type": "password", "username": "root", "password": "prefix-only"}
    connect = build_ssh_connect_message(
        case_id="c", node_ip="172.28.24.1", ssh_port=22, credential=password_credential, trace_id="t"
    )
    assert connect["password"] == "prefix-only"
    assert "sangfornetwork" not in json.dumps(connect)

    key_credential = {"auth_type": "key", "username": "root", "private_key": PRIVATE_KEY, "passphrase": "pp"}
    key_connect = build_ssh_connect_message(
        case_id="c", node_ip="172.28.24.1", ssh_port=22, credential=key_credential, trace_id="t"
    )
    assert key_connect["passphrase"] == "pp"
    assert "password" not in key_connect

    exec_msg = build_exec_message(case_id="c", node_ip="172.28.24.1", exec_id="e", command="true", trace_id="t", timeout=60)
    assert exec_msg["type"] == "ssh_exec_process"
    assert exec_msg["timeout"] == 60
