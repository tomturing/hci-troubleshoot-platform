"""
acquisition_strategy 统一解析模块 — 单元测试

覆盖场景：
  1. 规范全名格式（无冒号）
  2. 简写格式（无冒号）
  3. 冒号参数格式（skill:xxx / tool:xxx / env:xxx 等）
  4. 向后兼容旧名（env_context → env_injection，tool → tool_call）
  5. 未识别策略 → 兜底 user_input
  6. 辅助函数 is_guarded_strategy / is_auto_strategy / extract_acquisition_tool
"""

import pytest
from shared.utils.acquisition_strategy import (
    STRATEGY_AGENT_PASS,
    STRATEGY_DERIVED,
    STRATEGY_ENV_INJECTION,
    STRATEGY_JSON_EXTRACT,
    STRATEGY_LLM_INFERENCE,
    STRATEGY_SKILL_CALL,
    STRATEGY_SOP_DEFAULT,
    STRATEGY_TOOL_CALL,
    STRATEGY_USER_CONFIRM,
    STRATEGY_USER_INPUT,
    extract_acquisition_tool,
    is_auto_strategy,
    is_guarded_strategy,
    normalize_strategy,
    parse_strategy,
)


class TestParseStrategyFullNames:
    """测试规范全名格式（无冒号）"""

    def test_user_input(self):
        result = parse_strategy("user_input")
        assert result.strategy == STRATEGY_USER_INPUT
        assert result.parameter is None

    def test_user_confirm(self):
        result = parse_strategy("user_confirm")
        assert result.strategy == STRATEGY_USER_CONFIRM
        assert result.parameter is None

    def test_sop_default(self):
        result = parse_strategy("sop_default")
        assert result.strategy == STRATEGY_SOP_DEFAULT
        assert result.parameter is None

    def test_env_injection(self):
        result = parse_strategy("env_injection")
        assert result.strategy == STRATEGY_ENV_INJECTION
        assert result.parameter is None

    def test_tool_call(self):
        result = parse_strategy("tool_call")
        assert result.strategy == STRATEGY_TOOL_CALL
        assert result.parameter is None

    def test_skill_call(self):
        result = parse_strategy("skill_call")
        assert result.strategy == STRATEGY_SKILL_CALL
        assert result.parameter is None

    def test_llm_inference(self):
        result = parse_strategy("llm_inference")
        assert result.strategy == STRATEGY_LLM_INFERENCE
        assert result.parameter is None

    def test_agent_pass(self):
        result = parse_strategy("agent_pass")
        assert result.strategy == STRATEGY_AGENT_PASS
        assert result.parameter is None

    def test_derived(self):
        result = parse_strategy("derived")
        assert result.strategy == STRATEGY_DERIVED
        assert result.parameter is None

    def test_json_extract(self):
        result = parse_strategy("json_extract")
        assert result.strategy == STRATEGY_JSON_EXTRACT
        assert result.parameter is None


class TestParseStrategyAliases:
    """测试简写格式（无冒号）"""

    def test_sop_shorthand(self):
        result = parse_strategy("sop")
        assert result.strategy == STRATEGY_SOP_DEFAULT

    def test_env_shorthand(self):
        result = parse_strategy("env")
        assert result.strategy == STRATEGY_ENV_INJECTION

    def test_tool_shorthand(self):
        result = parse_strategy("tool")
        assert result.strategy == STRATEGY_TOOL_CALL

    def test_skill_shorthand(self):
        result = parse_strategy("skill")
        assert result.strategy == STRATEGY_SKILL_CALL

    def test_llm_shorthand(self):
        result = parse_strategy("llm")
        assert result.strategy == STRATEGY_LLM_INFERENCE

    def test_agent_shorthand(self):
        result = parse_strategy("agent")
        assert result.strategy == STRATEGY_AGENT_PASS


