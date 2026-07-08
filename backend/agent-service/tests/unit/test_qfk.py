"""
QFK 后端信号工具单元测试
验证 BackendSignal 加载验证、Handlers 命令构建及匹配、安全边界校验与引擎执行
"""

import os
import sys
from unittest.mock import AsyncMock, patch

# 注入工程后端路径以兼容测试规范
_backend = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
_agent_service = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent-service"))
if _backend not in sys.path:
    sys.path.insert(0, _backend)
if _agent_service not in sys.path:
    sys.path.insert(0, _agent_service)

import pytest
from app.tools.acli.executor import ExecResult
from app.tools.qfk import (
    BackendSignal,
    BackendSignalType,
    QFKResult,
    qfk_exec,
    qfk_load,
)
from app.tools.qfk.handlers import (
    CommandBuildError,
    HandlerRegistry,
    LogKeywordHandler,
)
from pydantic import ValidationError

# ─────────────────────────────────────────────────────────────────────────────
# BackendSignal 加载与数据模型校验测试
# ─────────────────────────────────────────────────────────────────────────────


class TestBackendSignalValidation:
    """后端信号 Pydantic 校验测试"""

    def test_load_valid_log_signal(self):
        data = {
            "signal_type": "log_keyword",
            "target": {
                "scope": "主节点",
                "resource": "mysql-managed.log",
                "path": "/sf/log/today/",
            },
            "keywords": ["file system read-only"],
            "match_mode": "any",
            "expected": True,
            "description": "主备传输文件系统只读"
        }
        sig = qfk_load(data)
        assert sig.signal_type == BackendSignalType.LOG_KEYWORD
        assert sig.target.resource == "mysql-managed.log"
        assert sig.keywords == ["file system read-only"]
        assert sig.expected is True

    def test_load_invalid_signal_type(self):
        data = {
            "signal_type": "invalid_type_name",
            "keywords": ["test"]
        }
        with pytest.raises(ValidationError):
            qfk_load(data)

    def test_missing_required_fields(self):
        # keywords 是必填（或默认为空，但 signal_type 是一定要的）
        data = {
            "target": {"scope": "主节点"}
        }
        with pytest.raises(ValidationError):
            qfk_load(data)

    def test_load_from_json_string(self):
        json_str = '{"signal_type": "service_status", "keywords": ["vs_mongo_host_state"], "expected": false}'
        sig = qfk_load(json_str)
        assert sig.signal_type == BackendSignalType.SERVICE_STATUS
        assert sig.keywords == ["vs_mongo_host_state"]
        assert sig.expected is False


# ─────────────────────────────────────────────────────────────────────────────
# Handler Registry 与 Command Builder 测试
# ─────────────────────────────────────────────────────────────────────────────


