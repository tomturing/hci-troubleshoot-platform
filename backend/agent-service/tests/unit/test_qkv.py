"""
QKV 前端信号变量提取工具单元测试
验证 FrontendSignal 校验、命令拼装、返回 JSON 字段过滤清洗提取与引擎执行
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# 注入工程后端路径以兼容测试规范
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent-service"))
_backend = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _svc not in sys.path:
    sys.path.insert(0, _svc)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

import pytest
from app.tools.acli.executor import ExecResult
from app.tools.qkv import (
    FrontendQueryType,
    FrontendSignal,
    QKVResult,
    qkv_exec,
    qkv_load,
)
from app.tools.qkv.parser import parse_frontend_value
from pydantic import ValidationError

# ─────────────────────────────────────────────────────────────────────────────
# FrontendSignal 校验测试
# ─────────────────────────────────────────────────────────────────────────────


class TestFrontendSignalValidation:
    """验证数据校验与加载"""

    def test_load_valid_signal(self):
        data = {"query": "alert", "keyword": "配置存储服务备节点异常", "limit": 50}
        sig = qkv_load(data)
        assert sig.query == FrontendQueryType.ALERT
        assert sig.keyword == "配置存储服务备节点异常"
        assert sig.limit == 50

    def test_invalid_query_type(self):
        data = {"query": "invalid_type", "keyword": "test"}
        with pytest.raises(ValidationError):
            qkv_load(data)

    def test_load_from_json_string(self):
        json_str = '{"query": "task", "keyword": "启动虚拟机", "is_failed": true}'
        sig = qkv_load(json_str)
        assert sig.query == FrontendQueryType.TASK
        assert sig.keyword == "启动虚拟机"
        assert sig.is_failed is True

    def test_qkv_output_processing_is_part_of_same_signal(self):
        sig = qkv_load(
            {
                "acquire": {"tool": "qkv_task", "args": {"keyword": "虚拟机无法启动"}},
                "orchestrate": {
                    "produces": [{"name": "DESCRIPTION", "path": "description"}],
                    "output_processing": [
                        {
                            "mode": "assert",
                            "input": "{{DESCRIPTION}}",
                            "match": {"type": "threshold", "expected": True, "operator": ">", "value": 90},
                        }
                    ],
                },
            }
        )
        assert sig.output_processing[0]["mode"] == "assert"

    def test_qkv_output_processing_rejects_script_fields(self):
        with pytest.raises(ValidationError):
            qkv_load(
                {
                    "query": "task",
                    "keyword": "x",
                    "output_processing": [
                        {"id": "x", "mode": "derive", "input": "{{DESCRIPTION}}", "operation": "trim", "script": "x"}
                    ],
                }
            )


# ─────────────────────────────────────────────────────────────────────────────
# QKV Engine 实际指令组装测试
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qkv_command_build():
    # 告警命令组装
    sig_alert = FrontendSignal(query=FrontendQueryType.ALERT, keyword="备节点异常", limit=10)
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecResult(
        stdout="[]", stderr="", exit_code=0, command="", node="127.0.0.1", duration_ms=1, truncated=False, risk_level=1
    )

    with patch("app.tools.acli.executor._executor", mock_executor):
        await qkv_exec(sig_alert, conversation_id="test")
        mock_executor.execute.assert_called_with(
            tool_name="acli_exec",
            args={
                "command": "acli --formatter json alert get -k '备节点异常' -l 10",
                "reason": "QKV前端变量抽取: alert",
            },
            conversation_id="test",
            node_ip=None,
            risk_level=1,
            policy="auto",
            exec_id=None,
        )


@pytest.mark.asyncio
async def test_qkv_alias_fallback_runs_only_after_canonical_keyword_has_no_matches():
    signal = FrontendSignal(query=FrontendQueryType.TASK, keyword="开启虚拟机", limit=5)
    mock_executor = AsyncMock()
    mock_executor.execute.side_effect = [
        ExecResult(
            stdout='{"data": []}',
            stderr="",
            exit_code=0,
            command="",
            node="127.0.0.1",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
        ExecResult(
            stdout='{"data": [{"id": 7, "type": "启动虚拟机", "status": 2, "process": "完成"}]}',
            stderr="",
            exit_code=0,
            command="",
            node="127.0.0.1",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
    ]

    with patch("app.tools.acli.executor._executor", mock_executor):
        result = await qkv_exec(signal, conversation_id="test")

    assert result.success is True
    assert result.resolution["evidence"]["action_id"] == "vm.power_on"
    assert result.resolution["evidence"]["matched_keyword"] == "开启虚拟机"
    assert result.resolution["evidence"]["keyword_status"] == "ALIAS_MATCH"
    assert mock_executor.execute.await_count == 2
    commands = [call.kwargs["args"]["command"] for call in mock_executor.execute.await_args_list]
    assert commands[0] == "acli --formatter json task get -k '启动虚拟机' -l 5"
    assert commands[1] == "acli --formatter json task get -k '开启虚拟机' -l 5"


@pytest.mark.asyncio
async def test_qkv_dialog_searches_master_logs_and_extracts_end_request_id_host():
    signal = FrontendSignal(
        query=FrontendQueryType.DIALOG,
        keyword="编辑显卡核心失败",
        produces=[
            {"name": "END", "path": "end"},
            {"name": "REQUEST_ID", "path": "request_id"},
            {"name": "HOST", "path": "host"},
        ],
    )
    mock_executor = AsyncMock()
    request_id = "a5ed4ad9340ce338ba1ac71d13ffcfb9"
    mock_executor.execute.side_effect = [
        ExecResult(
            stdout=(
                "/sf/log/today/audit_log/root/cmd.log:[2026-07-30 10:00:00] "
                "acli log get -k '编辑显卡核心失败' -p /sf/log/today -c 2\n"
                f"/sf/log/today/api.log:[2026-07-30 10:01:02] 编辑显卡核心失败 request_id={request_id}:123"
            ),
            stderr="",
            exit_code=0,
            command="",
            node="172.28.24.1",
            duration_ms=1,
            truncated=False,
            risk_level=1,
            exec_id="dialog-1",
        ),
        ExecResult(
            stdout="",
            stderr="",
            exit_code=0,
            command="",
            node="172.28.24.1",
            duration_ms=1,
            truncated=False,
            risk_level=1,
            exec_id="dialog-2",
        ),
    ]

    with patch("app.tools.acli.executor._executor", mock_executor):
        result = await qkv_exec(signal, conversation_id="test", node_ip="172.28.24.1")

    assert result.success is True
    assert result.values == [
        {
            "request_id": request_id,
            "end": "2026-07-30 10:01:02",
            "line": (f"/sf/log/today/api.log:[2026-07-30 10:01:02] 编辑显卡核心失败 request_id={request_id}:123"),
            "host": "172.28.24.1",
        }
    ]
    assert mock_executor.execute.await_count == 2
    commands = [call.kwargs["args"]["command"] for call in mock_executor.execute.await_args_list]
    assert commands == [
        "acli log get -k '编辑显卡核心失败' -p /sf/log/today -c 2",
        "acli log get -k '编辑显卡核心失败' -p /sf/log/today/vt -c 2",
    ]
    assert all(" -l " not in command for command in commands)


@pytest.mark.asyncio
async def test_qkv_output_processing_derives_variable_and_reports_assertion():
    signal = FrontendSignal(
        query=FrontendQueryType.TASK,
        keyword="虚拟机无法启动",
        produces=[{"name": "DESCRIPTION", "path": "description"}],
        output_processing=[
            {
                "mode": "derive",
                "input": "{{DESCRIPTION}}",
                "name": "VM_NAME",
                "type": "string",
                "extract": {"type": "feature", "feature": "vm_name", "cardinality": "exactly_one"},
            },
            {
                "mode": "assert",
                "input": "{{DESCRIPTION}}",
                "match": {"type": "threshold", "expected": True, "operator": ">", "value": 90},
            },
        ],
    )
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecResult(
        stdout='{"data":[{"description":"虚拟机名称：vm-001，使用率：92%"}]}',
        stderr="",
        exit_code=0,
        command="",
        node="127.0.0.1",
        duration_ms=1,
        truncated=False,
        risk_level=1,
    )
    with patch("app.tools.acli.executor._executor", mock_executor):
        result = await qkv_exec(signal, conversation_id="test")

    assert result.success is True
    assert result.values[0]["description"] == "虚拟机名称：vm-001，使用率：92%"
    assert result.values[0]["vm_name"] == "vm-001"
    assert result.matched is True
    assert result.assertions[0]["status"] == "PASS"


@pytest.mark.asyncio
async def test_qkv_output_processing_agent_negative_assertion_is_false():
    signal = FrontendSignal(
        query=FrontendQueryType.TASK,
        keyword="资源占用",
        produces=[{"name": "DESCRIPTION", "path": "description"}],
        output_processing=[
            {
                "mode": "assert",
                "input": "{{DESCRIPTION}}",
                "match": {"type": "threshold", "expected": True, "operator": ">", "value": 90},
            }
        ],
    )
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecResult(
        stdout='{"data":[{"description":"使用率：80%"}]}',
        stderr="",
        exit_code=0,
        command="",
        node="127.0.0.1",
        duration_ms=1,
        truncated=False,
        risk_level=1,
    )
    with patch("app.tools.acli.executor._executor", mock_executor):
        result = await qkv_exec(signal, conversation_id="test")
    assert result.success is True
    assert result.matched is False
    assert result.assertions[0]["status"] == "FAIL"


@pytest.mark.asyncio
async def test_qkv_output_processing_agent_unknown_and_cardinality_are_not_success():
    unknown_signal = FrontendSignal(
        query=FrontendQueryType.TASK,
        keyword="缺少字段",
        produces=[{"name": "DESCRIPTION", "path": "description"}],
        output_processing=[
            {
                "mode": "assert",
                "input": "{{DESCRIPTION}}",
                "match": {"type": "threshold", "expected": True, "operator": ">", "value": 90},
            }
        ],
    )
    cardinality_signal = FrontendSignal(
        query=FrontendQueryType.TASK,
        keyword="多条记录",
        produces=[{"name": "DESCRIPTION", "path": "description"}],
        output_processing=[
            {
                "mode": "derive",
                "scope": "single",
                "input": "{{DESCRIPTION}}",
                "name": "DESCRIPTION_TEXT",
                "type": "string",
                "extract": {"type": "feature", "feature": "number", "cardinality": "first"},
            }
        ],
    )
    mock_executor = AsyncMock()
    mock_executor.execute.side_effect = [
        ExecResult(
            stdout='{"data":[{"other":"x"}]}',
            stderr="",
            exit_code=0,
            command="",
            node="127.0.0.1",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
        ExecResult(
            stdout='{"data":[{"description":"a"},{"description":"b"}]}',
            stderr="",
            exit_code=0,
            command="",
            node="127.0.0.1",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
    ]
    with patch("app.tools.acli.executor._executor", mock_executor):
        unknown = await qkv_exec(unknown_signal, conversation_id="test")
        cardinality = await qkv_exec(cardinality_signal, conversation_id="test")
    assert unknown.success is True
    assert unknown.matched is None
    assert unknown.assertions[0]["status"] == "UNKNOWN"
    assert cardinality.success is False
    assert "QFK_CARDINALITY_MISMATCH" in (cardinality.error or "")


@pytest.mark.asyncio
async def test_hci_sim_shared_route_logical_consumers_cover_processing_outcomes():
    """hci-sim 只回放一份 stdout，Agent 按每个逻辑消费者各自处理。

    这是 Bundle→Agent 的契约级测试：不在 Go 中复制后处理规则，四态仍由
    QKV Agent 的共享内核计算，避免模拟环境和生产运行时出现双重语义。
    """

    physical_route = {
        "route_key": {"argv": ["acli", "--formatter", "json", "task", "get", "-k", "资源占用", "-l", "100"]},
        "logical_consumers": [
            {
                "signal_id": "assert-percent",
                "produces": [{"name": "DESCRIPTION", "path": "description"}],
                "output_processing": [
                    {
                        "mode": "assert",
                        "input": "{{DESCRIPTION}}",
                        "match": {"type": "threshold", "expected": True, "operator": ">", "value": 90},
                    }
                ],
                "derived_variables": [],
                "processing_fingerprint": "sha256:assert-percent",
            },
            {
                "signal_id": "derive-description",
                "produces": [{"name": "DESCRIPTION", "path": "description"}],
                "output_processing": [
                    {
                        "mode": "derive",
                        "scope": "single",
                        "input": "{{DESCRIPTION}}",
                        "name": "DESCRIPTION_TEXT",
                        "type": "string",
                        "extract": {"type": "feature", "feature": "number", "cardinality": "first"},
                    }
                ],
                "derived_variables": ["DESCRIPTION_TEXT"],
                "processing_fingerprint": "sha256:derive-description",
            },
        ],
    }

    def as_signal(consumer: dict[str, object]) -> FrontendSignal:
        return FrontendSignal(
            query=FrontendQueryType.TASK,
            keyword="资源占用",
            produces=consumer["produces"],  # type: ignore[arg-type]
            output_processing=consumer["output_processing"],  # type: ignore[arg-type]
        )

    assert len(physical_route["logical_consumers"]) == 2
    assert (
        physical_route["logical_consumers"][0]["processing_fingerprint"]
        != physical_route["logical_consumers"][1]["processing_fingerprint"]
    )
    assert physical_route["logical_consumers"][1]["derived_variables"] == ["DESCRIPTION_TEXT"]

    assert_consumer = as_signal(physical_route["logical_consumers"][0])
    derive_consumer = as_signal(physical_route["logical_consumers"][1])
    mock_executor = AsyncMock()
    mock_executor.execute.side_effect = [
        ExecResult(
            stdout='{"data":[{"description":"使用率：92%"}]}',
            stderr="",
            exit_code=0,
            command="",
            node="sim",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
        ExecResult(
            stdout='{"data":[{"description":"使用率：80%"}]}',
            stderr="",
            exit_code=0,
            command="",
            node="sim",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
        ExecResult(
            stdout='{"data":[{"other":"missing"}]}',
            stderr="",
            exit_code=0,
            command="",
            node="sim",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
        ExecResult(
            stdout='{"data":[{"description":"a"},{"description":"b"}]}',
            stderr="",
            exit_code=0,
            command="",
            node="sim",
            duration_ms=1,
            truncated=False,
            risk_level=1,
        ),
    ]
    with patch("app.tools.acli.executor._executor", mock_executor):
        positive = await qkv_exec(assert_consumer, conversation_id="sim-positive")
        negative = await qkv_exec(assert_consumer, conversation_id="sim-negative")
        unknown = await qkv_exec(assert_consumer, conversation_id="sim-unknown")
        cardinality = await qkv_exec(derive_consumer, conversation_id="sim-cardinality")

    assert positive.success is True and positive.matched is True and positive.assertions[0]["status"] == "PASS"
    assert negative.success is True and negative.matched is False and negative.assertions[0]["status"] == "FAIL"
    assert unknown.success is True and unknown.matched is None and unknown.assertions[0]["status"] == "UNKNOWN"
    assert cardinality.success is False and "QFK_CARDINALITY_MISMATCH" in (cardinality.error or "")


@pytest.mark.asyncio
async def test_qkv_terminal_timeout_is_error_not_empty_success():
    signal = FrontendSignal(query=FrontendQueryType.TASK, keyword="启动虚拟机失败", is_failed=True)
    mock_executor = AsyncMock()
    mock_executor.execute.return_value = ExecResult(
        stdout="",
        stderr="执行超时（30秒），前端未响应",
        exit_code=-1,
        command="",
        node="unknown",
        duration_ms=30000,
        truncated=False,
        risk_level=1,
        exec_id="exec-timeout",
    )

    with patch("app.tools.acli.executor._executor", mock_executor):
        result = await qkv_exec(signal, conversation_id="test")

    assert result.success is False
    assert result.exec_id == "exec-timeout"
    assert "执行超时" in (result.error or "")

    # 失败任务命令组装
    sig_task = FrontendSignal(query=FrontendQueryType.TASK, keyword="启动虚拟机", is_failed=True, limit=5)
    mock_executor.reset_mock()
    with patch("app.tools.acli.executor._executor", mock_executor):
        await qkv_exec(sig_task, conversation_id="test")
        mock_executor.execute.assert_called_with(
            tool_name="acli_exec",
            args={
                "command": "acli --formatter json task get -k '启动虚拟机' -s failed -l 5",
                "reason": "QKV前端变量抽取: task",
            },
            conversation_id="test",
            node_ip=None,
            risk_level=1,
            policy="auto",
            exec_id=None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# QKV Parser 数据字段解析与提取器测试
# ─────────────────────────────────────────────────────────────────────────────


class TestQKVParser:
    """数据结构反序列化过滤提取测试"""

    def test_parse_alert_payload(self):
        # 模拟真实 alert JSON 输出
        alert_json = """
        {
          "data": [
            {
              "alert_type": "host_bond",
              "description": "主机聚合口故障...",
              "end": "2026-06-09 13:23:48",
              "hostname": "channel1",
              "id": 40,
              "object_name": "聚合口(channel1)",
              "object_type": "主机",
              "host": "SVR_aCloud_668",
              "vm": ""
            }
          ]
        }
        """
        vals = parse_frontend_value(FrontendQueryType.ALERT, alert_json)
        assert len(vals) == 1
        v = vals[0]
        assert v["alert_type"] == "host_bond"
        assert v["end"] == "2026-06-09 13:23:48"
        assert v["target"] == "聚合口(channel1)"
        assert v["host"] == "SVR_aCloud_668"
        assert v["vm"] == ""

    def test_parse_task_payload(self):
        # 模拟真实 task JSON 输出 (失败任务)
        task_json = """
        {
          "data": [
            {
              "alert_type": "启动虚拟机",
              "description": "没有主机能够启动这台虚拟机...",
              "end": "2026-07-01 14:19:06",
              "errcode_tracing": "0x0C000005",
              "host": "SVR_aCloud_668",
              "hostname": "SVR_aCloud_668",
              "process": "失败",
              "request_id": "a44b15a25299b233ed861dab1a5f52a4",
              "status": 3,
              "target": "gpu-driver",
              "type": "启动虚拟机",
              "vm": "8329600027293"
            }
          ]
        }
        """
        vals = parse_frontend_value(FrontendQueryType.TASK, task_json)
        assert len(vals) == 1
        v = vals[0]
        assert v["status"] == 3
        assert v["type"] == "启动虚拟机"
        assert v["end"] == "2026-07-01 14:19:06"
        assert v["host"] == "SVR_aCloud_668"
        assert v["vm"] == "8329600027293"
        assert v["errcode_tracing"] == "0x0C000005"
        assert v["request_id"] == "a44b15a25299b233ed861dab1a5f52a4"

    def test_parse_dialog_raw_text(self):
        stdout = "2026-07-08 10:00 [INFO] popup: reboot confirmed\n2026-07-08 10:01 [WARN] dismiss"
        vals = parse_frontend_value(FrontendQueryType.DIALOG, stdout)
        assert len(vals) == 2
        assert vals[0]["line"] == "2026-07-08 10:00 [INFO] popup: reboot confirmed"
        assert vals[1]["description"] == "2026-07-08 10:01 [WARN] dismiss"

    def test_parse_dialog_uses_context_time_and_trace_id_equals_shape(self):
        trace_id = "b5ed4ad9340ce338ba1ac71d13ffcfb8"
        stdout = f"/sf/log/today/api.log:[2026-07-30 11:22:33] 编辑失败\n/sf/log/today/api.log: trace_id={trace_id}"
        vals = parse_frontend_value(FrontendQueryType.DIALOG, stdout)
        assert vals[0]["request_id"] == trace_id
        assert vals[0]["end"] == "2026-07-30 11:22:33"


class TestQKVParserDynamicProduces:
    """produces 动态字段提取测试"""

    def test_extract_by_produces(self):
        """produces 非空时按规格动态提取，不走路由硬编码"""
        alert_json = """
        {
          "data": [
            {
              "alert_type": "host_bond",
              "host": "SVR_aCloud_668",
              "vm": "vm-1001",
              "custom_field": "extra_value"
            }
          ]
        }
        """
        produces = [
            {"name": "HOST", "path": "host"},
            {"name": "VM", "path": "vm"},
            {"name": "CUSTOM", "path": "custom_field"},
        ]
        vals = parse_frontend_value(FrontendQueryType.ALERT, alert_json, produces)
        assert len(vals) == 1
        v = vals[0]
        # produces 模式下 key 为 name.lower()
        assert v["host"] == "SVR_aCloud_668"
        assert v["vm"] == "vm-1001"
        assert v["custom"] == "extra_value"
        # 不应包含硬编码模式才有的字段
        assert "alert_type" not in v
        assert "description" not in v

    def test_extract_by_produces_multi_path_fallback(self):
        """path 支持 | 分隔的多路径容错"""
        alert_json = """
        {
          "data": [
            {"hostname": "node-001", "vm": "vm-1"}
          ]
        }
        """
        produces = [
            {"name": "HOST", "path": "host|hostname|hostid"},
        ]
        vals = parse_frontend_value(FrontendQueryType.ALERT, alert_json, produces)
        assert len(vals) == 1
        assert vals[0]["host"] == "node-001"

    def test_explicit_end_produce_normalizes_unix_timestamp(self):
        """显式 produces 与硬编码路径必须产出相同的绝对时间 END。"""

        task_json = '{"data": [{"end": 1767778352}]}'

        vals = parse_frontend_value(
            FrontendQueryType.TASK,
            task_json,
            [{"name": "END", "path": "end"}],
        )
        fallback = parse_frontend_value(FrontendQueryType.TASK, task_json)

        assert vals[0]["end"] == fallback[0]["end"]

    def test_end_auto_derives_date_variable(self):
        """无论显式声明 produces END 还是硬编码兜底，均自动派生 DATE (YYYY-MM-DD)。"""
        task_json = '{"data": [{"end": "2026-06-23 22:54:03", "type": "delete_vm"}]}'

        vals = parse_frontend_value(
            FrontendQueryType.TASK,
            task_json,
            [{"name": "END", "path": "end"}],
        )
        assert vals[0]["end"] == "2026-06-23 22:54:03"
        assert vals[0]["date"] == "2026-06-23"

        # 显式声明 DATE 时直接提取
        vals_explicit = parse_frontend_value(
            FrontendQueryType.TASK,
            task_json,
            [{"name": "END", "path": "end"}, {"name": "DATE", "path": "end"}],
        )
        assert vals_explicit[0]["date"] == "2026-06-23"

        # 硬编码兜底模式
        fallback = parse_frontend_value(FrontendQueryType.TASK, task_json)
        assert fallback[0]["end"] == "2026-06-23 22:54:03"
        assert fallback[0]["date"] == "2026-06-23"

    def test_explicit_request_id_produce_normalizes_leading_comma(self):
        """显式 produces 与硬编码路径均自动剥离 request_id 的前导逗号。"""
        raw_task = '{"data": [{"request_id": ",a678d3fb5fdf2af4e78e6dae896a06e2", "end": "2026-07-28 00:54:36"}]}'

        vals = parse_frontend_value(
            FrontendQueryType.TASK,
            raw_task,
            [{"name": "REQUEST_ID", "path": "request_id"}, {"name": "END", "path": "end"}],
        )
        fallback = parse_frontend_value(FrontendQueryType.TASK, raw_task)

        assert vals[0]["request_id"] == "a678d3fb5fdf2af4e78e6dae896a06e2"
        assert fallback[0]["request_id"] == "a678d3fb5fdf2af4e78e6dae896a06e2"

    def test_request_id_normalization_multi_ids_and_synthetic_format(self):
        """多 trace 逗号拼接取首个，合成与常规 ID 正常保留。"""
        raw_multi = '{"data": [{"request_id": ",trace_first,trace_second"}]}'
        vals_multi = parse_frontend_value(
            FrontendQueryType.TASK,
            raw_multi,
            [{"name": "REQUEST_ID", "path": "request_id"}],
        )
        assert vals_multi[0]["request_id"] == "trace_first"

        raw_sim = '{"data": [{"request_id": ",SIM-REQUEST-41464"}]}'
        vals_sim = parse_frontend_value(
            FrontendQueryType.TASK,
            raw_sim,
            [{"name": "REQUEST_ID", "path": "request_id"}],
        )
        assert vals_sim[0]["request_id"] == "SIM-REQUEST-41464"

    def test_extract_empty_produces_falls_back(self):
        """produces 为空时走硬编码兜底"""
        alert_json = """
        {
          "data": [
            {"alert_type": "host_bond", "host": "node-1", "vm": ""}
          ]
        }
        """
        vals = parse_frontend_value(FrontendQueryType.ALERT, alert_json, produces=None)
        assert len(vals) == 1
        assert vals[0]["alert_type"] == "host_bond"
        assert vals[0]["host"] == "node-1"

    def test_extract_by_produces_filters_empty(self):
        """produces 提取全空的条目应被过滤"""
        alert_json = """
        {
          "data": [
            {"irrelevant": "data"},
            {"host": "node-1"}
          ]
        }
        """
        produces = [{"name": "HOST", "path": "host"}]
        vals = parse_frontend_value(FrontendQueryType.ALERT, alert_json, produces)
        assert len(vals) == 1
        assert vals[0]["host"] == "node-1"


# ─────────────────────────────────────────────────────────────────────────────
# QKVResult 格式化测试
# ─────────────────────────────────────────────────────────────────────────────


def test_qkv_result_to_observation():
    res = QKVResult(
        success=True,
        query="alert",
        keyword="只读",
        command="acli alert get -k 只读",
        values=[{"alert_type": "read-only", "host": "node-1", "vm": "vm-123"}],
    )
    obs = res.to_observation()
    assert "QKV 查询状态: 成功查找到 1 条记录" in obs
    assert "node-1" in obs
    assert "vm-123" in obs


@pytest.mark.asyncio
async def test_qkv_records_langfuse_tool_observation_without_output_body():
    signal = FrontendSignal(query=FrontendQueryType.ALERT, keyword="只读", limit=1)
    exec_result = ExecResult(
        stdout='{"data": []}',
        stderr="",
        exit_code=0,
        command="acli --formatter json alert get",
        node="10.0.0.1",
        duration_ms=42,
        truncated=False,
        risk_level=1,
        exec_id="exec-qkv-observe",
        trace_id="a" * 32,
        artifact_id="11111111-1111-4111-8111-111111111111",
        stdout_sha256="b" * 64,
        stderr_sha256="c" * 64,
        stdout_bytes=12,
        stderr_bytes=0,
    )
    executor = AsyncMock()
    executor.execute.return_value = exec_result
    observation = MagicMock()
    observation_context = MagicMock()
    observation_context.__enter__.return_value = observation
    observation_context.__exit__.return_value = False

    with (
        patch("app.tools.acli.executor._executor", executor),
        patch("app.tools.qkv.engine.observe_tool", return_value=observation_context) as observe,
    ):
        result = await qkv_exec(signal, conversation_id="conversation-1", exec_id="exec-qkv-observe")

    assert result.success is True
    assert observe.call_args.kwargs["tool_name"] == "qkv_alert"
    output = observation.update.call_args.kwargs["output"]
    assert output["exec_id"] == "exec-qkv-observe"
    assert output["artifact_id"] == "11111111-1111-4111-8111-111111111111"
    assert output["stdout_sha256"] == "b" * 64
    assert output["stderr_bytes"] == 0
    assert "error_type" in output
    assert output["error_type"] is None
    assert "stdout" not in output


@pytest.mark.asyncio
async def test_qkv_to_variable_pool_auto_derives_date():
    """QKV 生产者写入 END 时，自动向变量池派生 DATE (YYYY-MM-DD)。"""
    from types import SimpleNamespace

    from app.adapters.agents.htp.kbd_differential import KBDDiagnostic

    agent = KBDDiagnostic.__new__(KBDDiagnostic)
    agent._variable_pool = {}
    agent._variable_sources = {}
    agent._variable_pool_priority = {}
    agent._tool_def_default = lambda tool, field: None
    agent._resolve_host_ip = AsyncMock(side_effect=lambda x, **kw: x)
    agent._ai_registry = MagicMock()
    agent._assistant_type = "htp"
    agent._conversation_id = ""
    agent._case_id = ""
    agent._db_session_factory = None

    signal = {
        "acquire": {"tool": "qkv_task", "args": {"keyword": "delete"}},
        "orchestrate": {"produces": [{"name": "END", "path": "end"}]},
    }
    res = SimpleNamespace(
        values=[{"end": "2026-06-23 22:54:03", "date": "2026-06-23"}],
    )
    await agent._fill_pool_from_qkv(signal, res)

    assert (agent._variable_pool.get("end") or agent._variable_pool.get("END")) == "2026-06-23 22:54:03"
    assert (agent._variable_pool.get("date") or agent._variable_pool.get("DATE")) == "2026-06-23"