class TestParseStrategyColonFormat:
    """测试冒号参数格式（核心需求）"""

    def test_skill_colon_alert_parsing(self):
        """SOP 变量声明中 skill:hci-alert-parsing 格式"""
        result = parse_strategy("skill:hci-alert-parsing")
        assert result.strategy == STRATEGY_SKILL_CALL
        assert result.parameter == "hci-alert-parsing"

    def test_skill_call_colon_alert_parsing(self):
        """全名 + 冒号格式"""
        result = parse_strategy("skill_call:hci-alert-parsing")
        assert result.strategy == STRATEGY_SKILL_CALL
        assert result.parameter == "hci-alert-parsing"

    def test_tool_colon_acli_exec(self):
        result = parse_strategy("tool:acli_exec")
        assert result.strategy == STRATEGY_TOOL_CALL
        assert result.parameter == "acli_exec"

    def test_tool_call_colon_acli_exec(self):
        result = parse_strategy("tool_call:acli_exec")
        assert result.strategy == STRATEGY_TOOL_CALL
        assert result.parameter == "acli_exec"

    def test_env_colon_node_ip(self):
        result = parse_strategy("env:node_ip")
        assert result.strategy == STRATEGY_ENV_INJECTION
        assert result.parameter == "node_ip"

    def test_env_injection_colon(self):
        result = parse_strategy("env_injection:NODE_IP")
        assert result.strategy == STRATEGY_ENV_INJECTION
        assert result.parameter == "NODE_IP"

    def test_sop_colon_none(self):
        result = parse_strategy("sop:NONE")
        assert result.strategy == STRATEGY_SOP_DEFAULT
        assert result.parameter == "NONE"

    def test_sop_default_colon(self):
        result = parse_strategy("sop_default:offline")
        assert result.strategy == STRATEGY_SOP_DEFAULT
        assert result.parameter == "offline"

    def test_llm_colon_hint(self):
        result = parse_strategy("llm:根据告警推断磁盘类型")
        assert result.strategy == STRATEGY_LLM_INFERENCE
        assert result.parameter == "根据告警推断磁盘类型"

    def test_agent_colon(self):
        result = parse_strategy("agent:initialization")
        assert result.strategy == STRATEGY_AGENT_PASS
        assert result.parameter == "initialization"


class TestBackwardCompatibility:
    """测试向后兼容旧名"""

    def test_env_context_maps_to_env_injection(self):
        """旧名 env_context → 新规范名 env_injection"""
        result = parse_strategy("env_context")
        assert result.strategy == STRATEGY_ENV_INJECTION

    def test_tool_maps_to_tool_call(self):
        """旧简写 tool → tool_call"""
        result = parse_strategy("tool")
        assert result.strategy == STRATEGY_TOOL_CALL

    def test_old_skill_prefix_style(self):
        """旧写法 skill:alert-parsing（前缀为 skill）现在可正确解析"""
        result = parse_strategy("skill:alert-parsing")
        assert result.strategy == STRATEGY_SKILL_CALL
        assert result.parameter == "alert-parsing"


class TestEdgeCases:
    """测试边界场景"""

    def test_none_input(self):
        result = parse_strategy(None)
        assert result.strategy == STRATEGY_USER_INPUT
        assert result.parameter is None
        assert result.raw == ""

    def test_empty_string(self):
        result = parse_strategy("")
        assert result.strategy == STRATEGY_USER_INPUT
        assert result.parameter is None

    def test_unknown_strategy_fallback(self):
        result = parse_strategy("completely_unknown_strategy")
        assert result.strategy == STRATEGY_USER_INPUT
        assert result.parameter is None

    def test_whitespace_trimmed(self):
        result = parse_strategy("  skill:hci-alert-parsing  ")
        assert result.strategy == STRATEGY_SKILL_CALL
        assert result.parameter == "hci-alert-parsing"

    def test_uppercase_normalized(self):
        """大小写不敏感"""
        result = parse_strategy("SKILL_CALL")
        assert result.strategy == STRATEGY_SKILL_CALL

    def test_colon_empty_param(self):
        """冒号后无参数 → parameter 为 None"""
        result = parse_strategy("skill:")
        assert result.strategy == STRATEGY_SKILL_CALL
        assert result.parameter is None

    def test_multiple_colons_takes_first(self):
        """多个冒号：以第一个冒号为分隔"""
        result = parse_strategy("tool:cmd:with:colons")
        assert result.strategy == STRATEGY_TOOL_CALL
        assert result.parameter == "cmd:with:colons"