class TestHandlerRegistryAndBuilders:
    """后端信号处理器路由与命令拼装测试"""

    def test_registry_routing(self):
        for sig_type in BackendSignalType:
            handler = HandlerRegistry.get(sig_type)
            assert handler is not None

    def test_log_keyword_builder(self):
        # 正常构建
        sig = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            target={"resource": "vtpdaemon.log", "path": "/sf/log/today/", "time_window": "2026-07-01"},
            keywords=["HA state change"]
        )
        handler = HandlerRegistry.get(BackendSignalType.LOG_KEYWORD)
        cmds = handler.build_commands(sig)
        assert len(cmds) == 1
        assert "acli log get" in cmds[0]
        assert "-k 'HA state change'" in cmds[0]
        assert "-f vtpdaemon.log" in cmds[0]
        assert "-p /sf/log/today/" in cmds[0]
        assert "-t 2026-07-01" in cmds[0]

    def test_log_keyword_missing_keywords(self):
        # 没有 keywords 应报错
        sig = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            target={"resource": "vtpdaemon.log"}
        )
        handler = HandlerRegistry.get(BackendSignalType.LOG_KEYWORD)
        with pytest.raises(CommandBuildError, match="必须提供关键字"):
            handler.build_commands(sig)

    def test_log_keyword_path_traversal_defense(self):
        # 校验文件名不能有 /
        sig1 = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            target={"resource": "../etc/shadow"},
            keywords=["test"]
        )
        handler = HandlerRegistry.get(BackendSignalType.LOG_KEYWORD)
        with pytest.raises(CommandBuildError, match="不能包含路径"):
            handler.build_commands(sig1)

        # 校验路径前缀合法性
        sig2 = BackendSignal(
            signal_type=BackendSignalType.LOG_KEYWORD,
            target={"path": "/var/log/nginx/"},
            keywords=["test"]
        )
        with pytest.raises(CommandBuildError, match="只允许以"):
            handler.build_commands(sig2)


    def test_service_status_builder(self):
        sig = BackendSignal(
            signal_type=BackendSignalType.SERVICE_STATUS,
            target={"resource": "redis"},
            container="asv"
        )
        handler = HandlerRegistry.get(BackendSignalType.SERVICE_STATUS)
        cmds = handler.build_commands(sig)
        assert cmds == ["acli service asv redis status"]

    def test_service_status_missing_name(self):
        sig = BackendSignal(signal_type=BackendSignalType.SERVICE_STATUS)
        handler = HandlerRegistry.get(BackendSignalType.SERVICE_STATUS)
        with pytest.raises(CommandBuildError, match="必须通过 target.resource"):
            handler.build_commands(sig)

    def test_service_status_injection_blocked(self):
        # 服务名非法字符拦截
        sig = BackendSignal(
            signal_type=BackendSignalType.SERVICE_STATUS,
            target={"resource": "redis; rm -rf /"}
        )
        handler = HandlerRegistry.get(BackendSignalType.SERVICE_STATUS)
        with pytest.raises(CommandBuildError, match="非法服务名称"):
            handler.build_commands(sig)

    def test_service_status_invalid_container(self):
        sig = BackendSignal(
            signal_type=BackendSignalType.SERVICE_STATUS,
            target={"resource": "redis"},
            container="invalid_cont"
        )
        handler = HandlerRegistry.get(BackendSignalType.SERVICE_STATUS)
        with pytest.raises(CommandBuildError, match="非法服务容器"):
            handler.build_commands(sig)

    def test_generic_sub_command_builder(self):
        sig = BackendSignal(
            signal_type=BackendSignalType.VM_STATE,
            sub_command="list"
        )
        handler = HandlerRegistry.get(BackendSignalType.VM_STATE)
        cmds = handler.build_commands(sig)
        assert cmds == ["acli vm list"]

    def test_generic_sub_command_missing_sub(self):
        sig = BackendSignal(signal_type=BackendSignalType.VM_STATE)
        handler = HandlerRegistry.get(BackendSignalType.VM_STATE)
        with pytest.raises(CommandBuildError, match="必须在 sub_command 属性中"):
            handler.build_commands(sig)

    def test_generic_sub_command_injection_blocked(self):
        # 拦截管道等非法字符
        sig = BackendSignal(
            signal_type=BackendSignalType.VM_STATE,
            sub_command="list | cat /etc/shadow"
        )
        handler = HandlerRegistry.get(BackendSignalType.VM_STATE)
        with pytest.raises(CommandBuildError, match="包含非法字符"):
            handler.build_commands(sig)


# ─────────────────────────────────────────────────────────────────────────────
# Evaluator 关键字匹配测试
# ─────────────────────────────────────────────────────────────────────────────


