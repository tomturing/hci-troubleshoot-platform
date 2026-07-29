"""S0 分类驱动的完整知识清单接口测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import playbooks


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_inventory_returns_published_kbd_without_embedding_gate():
    signal = {
        "id": "sig_001",
        "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机失败"}},
        "match": None,
        "orchestrate": {"produces": [{"name": "VM", "path": "vm"}], "requires": []},
    }
    entry = SimpleNamespace(
        id=1,
        support_id="27123",
        title="虚拟机开机失败",
        category_id="虚拟机-003",
        status="published",
        signals_json={"schema_version": 2, "signals": [signal]},
        root_cause="镜像被占用",
        solution="解除占用",
        problem_description="虚拟机镜像忙",
        embedding=None,
        embedding_model=None,
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_ScalarResult([]), _ScalarResult([entry])]),
        commit=AsyncMock(),
    )
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))
    publisher = MagicMock()
    publisher.ensure_published = AsyncMock(return_value=SimpleNamespace())

    with (
        patch.object(playbooks, "_db_manager", db),
        patch.object(playbooks, "DynamicResourcePublisher", return_value=publisher),
        patch.object(playbooks, "kbd_resource_payload", return_value={}),
        patch.object(playbooks, "snapshot_revision_metadata", return_value={"revision": 4}),
    ):
        response = await playbooks.get_category_playbooks("虚拟机-003")

    assert response["sops"] == []
    assert response["kbds"][0]["support_id"] == "27123"
    assert response["kbds"][0]["executable"] is True
    assert response["kbds"][0]["signals"] == [signal]
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    sql = "\n".join(statements).lower()
    assert "embedding is not null" not in sql
    assert "embedding_model =" not in sql
    assert "tsv @@" not in sql


def test_backend_signal_without_matcher_or_output_is_visible_but_not_executable():
    signals = [{"id": "sig_002", "acquire": {"tool": "qfk_system", "args": {"command": "ps"}}}]
    assert playbooks._execution_issues(signals) == ["sig_002 必须且只能配置确定性 matcher 或有效产出变量"]


def test_qkv_with_residual_match_is_visible_but_not_executable():
    signals = [
        {
            "id": "sig_001",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机失败"}},
            "match": {"type": "keyword", "pattern": "", "expected": True},
            "orchestrate": {"produces": [{"name": "VM", "path": "vm"}]},
        }
    ]
    assert playbooks._execution_issues(signals) == ["sig_001 的 QKV 必须配置有效产出变量且 match 为 null"]


def test_backend_signal_with_output_variables_is_executable_without_matcher():
    """QFK 产出变量模式的 match=null 是 v2 合法执行契约，不能在快照阶段被过滤。"""
    signals = [
        {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "lsof"}},
            "match": None,
            "orchestrate": {"produces": [{"name": "PID", "type": "string", "path": ""}]},
        }
    ]

    assert playbooks._execution_issues(signals) == []


def test_backend_signal_with_match_and_output_is_not_executable():
    """二义的 QFK 信号必须在执行前被拒绝，和保存 v2 契约保持一致。"""
    signals = [
        {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "lsof"}},
            "match": {"type": "keyword", "pattern": "busy", "expected": True},
            "orchestrate": {"produces": [{"name": "PID", "type": "string", "path": ""}]},
        }
    ]

    assert playbooks._execution_issues(signals) == ["sig_002 必须且只能配置确定性 matcher 或有效产出变量"]


def test_stale_generation_metadata_is_visible_but_not_executable():
    signals = [
        {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
            "match": {"type": "exists", "expected": True},
        }
    ]
    metadata = {
        "schema_version": 1,
        "status": "stale",
        "source_fingerprint": "0" * 64,
        "prompt_revision": "1" * 64,
        "model_id": "model-v1",
        "tool_contract_revision": playbooks.current_tool_contract_revision(),
        "generation_fingerprint": "2" * 64,
    }

    issues = playbooks._execution_issues(
        signals,
        {
            "schema_version": 2,
            "signals": signals,
            "generation_metadata": metadata,
        },
    )

    assert issues == ["Signal/Contract 生成输入已变化，必须重新抽取或完成人工复核"]