class TestAcquisitionToolProperty:
    """测试 ParsedStrategy.acquisition_tool 属性"""

    def test_skill_call_acquisition_tool(self):
        result = parse_strategy("skill:hci-alert-parsing")
        assert result.acquisition_tool == "hci-alert-parsing"

    def test_tool_call_acquisition_tool(self):
        result = parse_strategy("tool:acli_exec")
        assert result.acquisition_tool == "acli_exec"

    def test_agent_pass_acquisition_tool(self):
        result = parse_strategy("agent:init")
        assert result.acquisition_tool == "init"

    def test_env_injection_no_acquisition_tool(self):
        """env_injection 的 parameter 不映射为 acquisition_tool"""
        result = parse_strategy("env:node_ip")
        assert result.acquisition_tool is None
        assert result.parameter == "node_ip"

    def test_user_input_no_acquisition_tool(self):
        result = parse_strategy("user_input")
        assert result.acquisition_tool is None


class TestGuardedAutoClassification:
    """测试 is_guarded / is_auto 属性"""

    @pytest.mark.parametrize("raw", ["user_input", "user_confirm", "env_injection", "env:node_ip", "env_context"])
    def test_guarded_strategies(self, raw):
        assert parse_strategy(raw).is_guarded is True
        assert is_guarded_strategy(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "tool_call",
            "tool:acli_exec",
            "skill_call",
            "skill:hci-alert-parsing",
            "llm_inference",
            "agent_pass",
            "derived",
            "json_extract",
            "sop_default",
        ],
    )
    def test_auto_strategies(self, raw):
        assert parse_strategy(raw).is_auto is True
        assert is_auto_strategy(raw) is True

    def test_user_input_not_auto(self):
        assert is_auto_strategy("user_input") is False

    def test_sop_default_not_guarded(self):
        assert is_guarded_strategy("sop_default") is False


class TestNormalizeStrategy:
    """测试 normalize_strategy 函数"""

    def test_skill_colon(self):
        assert normalize_strategy("skill:hci-alert-parsing") == STRATEGY_SKILL_CALL

    def test_env_context(self):
        assert normalize_strategy("env_context") == STRATEGY_ENV_INJECTION

    def test_none(self):
        assert normalize_strategy(None) == STRATEGY_USER_INPUT


class TestExtractAcquisitionTool:
    """测试 extract_acquisition_tool 辅助函数"""

    def test_from_colon_notation(self):
        """冒号格式的参数优先于独立字段"""
        var_def = {"acquisition_strategy": "skill:hci-alert-parsing", "acquisition_tool": "some-other-skill"}
        assert extract_acquisition_tool(var_def) == "hci-alert-parsing"

    def test_from_independent_field(self):
        """无冒号时退回到独立 acquisition_tool 字段"""
        var_def = {"acquisition_strategy": "skill_call", "acquisition_tool": "hci-alert-parsing"}
        assert extract_acquisition_tool(var_def) == "hci-alert-parsing"

    def test_no_tool(self):
        var_def = {"acquisition_strategy": "user_input"}
        assert extract_acquisition_tool(var_def) is None

    def test_env_colon_param_not_acquisition_tool(self):
        """env:node_ip 中的 node_ip 不返回为 acquisition_tool"""
        var_def = {"acquisition_strategy": "env:node_ip"}
        assert extract_acquisition_tool(var_def) is None