class TestEvaluator:
    """评估匹配测试"""

    def test_any_mode_success(self):
        res = ExecResult(
            stdout="error: mysql connection failed\nfile system is healthy",
            stderr="",
            exit_code=0,
            command="test_cmd",
            node="127.0.0.1",
            duration_ms=10,
            truncated=False,
            risk_level=1
        )
        handler = LogKeywordHandler()
        matched, evidence = handler.evaluate([res], ["failed", "unrelated"], "any")
        assert matched is True
        assert "【关键字对比评估 (OR)】" in evidence
        assert "命中的关键字: ['failed']" in evidence

    def test_all_mode_success(self):
        res = ExecResult(
            stdout="error: mysql connection failed\nfile system read-only",
            stderr="",
            exit_code=0,
            command="test_cmd",
            node="127.0.0.1",
            duration_ms=10,
            truncated=False,
            risk_level=1
        )
        handler = LogKeywordHandler()
        matched, evidence = handler.evaluate([res], ["failed", "read-only"], "all")
        assert matched is True

    def test_all_mode_failure_missing_one(self):
        res = ExecResult(
            stdout="error: mysql connection failed",
            stderr="",
            exit_code=0,
            command="test_cmd",
            node="127.0.0.1",
            duration_ms=10,
            truncated=False,
            risk_level=1
        )
        handler = LogKeywordHandler()
        matched, _ = handler.evaluate([res], ["failed", "read-only"], "all")
        assert matched is False


# ─────────────────────────────────────────────────────────────────────────────
# QFKResult 与 ReAct Observation 文本格式化测试
# ─────────────────────────────────────────────────────────────────────────────


class TestQFKResultFormatting:
    """输出格式化展示校验"""

    def test_to_observation(self):
        res = QFKResult(
            matched=True,
            signal_type="log_keyword",
            commands=["acli log get -k 'test'"],
            keywords=["test"],
            match_mode="any",
            matched_keywords=["test"],
            evidence="Matched evidence text here"
        )
        obs = res.to_observation()
        assert "QFK 排查状态: ✅ 符合排查判定" in obs
        assert "信号类型: log_keyword" in obs
        assert "Matched evidence text here" in obs


# ─────────────────────────────────────────────────────────────────────────────
# QFK Engine 执行测试 (Mock Executor)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qfk_engine_expected_true_matched():
    # 期望出现，且匹配到了 -> final_matched = True
    sig = BackendSignal(
        signal_type=BackendSignalType.SERVICE_STATUS,
        target={"resource": "redis"},
        keywords=["running"],
        expected=True
    )

    mock_exec_res = ExecResult(
        stdout="redis status is active (running)",
        stderr="",
        exit_code=0,
        command="acli service asv redis status",
        node="10.0.0.1",
        duration_ms=20,
        truncated=False,
        risk_level=1
    )

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = mock_exec_res

    with patch("app.tools.acli.executor._executor", mock_executor):
        res = await qfk_exec(sig, conversation_id="conv-123")
        assert res.matched is True
        assert res.matched_keywords == ["running"]
        assert "active (running)" in res.evidence
        mock_executor.execute.assert_called_once()


@pytest.mark.asyncio
async def test_qfk_engine_expected_false_matched_flip():
    # expected=False (期望不出现报错，健康指标)，匹配到了报错词 -> final_matched = False
    sig = BackendSignal(
        signal_type=BackendSignalType.LOG_KEYWORD,
        target={"resource": "vtpdaemon.log"},
        keywords=["OOM error"],
        expected=False
    )

    mock_exec_res = ExecResult(
        stdout="Fatal: OOM error detected on node",
        stderr="",
        exit_code=0,
        command="acli log get -k 'OOM error' -f vtpdaemon.log",
        node="10.0.0.1",
        duration_ms=20,
        truncated=False,
        risk_level=1
    )

    mock_executor = AsyncMock()
    mock_executor.execute.return_value = mock_exec_res

    with patch("app.tools.acli.executor._executor", mock_executor):
        res = await qfk_exec(sig, conversation_id="conv-123")
        # 匹配到了 OOM (matched=True)，但是 expected=False，故最终匹配 matched 应翻转为 False！
        assert res.matched is False
        assert res.matched_keywords == ["OOM error"]
# 触发CI重新运行
