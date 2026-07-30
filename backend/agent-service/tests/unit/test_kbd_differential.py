"""
KBDDiagnostic 单元测试

测试覆盖：
  1. _resolve_args：占位符替换逻辑
  2. matcher：确定性规则求值
  3. diagnose：按 KBD signal_id 执行和证据门禁
  4. 27123 golden case：QKV + 两个独立 QFK 信号
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.cdd import SignalOutcome
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic, StepResult
from app.adapters.agents.htp.kbd_model import KBD, KBDStep

# ─── 测试数据工厂 ─────────────────────────────────────────────────────────────


def _expected_to_matcher(expected_pattern):
    """把 __REGEX__:/__CONTAINS__:/__MATCHER__: 序列化串还原为 v2 Matcher dict。

    v2 Matcher 契约类型为 keyword/regex/state/threshold/json_path/exists：
    - __CONTAINS__ 等价于 keyword + mode="or"（任一关键字出现即命中）
    - __REGEX__ 等价于 regex
    """
    if not expected_pattern:
        return None
    if expected_pattern.startswith("__REGEX__:"):
        return {
            "type": "regex",
            "pattern": expected_pattern[len("__REGEX__:") :],
            "mode": "or",
            "expected": True,
        }
    if expected_pattern.startswith("__CONTAINS__:"):
        return {
            "type": "keyword",
            "pattern": expected_pattern[len("__CONTAINS__:") :],
            "mode": "or",
            "expected": True,
        }
    if expected_pattern.startswith("__MATCHER__:"):
        import json as _json

        return _json.loads(expected_pattern[len("__MATCHER__:") :])
    return expected_pattern


def make_kbd(
    kbd_id: str,
    steps: list[tuple[str, str]],  # [(tool_name, expected_pattern), ...]
    root_cause: str = "测试根因",
    similarity: float = 0.8,
) -> KBD:
    """快速构建 KBD 测试对象（signals 为 v2 嵌套 dict 列表）。"""
    return KBD(
        id=kbd_id,
        name=f"KBD {kbd_id}",
        category_id="虚拟机-003",
        problem_description=f"{kbd_id} 的问题描述",
        signals=[
            {
                "acquire": {"tool": tool_name, "args": {}},
                "match": _expected_to_matcher(expected_pattern),  # v2 Matcher dict
                "provenance": {"category": "backend"},
            }
            for tool_name, expected_pattern in steps
        ],
        root_cause=root_cause,
        solution=f"{kbd_id} 的解决方案",
        similarity=similarity,
    )


def make_registry_mock(invoke_response: str = '{"matches": {}}') -> MagicMock:
    """构建 AIAssistantRegistry mock，invoke() 返回指定 JSON。"""
    mock_result = MagicMock()
    mock_result.content = invoke_response

    mock_client = MagicMock()
    mock_client.invoke = AsyncMock(return_value=mock_result)

    mock_registry = MagicMock()
    mock_registry.get_client.return_value = mock_client
    return mock_registry


def make_tool_executor(results: dict[str, str]) -> MagicMock:
    """构建工具执行器 mock，按 tool_name 返回对应结果。"""
    mock = MagicMock()

    async def execute(tool_name: str, args: dict) -> str:
        return results.get(tool_name, "")

    mock.execute = execute
    return mock


# ─── 单元测试：_resolve_args ──────────────────────────────────────────────────


class TestResolveArgs:
    """_resolve_args：占位符替换逻辑测试"""

    def test_replaces_single_placeholder(self):
        template = {"vm_name": "{{vm_name}}"}
        result = KBDDiagnostic._resolve_args(template, {"vm_name": "vm-001"})
        assert result == {"vm_name": "vm-001"}

    def test_replaces_multiple_placeholders(self):
        template = {"vm": "{{vm_name}}", "host": "{{host_id}}"}
        result = KBDDiagnostic._resolve_args(template, {"vm_name": "vm-001", "host_id": "host-01"})
        assert result == {"vm": "vm-001", "host": "host-01"}

    def test_leaves_non_string_values_unchanged(self):
        template = {"limit": 10, "enabled": True}
        result = KBDDiagnostic._resolve_args(template, {})
        assert result == {"limit": 10, "enabled": True}

    def test_unknown_placeholder_remains(self):
        template = {"key": "{{unknown}}"}
        result = KBDDiagnostic._resolve_args(template, {})
        assert result == {"key": "{{unknown}}"}


# ─── 单元测试：变量池大小写归一化（B/2.6）────────────────────────────────────


class TestVariablePoolCaseNormalization:
    """变量池大小写归一化：消费侧不敏感 + 生产侧小写规范（纵深防御）。"""

    def test_resolve_args_case_insensitive(self):
        # 生产侧写入小写 Key（_set_pool_var 规范）；消费侧任意大小写占位符均可命中
        pool = {"host": "10.0.0.1", "vm_id": "v-123"}
        template = {
            "a": "http://{{HOST}}",
            "b": "http://{{host}}",
            "c": "http://{{Host}}",
            "d": "vm is {{VM_ID}}",
        }
        result = KBDDiagnostic._resolve_args(template, {}, pool)
        assert result["a"] == "http://10.0.0.1"
        assert result["b"] == "http://10.0.0.1"
        assert result["c"] == "http://10.0.0.1"
        assert result["d"] == "vm is v-123"

    def test_set_pool_var_lowercases_key(self):
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        # 生产侧写入入口强制小写，无论 produces 名大小写 / 首尾空格如何，池内 Key 永远统一
        diag._set_pool_var("HOST", "node-001")
        diag._set_pool_var("HoSt", "x")  # 同键不同大小写 -> 归一为 host（后者覆盖）
        diag._set_pool_var(" vm_id ", "v-9")  # 首尾空格 + 大写 -> vm_id
        assert diag._variable_pool == {"host": "x", "vm_id": "v-9"}

    def test_producer_consumer_roundtrip(self):
        # 模拟 _fill_pool_from_qkv 经 _set_pool_var 写入任意大小写 produces 名
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        diag._set_pool_var("HOST", "node-001")  # produces name 可能大写
        # 消费侧模板用大写占位符 {{HOST}}，应命中池内小写 host
        result = KBDDiagnostic._resolve_args({"scope": "{{HOST}}"}, {}, diag._variable_pool)
        assert result == {"scope": "node-001"}
        # 即便模板误用小写 {{host}}，同样命中（纵深防御）
        result2 = KBDDiagnostic._resolve_args({"scope": "{{host}}"}, {}, diag._variable_pool)
        assert result2 == {"scope": "node-001"}


class TestHostIPResolution:
    """QKV 产出主机名后，按 HCI 节点列表归一化为节点 IP。"""

    @pytest.mark.asyncio
    async def test_resolves_host_name_from_platform_node_list(self, monkeypatch):
        import app.tools.acli.executor as executor_module

        bridge_executor = MagicMock()
        bridge_executor.execute = AsyncMock(
            return_value=SimpleNamespace(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "data": [
                            {
                                "id": "host-047bcb4bc820",
                                "name": "SVR_aCloud_668",
                                "ip": "172.28.24.2",
                            }
                        ]
                    }
                ),
                stderr="",
            )
        )
        monkeypatch.setattr(executor_module, "_executor", bridge_executor)

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        result = await diag._resolve_host_ip(
            "SVR_aCloud_668",
            node_ip="172.28.24.1",
            session_id="case-1",
        )

        assert result == "172.28.24.2"
        # 同一诊断会话复用缓存，不重复执行 platform node list。
        assert (
            await diag._resolve_host_ip("SVR_aCloud_668", node_ip="172.28.24.1", session_id="case-1")
            == "172.28.24.2"
        )
        bridge_executor.execute.assert_awaited_once()
        assert bridge_executor.execute.await_args.kwargs["node_ip"] == "172.28.24.1"

    @pytest.mark.asyncio
    async def test_qkv_host_is_normalized_before_variable_pool_write(self, monkeypatch):
        import app.tools.acli.executor as executor_module

        bridge_executor = MagicMock()
        bridge_executor.execute = AsyncMock(
            return_value=SimpleNamespace(
                exit_code=0,
                stdout=json.dumps({"data": [{"id": "host-1", "name": "node-a", "ip": "10.0.0.2"}]}),
                stderr="",
            )
        )
        monkeypatch.setattr(executor_module, "_executor", bridge_executor)

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        await diag._fill_pool_from_qkv(
            {
                "acquire": {"tool": "qkv_task", "args": {}},
                "orchestrate": {"produces": [{"name": "HOST", "path": "host"}]},
            },
            SimpleNamespace(values=[{"host": "node-a"}]),
            node_ip="10.0.0.1",
            session_id="case-1",
        )

        assert diag._variable_pool["host"] == "10.0.0.2"

    @pytest.mark.asyncio
    async def test_qfk_routes_to_resolved_host_ip(self, monkeypatch):
        import app.tools.qfk.engine as qfk_engine

        qfk_result = SimpleNamespace(error=None, raw_output="node output", matched=True)
        qfk_exec = AsyncMock(return_value=qfk_result)
        monkeypatch.setattr(qfk_engine, "qfk_exec", qfk_exec)

        diag = KBDDiagnostic(
            ai_registry=MagicMock(),
            tool_executor=MagicMock(),
            conversation_id="conversation-1",
            case_id="case-1",
        )
        await diag._execute_acquirer(
            KBDStep(
                tool_name="qfk_system",
                tool_args_template={
                    "host": "172.28.24.2",
                    "command": "lsof",
                    "resource_keyword": "4359974862144",
                },
                matcher={"type": "keyword", "pattern": "node output", "mode": "or", "expected": True},
            ),
            {"node_ip": "172.28.24.1"},
            "case-1",
            "",
        )

        assert qfk_exec.await_args.kwargs["node_ip"] == "172.28.24.2"

    @pytest.mark.asyncio
    async def test_qfk_resolves_named_host_and_never_silently_falls_back_to_current_node(self, monkeypatch):
        import app.tools.qfk.engine as qfk_engine

        qfk_exec = AsyncMock(return_value=SimpleNamespace(error=None, raw_output="node output", matched=True))
        monkeypatch.setattr(qfk_engine, "qfk_exec", qfk_exec)
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        diag._resolve_host_ip = AsyncMock(return_value="172.28.24.3")

        await diag._execute_acquirer(
            KBDStep(
                tool_name="qfk_log",
                tool_args_template={"host": "SVR_aCloud_668", "file": "kernel.log"},
                matcher={"type": "keyword", "pattern": "I/O error", "expected": True},
            ),
            {"node_ip": "172.28.24.1"},
            "case-1",
            "",
        )

        diag._resolve_host_ip.assert_awaited_once()
        assert qfk_exec.await_args.kwargs["node_ip"] == "172.28.24.3"

        diag._resolve_host_ip = AsyncMock(return_value="unknown-host")
        _raw, error, _matched = await diag._execute_acquirer(
            KBDStep(
                tool_name="qfk_log",
                tool_args_template={"host": "unknown-host", "file": "kernel.log"},
                matcher={"type": "keyword", "pattern": "I/O error", "expected": True},
            ),
            {"node_ip": "172.28.24.1"},
            "case-1",
            "",
        )
        assert "QFK_TARGET_HOST_UNRESOLVED" in (error or "")
        assert qfk_exec.await_count == 1

    @pytest.mark.asyncio
    async def test_kbd_qkv_executes_explicit_acquisition_even_with_prefetched_tasks(self, monkeypatch):
        import app.tools.qkv.engine as qkv_engine

        qkv_exec = AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                values=[{"vm": "4359974862144", "host": "SVR_aCloud_668"}],
                to_observation=lambda: "QKV command acquisition",
            )
        )
        monkeypatch.setattr(qkv_engine, "qkv_exec", qkv_exec)
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        diag._resolve_host_ip = AsyncMock(return_value="172.28.24.2")
        signal = {
            "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机失败", "is_failed": True}},
            "orchestrate": {
                "produces": [
                    {"name": "VM", "path": "vm"},
                    {"name": "HOST", "path": "host"},
                ]
            },
        }
        step = KBDStep(tool_name="qkv_task", tool_args_template=signal["acquire"]["args"])

        observation, error, matched = await diag._execute_acquirer(
            step,
            {
                "node_ip": "172.28.24.1",
                "task_logs": [
                    {
                        "vm": "4359974862144",
                        "host": "SVR_aCloud_668",
                        "status": 3,
                        "description": "启动虚拟机失败",
                    }
                ],
            },
            "case-1",
            "",
            signal=signal,
        )

        assert error is None
        assert matched is True
        assert observation == "QKV command acquisition"
        assert diag._variable_pool["host"] == "172.28.24.2"
        qkv_exec.assert_awaited_once()
        assert qkv_exec.await_args.kwargs["signal"].keyword == "启动虚拟机"
        assert qkv_exec.await_args.kwargs["signal"].is_failed is True


class TestQFKProduces:
    """QFK 命令输出写入变量池，并作为无 matcher 信号的通过依据。"""

    def test_signal_to_qfk_resolves_runtime_variables_before_handler_execution(self):
        """QFK Handler 只能接收现场值，不能收到未解析的 ``{{VAR}}`` 模板。"""
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        step = KBDStep(
            tool_name="qfk_system",
            tool_args_template={
                "host": "{{HOST}}",
                "command": "ls -1 /proc/{{PID}}/fd",
                "resource_keyword": "{{VM}}",
            },
            matcher={"type": "exists", "expected": True},
        )

        signal = diag._signal_to_qfk(
            step,
            {"HOST": "10.0.0.1", "PID": 4046749, "VM": "vm-01"},
        )

        assert signal is not None
        assert signal.host == "10.0.0.1"
        assert signal.command == "ls -1 /proc/4046749/fd"
        assert signal.resource_keyword == "vm-01"

    def test_extracts_full_output_and_json_paths(self):
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        ok, error = diag._fill_pool_from_qfk(
            [
                {"name": "RAW", "path": ""},
                {"name": "PID", "path": "data.0.pid|pid"},
            ],
            '{"data": [{"pid": 27123}]}',
        )

        assert ok is True
        assert error is None
        assert diag._variable_pool["raw"] == '{"data": [{"pid": 27123}]}'
        assert diag._variable_pool["pid"] == 27123

    def test_rejects_json_path_for_non_json_output(self):
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        ok, error = diag._fill_pool_from_qfk([{"name": "PID", "path": "data.0.pid"}], "pid=27123")

        assert ok is False
        assert "不是合法 JSON" in str(error)

    @pytest.mark.asyncio
    async def test_output_signal_passes_and_populates_pool(self, monkeypatch):
        import app.tools.qfk.engine as qfk_engine

        qfk_exec = AsyncMock(return_value=SimpleNamespace(error=None, raw_output="27123", matched=False))
        monkeypatch.setattr(qfk_engine, "qfk_exec", qfk_exec)
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock(), conversation_id="conv-1", case_id="case-1")
        signal = {
            "acquire": {"tool": "qfk_system", "args": {"command": "pidof test", "container": "host"}},
            "match": None,
            "orchestrate": {"produces": [{"name": "PID", "path": ""}]},
        }
        step = KBDStep(tool_name="qfk_system", tool_args_template=signal["acquire"]["args"], matcher=None)

        raw_output, error, pre_matched = await diag._execute_acquirer(step, {}, "case-1", "", signal=signal)

        assert raw_output == "27123"
        assert error is None
        assert pre_matched is True
        assert diag._variable_pool["pid"] == "27123"
        assert diag._evaluate_signal_outcome(signal, raw_output, error, pre_matched).value == "SATISFIED"

    @pytest.mark.asyncio
    async def test_text_extract_is_forwarded_as_resolved_edge_row_filter(self, monkeypatch):
        import app.tools.qfk.engine as qfk_engine

        qfk_exec = AsyncMock(
            return_value=SimpleNamespace(
                error=None,
                raw_output="qemu 9527 4359974862144",
                matched=False,
                complete_outputs={"stdout": "qemu 9527 4359974862144"},
            )
        )
        monkeypatch.setattr(qfk_engine, "qfk_exec", qfk_exec)
        diag = KBDDiagnostic(
            ai_registry=MagicMock(), tool_executor=MagicMock(), conversation_id="conv-1", case_id="case-1"
        )
        signal = {
            "acquire": {"tool": "qfk_system", "args": {"command": "lsof", "container": "host"}},
            "match": None,
            "orchestrate": {
                "produces": [
                    {
                        "name": "PID",
                        "extract": {
                            "type": "text",
                            "source": "stdout",
                            "include": ["{{VM}}"],
                            "exclude": ["grep"],
                            "include_mode": "all",
                            "case_sensitive": True,
                            "column": 2,
                        },
                    }
                ]
            },
        }
        diag._set_pool_var("VM", "4359974862144")

        raw_output, error, matched = await diag._execute_acquirer(
            KBDStep(tool_name="qfk_system", tool_args_template=signal["acquire"]["args"], matcher=None),
            {},
            "case-1",
            "",
            signal=signal,
        )

        assert error is None
        assert matched is True
        assert diag._variable_pool["pid"] == "9527"
        assert qfk_exec.await_args.kwargs["output_filters"] == [
            {
                "source": "stdout",
                "include": ["4359974862144"],
                "exclude": ["grep"],
                "include_mode": "all",
                "case_sensitive": True,
            }
        ]


# ─── 单元测试：_evaluate_matcher（§6 5 类定型 valuator）────────────────────────


class TestMatcherEvaluation:
    """_evaluate_matcher：5 类定型 valuator 确定性求值，不调用 LLM。"""

    def setup_method(self):
        self.diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())

    def test_keyword_or(self):
        m = {"type": "keyword", "pattern": "CPU 资源不足", "mode": "or", "expected": True}
        assert self.diag._evaluate_matcher(m, "检测到 CPU 资源不足") is True
        assert self.diag._evaluate_matcher(m, "一切正常") is False

    def test_keyword_not(self):
        # not 模式：均不出现才符合期望
        m = {"type": "keyword", "pattern": "OOM", "mode": "not"}
        assert self.diag._evaluate_matcher(m, "无相关报错") is True
        assert self.diag._evaluate_matcher(m, "出现 OOM") is False

    def test_keyword_and(self):
        m = {"type": "keyword", "pattern": ["a", "b"], "mode": "and", "expected": True}
        assert self.diag._evaluate_matcher(m, "a 和 b 都存在") is True
        assert self.diag._evaluate_matcher(m, "只有 a") is False

    def test_regex(self):
        m = {"type": "regex", "pattern": r"err=\d{3}", "expected": True}
        assert self.diag._evaluate_matcher(m, "err=404 found") is True
        assert self.diag._evaluate_matcher(m, "ok") is False

    def test_state(self):
        m = {"type": "state", "pattern": "running", "expected": True}
        assert self.diag._evaluate_matcher(m, "service status: running") is True
        assert self.diag._evaluate_matcher(m, "status: stopped") is False

    def test_threshold_gt(self):
        m = {"type": "threshold", "operator": ">", "value": 90, "expected": True}
        assert self.diag._evaluate_matcher(m, "cpu_usage=95%") is True
        assert self.diag._evaluate_matcher(m, "cpu_usage=80%") is False

    def test_threshold_le(self):
        m = {"type": "threshold", "operator": "<=", "value": 10, "expected": True}
        assert self.diag._evaluate_matcher(m, "load=8") is True
        assert self.diag._evaluate_matcher(m, "load=20") is False

    def test_json_path(self):
        import json as _json

        m = {"type": "json_path", "path": "status", "expected_value": "healthy"}
        assert self.diag._evaluate_matcher(m, _json.dumps({"status": "healthy"})) is True
        assert self.diag._evaluate_matcher(m, _json.dumps({"status": "bad"})) is False

    def test_exists(self):
        m = {"type": "exists", "expected": True}
        assert self.diag._evaluate_matcher(m, "id=123 found") is True
        assert self.diag._evaluate_matcher(m, "对象不存在") is False

    def test_unknown_type_falls_back_to_llm(self):
        assert self.diag._evaluate_matcher({"type": "bogus"}, "x") is None


class TestToolDefinitionFallback:
    """2.1 让 tool_definition 生效：signals_json 缺省时回退 admin-ui 配置默认值。"""

    def setup_method(self):
        from types import SimpleNamespace

        from app.adapters.agents.htp import tool_registry

        self._mod = tool_registry
        self._added = ["qkv_alert", "qfk_log"]
        # 模拟 admin-ui 在 tool_definition 中配置的默认值
        self._mod.TOOL_REGISTRY["qkv_alert"] = SimpleNamespace(
            parameters={"properties": {"produces": {"default": [{"name": "HOST", "path": "host"}]}}}
        )
        self._mod.TOOL_REGISTRY["qfk_log"] = SimpleNamespace(
            parameters={
                "properties": {
                    "matcher": {"default": {"type": "keyword", "pattern": ["X"], "mode": "or", "expected": True}}
                }
            }
        )

    def teardown_method(self):
        for k in self._added:
            self._mod.TOOL_REGISTRY.pop(k, None)

    def test_qkv_produces_fallback(self):
        from app.tools.qkv.signal import FrontendSignal

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        sig = {"acquire": {"tool": "qkv_alert", "args": {"keyword": "disk"}}}
        fsig = diag._signal_to_qkv(sig, {})
        assert isinstance(fsig, FrontendSignal)
        # signals_json 未配置 produces -> 应采用 tool_definition 默认值
        assert fsig.produces == [{"name": "HOST", "path": "host"}]

    def test_qkv_produces_explicit_overrides_fallback(self):
        from app.tools.qkv.signal import FrontendSignal

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        sig = {
            "acquire": {"tool": "qkv_alert", "args": {"keyword": "disk"}},
            "orchestrate": {"produces": [{"name": "VM", "path": "vm"}]},
        }
        fsig = diag._signal_to_qkv(sig, {})
        assert isinstance(fsig, FrontendSignal)
        # signals_json 显式配置应优先于 tool_definition 默认值
        assert fsig.produces == [{"name": "VM", "path": "vm"}]

    def test_qfk_matcher_fallback(self):
        from types import SimpleNamespace

        from app.tools.qfk.signal import BackendSignal

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        # matcher 可以回退到工具定义，但日志文件是运行时定位数据源的必要参数，
        # 不能用一个本身不可执行的空参数信号验证默认值行为。
        step = SimpleNamespace(
            tool_name="qfk_log",
            tool_args_template={"file": "kernel.log"},
            matcher=None,
        )
        bsig = diag._signal_to_qfk(step)
        assert isinstance(bsig, BackendSignal)
        # signals_json 未配置 matcher -> 应采用 tool_definition 默认值
        assert bsig.match_mode == "or"
        assert bsig.keyword == ["X"]


# ─── 集成测试：完整诊断主循环 ────────────────────────────────────────────────


class TestDiagnoseLoop:
    """diagnose()：完整 KBD 差异诊断主循环集成测试"""

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_error_event(self):
        """候选 KBD 为空时应返回错误提示并设置 result"""
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=MagicMock(),
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=[],
                env_context={},
                session_id="test-001",
            )
        ]

        assert diag.get_result() is not None
        assert diag.get_result().matched_kbds == []
        text_events = [e for e in events if hasattr(e, "content")]
        assert any("未找到" in e.content for e in text_events)

    @pytest.mark.asyncio
    async def test_unknown_contract_scope_stops_execution_and_remains_inconclusive(self):
        candidate = make_kbd("scoped", [("tool_a", "__CONTAINS__:yes")])
        candidate.verification_contract = {
            "scope": {"products": ["HCI"]},
            "evidence_policy": {"must": ["signal_001"]},
        }
        diag = KBDDiagnostic(ai_registry=make_registry_mock(), tool_executor=MagicMock())
        diag._execute_acquirer = AsyncMock(return_value=("yes", None, None))

        events = [
            event
            async for event in diag.diagnose(
                candidates=[candidate],
                env_context={},
                session_id="scope-unknown",
            )
        ]

        result = diag.get_result()
        assert result is not None
        assert result.conclusion_level == "INCONCLUSIVE"
        assert result.candidate_states == {"scoped": "INCONCLUSIVE"}
        assert result.steps_executed == []
        diag._execute_acquirer.assert_not_awaited()
        start = next(event for event in events if getattr(event, "stage", "") == "kbd_diag_start")
        assert start.metadata["scope_states"] == {"scoped": "UNKNOWN"}

    @pytest.mark.asyncio
    async def test_shared_acquisition_still_evaluates_each_kbd_signal(self):
        """相同采集只执行一次，但每篇 KBD 的 signal 必须独立求值。"""
        tool_executor = make_tool_executor({"tool_a": "output"})
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock('{"matches": {"k1": true}}'),
            tool_executor=tool_executor,
        )

        candidates = [
            make_kbd("k1", [("tool_a", "__CONTAINS__:output")]),
            make_kbd("k2", [("tool_a", "__CONTAINS__:output")]),
        ]

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-002",
            )
        ]

        result = diag.get_result()
        assert result is not None
        assert len(result.matched_kbds) == 2
        # 同一次 acquisition 只实际执行一次，但每篇 KBD 的 signal 必须独立求值。
        assert len(result.steps_executed) == 2
        assert {step.tool_name for step in result.steps_executed} == {"tool_a"}
        assert all(step.outcome.value == "SATISFIED" for step in result.steps_executed)
        assert len({step.exec_id for step in result.steps_executed}) == 1
        assert len({step.evaluation_id for step in result.steps_executed}) == 2

    @pytest.mark.asyncio
    async def test_supported_plus_tool_error_is_partial_and_never_s4(self):
        first = make_kbd("k1", [("tool_a", "__CONTAINS__:yes")])
        second = make_kbd("k2", [("tool_b", "__CONTAINS__:yes")])
        diag = KBDDiagnostic(ai_registry=make_registry_mock(), tool_executor=MagicMock())

        async def execute(step, *_args, **_kwargs):
            if step.tool_name == "tool_a":
                return "yes", None, None
            return None, "bridge timeout", None

        diag._execute_acquirer = AsyncMock(side_effect=execute)
        events = [
            event
            async for event in diag.diagnose(
                candidates=[first, second], env_context={}, session_id="partial"
            )
        ]

        result = diag.get_result()
        assert result is not None
        assert result.conclusion_level == "PARTIAL"
        assert not result.is_definitive
        assert [kbd.id for kbd in result.matched_kbds] == ["k1"]
        assert "暂不能定论" in result.diagnosis_report
        assert "测试根因" not in result.diagnosis_report
        complete = next(event for event in events if getattr(event, "stage", "") == "kbd_diag_complete")
        assert complete.metadata["conclusion_level"] == "PARTIAL"

    @pytest.mark.asyncio
    async def test_single_candidate_confirms_its_signals(self):
        """单候选 KBD 也必须执行其 backend 关键信号确认，不能跳过就给结论（回归测试）"""
        # 模拟真实场景：工具采集到“第三方进程 ClwDRDBClient 持有镜像”的关键信号
        tool_executor = make_tool_executor({"acli_vm_config": "ERROR: 虚拟机镜像忙，ClwDRDBClient 持有镜像文件"})
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock('{"matches": {"k1": true}}'),
            tool_executor=tool_executor,
        )

        candidates = [make_kbd("k1", [("acli_vm_config", "__CONTAINS__:ClwDRDBClient")])]

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-002b",
            )
        ]

        result = diag.get_result()
        assert result is not None
        # 单候选不等于已确认，仍须执行关键信号。
        assert len(result.steps_executed) == 1
        step = result.steps_executed[0]
        assert step.tool_name == "acli_vm_config"
        assert step.raw_output is not None
        assert "ClwDRDBClient" in step.raw_output
        # 确认语义：不剔除唯一候选
        assert len(result.matched_kbds) == 1

    @pytest.mark.asyncio
    async def test_eliminates_non_matching_kbds(self):
        """执行步骤后，不匹配的 KBD 被过滤掉"""
        # 工具执行返回包含 "memory_error" 的输出
        tool_executor = make_tool_executor({"acli_vm_config": "ERROR: memory_error critical"})

        # k1 期望 memory_error（匹配），k2 期望 disk_full（不匹配）
        candidates = [
            make_kbd("k1", [("acli_vm_config", "__CONTAINS__:memory_error")]),
            make_kbd("k2", [("acli_vm_config", "__CONTAINS__:disk_full")]),
            make_kbd("k3", [("acli_vm_config", "__CONTAINS__:memory_error")]),
        ]

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=tool_executor,
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-003",
            )
        ]

        result = diag.get_result()
        assert result is not None
        # k2 被过滤，k1 和 k3 保留
        matched_ids = {kbd.id for kbd in result.matched_kbds}
        assert "k1" in matched_ids
        assert "k3" in matched_ids
        assert "k2" not in matched_ids

    @pytest.mark.asyncio
    async def test_definitive_match_sets_is_definitive_true(self):
        """精确锁定到 1 个 KBD 时 is_definitive=True"""
        tool_executor = make_tool_executor(
            {
                "tool_a": "memory_error",
                "tool_b": "node01 only",
            }
        )

        candidates = [
            make_kbd("k1", [("tool_a", "__CONTAINS__:memory_error"), ("tool_b", "__CONTAINS__:node01")]),
            make_kbd("k2", [("tool_a", "__CONTAINS__:memory_error"), ("tool_b", "__CONTAINS__:node02")]),
        ]

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=tool_executor,
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-004",
            )
        ]

        result = diag.get_result()
        assert result is not None
        assert result.is_definitive is True
        assert result.matched_kbds[0].id == "k1"

    @pytest.mark.asyncio
    async def test_tool_execution_failure_does_not_crash(self):
        """工具执行失败时不崩溃，继续尝试其他步骤"""
        mock_executor = MagicMock()
        call_count = {"n": 0}

        async def execute(tool_name: str, args: dict) -> str:
            call_count["n"] += 1
            if tool_name == "tool_a":
                raise RuntimeError("工具连接超时")
            return "disk_error detected"

        mock_executor.execute = execute

        candidates = [
            make_kbd("k1", [("tool_a", "__CONTAINS__:disk"), ("tool_b", "__CONTAINS__:disk_error")]),
            make_kbd("k2", [("tool_a", "__CONTAINS__:memory"), ("tool_b", "__CONTAINS__:disk_error")]),
        ]

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=mock_executor,
        )

        # 不应抛出异常
        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-005",
            )
        ]

        result = diag.get_result()
        assert result is not None  # 正常完成

    @pytest.mark.asyncio
    async def test_env_context_fills_args_template(self):
        """env_context 中的值应替换工具参数模板中的占位符"""
        captured_args = {}

        async def execute(tool_name: str, args: dict) -> str:
            captured_args.update(args)
            return "ok"

        mock_executor = MagicMock()
        mock_executor.execute = execute

        # 构建含占位符的 KBD
        kbd = KBD(
            id="k1",
            name="KBD k1",
            category_id="虚拟机-003",
            problem_description="测试问题描述",
            signals=[
                {
                    "acquire": {"tool": "acli_vm_config", "args": {"vm_name": "{{vm_name}}"}},
                    "match": {"type": "contains", "keyword": "ok"},
                    "provenance": {"category": "backend"},
                }
            ],
            root_cause="测试",
            solution="测试",
        )

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=mock_executor,
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=[kbd],
                env_context={"vm_name": "test-vm-001"},
                session_id="test-006",
            )
        ]

        # 验证 vm_name 占位符已被替换
        assert captured_args.get("vm_name") == "test-vm-001"


# ─── 多候选证据求值验证 ───────────────────────────────────────────────────────


class TestKBDDiagEffectiveness:
    """多篇 KBD 的全部信号均进入证据求值。"""

    @pytest.mark.asyncio
    async def test_ten_candidates_use_fail_short_circuit_without_changing_result(self):
        """required FAIL 后取消候选独占信号，但仍找到具有完整 PASS 证据的 KBD。"""
        # 构建 10 个 KBD，每个有独特的期望模式
        # 真实故障：k5（Redis 服务异常导致虚拟机开机失败）
        candidates = [
            make_kbd("k1", [("get_active_alerts", "__CONTAINS__:network"), ("acli_vm_config", "__CONTAINS__:vlan")]),
            make_kbd(
                "k2", [("get_active_alerts", "__CONTAINS__:storage"), ("acli_vm_disk_check", "__CONTAINS__:error")]
            ),
            make_kbd(
                "k3", [("acli_vm_config", "__CONTAINS__:cpu_overcommit"), ("acli_system_top", "__CONTAINS__:cpu_load")]
            ),
            make_kbd(
                "k4", [("get_active_alerts", "__CONTAINS__:memory"), ("acli_vm_config", "__CONTAINS__:memory_mb")]
            ),
            make_kbd(
                "k5", [("get_failed_tasks", "__CONTAINS__:redis"), ("acli_platform_node_list", "__CONTAINS__:degraded")]
            ),
            make_kbd(
                "k6", [("get_active_alerts", "__CONTAINS__:network"), ("acli_vm_config", "__CONTAINS__:mac_address")]
            ),
            make_kbd("k7", [("get_failed_tasks", "__CONTAINS__:timeout"), ("acli_system_top", "__CONTAINS__:io_wait")]),
            make_kbd(
                "k8", [("acli_vm_config", "__CONTAINS__:disk"), ("acli_vm_disk_check", "__CONTAINS__:bad_sector")]
            ),
            make_kbd(
                "k9",
                [
                    ("get_failed_tasks", "__CONTAINS__:permission"),
                    ("acli_platform_node_list", "__CONTAINS__:node_count"),
                ],
            ),
            make_kbd(
                "k10", [("get_active_alerts", "__CONTAINS__:cluster"), ("acli_vm_list", "__CONTAINS__:powered_off")]
            ),
        ]

        # 模拟真实系统输出（k5 的特征：redis 失败，节点降级）
        tool_results = {
            "get_active_alerts": "No critical alerts",
            "get_failed_tasks": "FAILED: redis service start failed on vm-001 at 2024-01-15",
            "acli_vm_config": "vm-001 config: memory_mb=4096 cpu=2",
            "acli_vm_disk_check": "All disks healthy",
            "acli_system_top": "cpu_load=0.3 io_wait=0.1",
            "acli_platform_node_list": "node-01: healthy, node-02: degraded service",
            "acli_vm_list": "vm-001: powered_off vm-002: running",
        }

        tool_executor = make_tool_executor(tool_results)

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=tool_executor,
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={"vm_name": "vm-001"},
                session_id="effectiveness-test",
            )
        ]

        result = diag.get_result()
        assert result is not None

        # 已被 required FAIL 排除的 KBD 不再执行其独占后续信号。
        assert len(result.steps_executed) < sum(len(kbd.signals) for kbd in candidates)

        # 验收标准 2：真实 KBD k5 在匹配列表中
        matched_ids = {kbd.id for kbd in result.matched_kbds}
        assert "k5" in matched_ids, f"真实 KBD k5 未出现在匹配列表中，当前匹配：{matched_ids}"

        # 验收标准 3：最终候选数量缩减（从 10 减少到 ≤ 6）
        assert len(result.matched_kbds) <= 6, f"候选 KBD 数量 {len(result.matched_kbds)} 未有效缩减"


class TestEvidenceGatedCDD:
    """分类全量 KBD 进入 CDD 后的证据闭环不变量。"""

    @staticmethod
    def _kbd_27123() -> KBD:
        return KBD(
            id="1",
            support_id="27123",
            name="虚拟机开机失败，报错虚拟机镜像忙",
            category_id="虚拟机-003",
            root_cause="第三方程序占用虚拟机镜像文件",
            solution="按案例原文解除占用后重试",
            resource_revision={"revision": 4},
            signals=[
                {
                    "id": "sig_001",
                    "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机失败", "is_failed": True}},
                    "match": None,
                    "provenance": {"category": "frontend"},
                    "orchestrate": {
                        "phase": "diagnostic",
                        "requires": [],
                        "produces": [{"name": "VM", "path": "vm"}, {"name": "HOST", "path": "host"}],
                    },
                },
                {
                    "id": "sig_002",
                    "acquire": {
                        "tool": "qfk_system",
                        "args": {"host": "{{HOST}}", "command": "lsof", "resource_keyword": "{{VM}}"},
                    },
                    "match": {"type": "keyword", "pattern": "vm-disk", "mode": "or", "expected": True},
                    "provenance": {"category": "backend"},
                    "orchestrate": {"phase": "diagnostic", "requires": ["VM", "HOST"], "produces": []},
                },
                {
                    "id": "sig_003",
                    "acquire": {"tool": "qfk_system", "args": {"host": "{{HOST}}", "command": "ps"}},
                    "match": {
                        "type": "keyword",
                        "pattern": "ClwDRDBClient",
                        "mode": "or",
                        "expected": True,
                    },
                    "provenance": {"category": "backend"},
                    "orchestrate": {"phase": "diagnostic", "requires": ["HOST"], "produces": []},
                },
            ],
        )

    @pytest.mark.asyncio
    async def test_27123_executes_both_qfk_system_signals_and_reports_reference(self):
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())

        async def execute(step, env_context, session_id, user_id, *, signal=None, exec_id=None):
            signal_id = signal["id"]
            if signal_id == "sig_001":
                diag._set_pool_var("VM", "Server-IMG")
                diag._set_pool_var("HOST", "SVR_aCloud_668")
                return "失败任务: 虚拟机镜像忙", None, True
            if signal_id == "sig_002":
                assert step.tool_args_template["resource_keyword"] == "Server-IMG"
                return "vm-disk Server-IMG pid=9527", None, True
            return "9527 ClwDRDBClient", None, True

        diag._execute_acquirer = AsyncMock(side_effect=execute)
        events = [
            event
            async for event in diag.diagnose(candidates=[self._kbd_27123()], env_context={}, session_id="golden-27123")
        ]

        result = diag.get_result()
        assert result is not None and result.is_definitive
        assert [step.signal_id for step in result.steps_executed] == ["sig_001", "sig_002", "sig_003"]
        assert len({step.exec_id for step in result.steps_executed}) == 3
        assert len({step.evaluation_id for step in result.steps_executed}) == 3
        assert all(step.outcome.value == "SATISFIED" for step in result.steps_executed)
        assert "参考案例 27123" in result.diagnosis_report
        assert "category_id=27123" in result.diagnosis_report
        tool_calls = [event for event in events if getattr(event, "stage", "") == "tool_call"]
        assert len(tool_calls) == 3
        assert [call.metadata["signal_id"] for call in tool_calls] == ["sig_001", "sig_002", "sig_003"]

    @pytest.mark.asyncio
    async def test_27123_production_variable_chain_executes_three_explicit_acquisitions(self, monkeypatch):
        import app.tools.qfk.engine as qfk_engine
        import app.tools.qkv.engine as qkv_engine

        qkv_exec = AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                values=[
                    {
                        "vm": "4359974862144",
                        "host": "SVR_aCloud_670",
                        "end": "2026-07-27 21:19:37",
                    }
                ],
                to_observation=lambda: "task get matched=1",
            )
        )

        async def execute_qfk(*, signal, **kwargs):
            if signal.command == "lsof":
                assert kwargs["node_ip"] == "172.28.24.4"
                assert kwargs["output_filters"][0]["include"] == ["4359974862144"]
                output = "ClwDRDBClient 9527 root /images/4359974862144.vm/vm-disk-1.qcow2"
                return SimpleNamespace(
                    error=None,
                    raw_output=output,
                    matched=False,
                    complete_outputs={"stdout": output},
                )
            assert signal.command == "ps -p 9527 -o cmd="
            assert kwargs["output_filters"][0]["include"] == ["4359974862144"]
            output = "flock -x /sf/data/4359974862144.vm/vm-disk-2.qcow2 sleep 999999"
            return SimpleNamespace(
                error=None,
                raw_output=output,
                matched=False,
                complete_outputs={"stdout": output},
            )

        qfk_exec = AsyncMock(side_effect=execute_qfk)
        monkeypatch.setattr(qkv_engine, "qkv_exec", qkv_exec)
        monkeypatch.setattr(qfk_engine, "qfk_exec", qfk_exec)

        kbd = KBD(
            id="1",
            support_id="27123",
            name="虚拟机开机失败，报错虚拟机镜像忙",
            category_id="虚拟机-003",
            root_cause="第三方程序占用虚拟机镜像文件",
            solution="解除占用后重试",
            signals=[
                {
                    "id": "sig_001",
                    "acquire": {
                        "tool": "qkv_task",
                        "args": {
                            "instruction": "查看虚拟机任务详情，确认开机失败信息",
                            "keyword": "启动虚拟机失败",
                            "is_failed": True,
                            "limit": 1,
                        },
                    },
                    "match": None,
                    "provenance": {"category": "frontend"},
                    "orchestrate": {
                        "requires": [],
                        "produces": [
                            {"name": "VM", "path": "vm"},
                            {"name": "HOST", "path": "host"},
                            {"name": "END", "path": "end"},
                        ],
                    },
                },
                {
                    "id": "sig_002",
                    "acquire": {
                        "tool": "qfk_system",
                        "args": {
                            "instruction": "检查虚拟机镜像文件是否被进程占用",
                            "host": "{{HOST}}",
                            "command": "lsof",
                            "container": "host",
                            "timeout": 120,
                        },
                    },
                    "match": None,
                    "provenance": {"category": "backend"},
                    "orchestrate": {
                        "requires": ["HOST", "VM"],
                        "produces": [
                            {
                                "name": "PID",
                                "extract": {
                                    "type": "text",
                                    "source": "stdout",
                                    "include": ["{{VM}}"],
                                    "column": 2,
                                    "column_mode": "index",
                                    "cardinality": "first",
                                },
                            }
                        ],
                    },
                },
                {
                    "id": "sig_003",
                    "acquire": {
                        "tool": "qfk_system",
                        "args": {
                            "instruction": "查询占用镜像文件的进程详情，确认进程仍然关联目标虚拟机镜像",
                            "host": "{{HOST}}",
                            "command": "ps -p {{PID}} -o cmd=",
                            "container": "host",
                            "timeout": 10,
                        },
                    },
                    "match": None,
                    "provenance": {"category": "backend"},
                    "orchestrate": {
                        "requires": ["HOST", "PID", "VM"],
                        "produces": [
                            {
                                "name": "CMD",
                                "extract": {
                                    "type": "text",
                                    "source": "stdout",
                                    "include": ["{{VM}}"],
                                    "exclude": [],
                                    "column_mode": "whole",
                                    "cardinality": "exactly_one",
                                },
                            }
                        ],
                    },
                },
            ],
        )
        diag = KBDDiagnostic(
            ai_registry=MagicMock(),
            tool_executor=MagicMock(),
            conversation_id="00000000-0000-0000-0000-000000000749",
            case_id="Q2026072747493",
        )
        diag._resolve_host_ip = AsyncMock(return_value="172.28.24.4")

        events = [event async for event in diag.diagnose(candidates=[kbd], env_context={}, session_id="golden-7493")]

        result = diag.get_result()
        assert result is not None and result.is_definitive
        assert diag._variable_pool == {
            "vm": "4359974862144",
            "host": "172.28.24.4",
            "end": "2026-07-27 21:19:37",
            "pid": "9527",
            "cmd": "flock -x /sf/data/4359974862144.vm/vm-disk-2.qcow2 sleep 999999",
        }
        assert qkv_exec.await_count == 1
        assert qfk_exec.await_count == 2
        tool_calls = [event.metadata for event in events if getattr(event, "stage", "") == "tool_call"]
        tool_results = [event.metadata for event in events if getattr(event, "stage", "") == "tool_result"]
        assert all(call["args"] for call in tool_calls)
        assert [event["status"] for event in tool_results] == ["success", "success", "success"]
        assert all("result" in event for event in tool_results)
        assert "查看虚拟机任务详情，确认开机失败信息" in result.diagnosis_report
        assert "虚拟机 ID（VM）" in result.diagnosis_report
        assert "目标主机（HOST）" in result.diagnosis_report
        assert "发生时间（END）" in result.diagnosis_report
        assert "进程 PID（PID）" in result.diagnosis_report
        assert "进程命令（CMD）" in result.diagnosis_report
        assert "flock -x /sf/data/4359974862144.vm/vm-disk-2.qcow2 sleep 999999" in result.diagnosis_report
        assert result.diagnosis_report.index("虚拟机 ID（VM）") < result.diagnosis_report.index("目标主机（HOST）")
        assert result.diagnosis_report.index("目标主机（HOST）") < result.diagnosis_report.index("发生时间（END）")
        assert "exec_id" not in result.diagnosis_report
        assert "evaluation_id" not in result.diagnosis_report
        assert '"instruction"' not in result.diagnosis_report

    @pytest.mark.asyncio
    async def test_unresolved_placeholder_is_blocked_and_never_executed(self):
        kbd = self._kbd_27123()
        kbd.signals = [kbd.signals[1]]
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        diag._execute_acquirer = AsyncMock()

        _events = [event async for event in diag.diagnose(candidates=[kbd], env_context={}, session_id="blocked")]

        result = diag.get_result()
        assert result is not None and not result.is_definitive
        assert result.steps_executed[0].outcome.value == "BLOCKED"
        assert "依赖变量缺失" in (result.steps_executed[0].error or "")
        diag._execute_acquirer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tool_error_never_confirms_single_candidate(self):
        kbd = self._kbd_27123()
        kbd.signals = [kbd.signals[2]]
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        diag._execute_acquirer = AsyncMock(return_value=(None, "terminal bridge timeout", None))

        _events = [
            event
            async for event in diag.diagnose(
                candidates=[kbd], env_context={"HOST": "SVR_aCloud_668"}, session_id="error"
            )
        ]

        result = diag.get_result()
        assert result is not None and not result.is_definitive
        assert result.matched_kbds == []
        assert result.steps_executed[0].outcome.value == "ERROR"
        assert "证据不足" in result.diagnosis_report
        assert "参考案例 27123" in result.diagnosis_report
        assert "（未确认）" in result.diagnosis_report
        assert "category_id=27123" in result.diagnosis_report
        assert result.steps_executed[0].exec_id not in result.diagnosis_report
        assert "执行失败" in result.diagnosis_report
        assert "第三方程序占用" not in result.diagnosis_report

    def test_user_report_hides_raw_output_and_translates_error_and_blocked(self):
        error_step = StepResult(
            tool_name="qfk_system",
            tool_args={"instruction": "查询进程详情", "command": "ps -p 10134 -o cmd="},
            raw_output="lsof: no pwd entry for UID 65535\n" * 100,
            error="QFK 产出变量 CMD 提取失败：QFK_OUTPUT_EMPTY: 命令标准输出为空",
            signal_id="sig_003",
            exec_id="internal-exec-id",
            evaluation_id="internal-evaluation-id",
            outcome=SignalOutcome.ERROR,
        )
        blocked_step = StepResult(
            tool_name="qfk_system",
            tool_args={"instruction": "检查镜像占用"},
            raw_output=None,
            error="依赖变量缺失: host, pid",
            signal_id="sig_004",
            outcome=SignalOutcome.BLOCKED,
        )
        not_applicable_step = StepResult(
            tool_name="qfk_hardware",
            tool_args={"instruction": "检查物理 GPU"},
            raw_output=None,
            error=None,
            signal_id="sig_005",
            outcome=SignalOutcome.NOT_APPLICABLE,
        )

        report = "\n".join(
            [
                KBDDiagnostic._format_step_evidence(error_step, 1),
                KBDDiagnostic._format_step_evidence(blocked_step, 2),
                KBDDiagnostic._format_step_evidence(not_applicable_step, 3),
            ]
        )

        assert "命令没有返回可用于提取变量的内容" in report
        assert "缺少前置步骤产出的变量" in report
        assert "不适用于当前产品、版本、组件或拓扑" in report
        assert "`HOST`" in report and "`PID`" in report
        assert "no pwd entry" not in report
        assert "internal-exec-id" not in report
        assert "internal-evaluation-id" not in report

    def test_qkv_uses_prefetched_failed_task_fact(self):
        signal = self._kbd_27123().signals[0]
        task = {
            "id": 907,
            "vm": "4359974862144",
            "host": "SVR_aCloud_668",
            "status": 3,
            "process": "失败",
            "description": "启动虚拟机（Server-IMG）失败，错误信息：虚拟机镜像忙",
        }

        result = KBDDiagnostic._qkv_values_from_context(signal, {"task_logs": [task]})

        assert result is not None
        values, source = result
        assert source == "task_logs"
        assert values == [{"vm": "4359974862144", "host": "SVR_aCloud_668"}]
