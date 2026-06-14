"""
SOP 变量获取策略统一解析模块

设计规范：
  所有 acquisition_strategy 遵循 "实体_动作" 命名规则，同时支持冒号简写。

支持的策略（全名 / 简写 / 冒号参数格式）：
  sop_default        / sop        / sop_default:default_value 或 sop:xxx
  env_injection      / env        / env_injection:VAR_NAME 或 env:VAR_NAME
  user_input         / -          / user_input（无参数）
  user_confirm       / -          / user_confirm（无参数）
  tool_call          / tool       / tool_call:acli_exec 或 tool:acli_exec
  skill_call         / skill      / skill_call:hci-alert-parsing 或 skill:hci-alert-parsing
  llm_inference      / llm        / llm_inference:prompt_hint 或 llm:prompt_hint
  agent_pass         / agent      / agent_pass:key_name 或 agent:key_name
  derived            / -          / derived（配合 expression 字段）
  json_extract       / -          / json_extract（配合 expression / depends_on 字段）

冒号格式规则：
  strategy:parameter → 冒号左侧为策略名（全名或简写），冒号右侧为参数（acquisition_tool 或额外配置）
  参数会作为 ParsedStrategy.parameter 返回，调用方根据需要解读（如赋值给 acquisition_tool）

向后兼容：
  旧写法 "tool"、"env_context"、"skill:" 等均已纳入别名表，解析结果归一到规范策略名。

公共入口：
  parse_strategy(raw: str) -> ParsedStrategy
  normalize_strategy(raw: str) -> str         # 仅返回规范策略名
  is_auto_strategy(raw: str) -> bool          # 是否为无需用户介入的自动获取策略
  is_guarded_strategy(raw: str) -> bool       # 是否需要在变量就绪前阻断推进（env + user 类）
"""

from __future__ import annotations

from dataclasses import dataclass

# ──────────────────────────────────────────────────────────────────────────────
# 规范策略名常量
# ──────────────────────────────────────────────────────────────────────────────

STRATEGY_SOP_DEFAULT = "sop_default"
STRATEGY_ENV_INJECTION = "env_injection"
STRATEGY_USER_INPUT = "user_input"
STRATEGY_USER_CONFIRM = "user_confirm"
STRATEGY_TOOL_CALL = "tool_call"
STRATEGY_SKILL_CALL = "skill_call"
STRATEGY_LLM_INFERENCE = "llm_inference"
STRATEGY_AGENT_PASS = "agent_pass"
STRATEGY_DERIVED = "derived"
STRATEGY_JSON_EXTRACT = "json_extract"

# 所有合法的规范策略名集合
ALL_STRATEGIES: frozenset[str] = frozenset(
    {
        STRATEGY_SOP_DEFAULT,
        STRATEGY_ENV_INJECTION,
        STRATEGY_USER_INPUT,
        STRATEGY_USER_CONFIRM,
        STRATEGY_TOOL_CALL,
        STRATEGY_SKILL_CALL,
        STRATEGY_LLM_INFERENCE,
        STRATEGY_AGENT_PASS,
        STRATEGY_DERIVED,
        STRATEGY_JSON_EXTRACT,
    }
)

# ──────────────────────────────────────────────────────────────────────────────
# 别名映射表（前缀 → 规范策略名）
# 说明：key 为输入前缀（冒号左侧或完整字符串），value 为规范策略名
# ──────────────────────────────────────────────────────────────────────────────

_ALIAS_MAP: dict[str, str] = {
    # sop_default
    "sop_default": STRATEGY_SOP_DEFAULT,
    "sop": STRATEGY_SOP_DEFAULT,
    # env_injection（含旧名别名）
    "env_injection": STRATEGY_ENV_INJECTION,
    "env_context": STRATEGY_ENV_INJECTION,  # 向后兼容旧名
    "env": STRATEGY_ENV_INJECTION,
    # user_input
    "user_input": STRATEGY_USER_INPUT,
    "user": STRATEGY_USER_INPUT,
    # user_confirm
    "user_confirm": STRATEGY_USER_CONFIRM,
    "confirm": STRATEGY_USER_CONFIRM,
    # tool_call（含旧名别名）
    "tool_call": STRATEGY_TOOL_CALL,
    "tool": STRATEGY_TOOL_CALL,  # 向后兼容旧简写
    # skill_call
    "skill_call": STRATEGY_SKILL_CALL,
    "skill": STRATEGY_SKILL_CALL,
    # llm_inference
    "llm_inference": STRATEGY_LLM_INFERENCE,
    "llm": STRATEGY_LLM_INFERENCE,
    # agent_pass
    "agent_pass": STRATEGY_AGENT_PASS,
    "agent": STRATEGY_AGENT_PASS,
    # derived（无简写，有参数时用 expression 字段）
    "derived": STRATEGY_DERIVED,
    # json_extract（无简写）
    "json_extract": STRATEGY_JSON_EXTRACT,
}

# ──────────────────────────────────────────────────────────────────────────────
# 策略分组（用于各类判断逻辑）
# ──────────────────────────────────────────────────────────────────────────────

# "受守护"策略：需要在变量就绪前阻断 SOP 推进的策略（env + user 类）
GUARDED_STRATEGIES: frozenset[str] = frozenset(
    {
        STRATEGY_ENV_INJECTION,
        STRATEGY_USER_INPUT,
        STRATEGY_USER_CONFIRM,
    }
)

# "自动获取"策略：无需用户介入的策略（LLM/Tool/Skill 自动执行）
AUTO_STRATEGIES: frozenset[str] = frozenset(
    {
        STRATEGY_TOOL_CALL,
        STRATEGY_SKILL_CALL,
        STRATEGY_LLM_INFERENCE,
        STRATEGY_AGENT_PASS,
        STRATEGY_DERIVED,
        STRATEGY_JSON_EXTRACT,
        STRATEGY_SOP_DEFAULT,
    }
)


# ──────────────────────────────────────────────────────────────────────────────
# 解析结果数据类
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedStrategy:
    """策略解析结果。

    Attributes:
        strategy:   规范化策略名（如 "skill_call"、"tool_call"）
        parameter:  冒号右侧的参数值（如技能名、工具名、env 变量名），无则为 None
        raw:        原始输入字符串（调试用）
    """

    strategy: str
    parameter: str | None
    raw: str

    @property
    def acquisition_tool(self) -> str | None:
        """语义别名：对于 tool_call / skill_call / agent_pass，parameter 即为工具/技能名。

        注意：env_injection 的 parameter 是环境变量名（如 node_ip），不是工具名；
        sop_default 的 parameter 是默认值，也不是工具名。
        """
        if self.strategy in (STRATEGY_TOOL_CALL, STRATEGY_SKILL_CALL, STRATEGY_AGENT_PASS):
            return self.parameter
        return None

    @property
    def is_guarded(self) -> bool:
        """是否为受守护策略（需阻断推进直到变量就绪）。"""
        return self.strategy in GUARDED_STRATEGIES

    @property
    def is_auto(self) -> bool:
        """是否为自动获取策略（无需用户介入）。"""
        return self.strategy in AUTO_STRATEGIES


# ──────────────────────────────────────────────────────────────────────────────
# 公共解析函数
# ──────────────────────────────────────────────────────────────────────────────


def parse_strategy(raw: str | None) -> ParsedStrategy:
    """解析 acquisition_strategy 字符串，支持全名、简写、冒号参数格式。

    Args:
        raw: 原始策略字符串，如 "skill:hci-alert-parsing"、"tool_call"、"env:NODE_IP"

    Returns:
        ParsedStrategy 对象，包含规范化策略名和可选参数

    Examples:
        >>> parse_strategy("skill:hci-alert-parsing")
        ParsedStrategy(strategy="skill_call", parameter="hci-alert-parsing", raw="skill:hci-alert-parsing")

        >>> parse_strategy("tool_call")
        ParsedStrategy(strategy="tool_call", parameter=None, raw="tool_call")

        >>> parse_strategy("env:node_ip")
        ParsedStrategy(strategy="env_injection", parameter="node_ip", raw="env:node_ip")

        >>> parse_strategy("sop_default:NONE")
        ParsedStrategy(strategy="sop_default", parameter="NONE", raw="sop_default:NONE")

        >>> parse_strategy(None)  # 默认兜底
        ParsedStrategy(strategy="user_input", parameter=None, raw="")
    """
    if not raw:
        return ParsedStrategy(strategy=STRATEGY_USER_INPUT, parameter=None, raw="")

    raw_stripped = raw.strip()

    # 解析冒号分割
    if ":" in raw_stripped:
        prefix, _, param_part = raw_stripped.partition(":")
        prefix_lower = prefix.strip().lower()
        parameter: str | None = param_part.strip() or None
    else:
        prefix_lower = raw_stripped.lower()
        parameter = None

    # 别名映射
    normalized = _ALIAS_MAP.get(prefix_lower)

    # 未识别的策略：兜底为 user_input
    if normalized is None:
        normalized = STRATEGY_USER_INPUT
        parameter = None

    return ParsedStrategy(strategy=normalized, parameter=parameter, raw=raw_stripped)


def normalize_strategy(raw: str | None) -> str:
    """仅返回规范化策略名（不含参数），用于 if 判断场景。

    Args:
        raw: 原始策略字符串

    Returns:
        规范化策略名字符串
    """
    return parse_strategy(raw).strategy


def is_auto_strategy(raw: str | None) -> bool:
    """判断策略是否为自动获取（无需用户介入）。"""
    return parse_strategy(raw).is_auto


def is_guarded_strategy(raw: str | None) -> bool:
    """判断策略是否为受守护策略（需要在变量就绪前阻断 SOP 推进）。"""
    return parse_strategy(raw).is_guarded


def extract_acquisition_tool(var_def: dict) -> str | None:
    """从变量定义字典中提取 acquisition_tool。

    兼容两种来源：
    1. 冒号格式的 acquisition_strategy 中内嵌的参数（如 skill:hci-alert-parsing → hci-alert-parsing）
    2. 独立的 acquisition_tool 字段

    冒号参数优先级高于独立字段。

    Args:
        var_def: 变量 Schema 字典（包含 acquisition_strategy 和可选的 acquisition_tool）

    Returns:
        工具/技能名称，无则为 None
    """
    raw_strategy = var_def.get("acquisition_strategy") or ""
    parsed = parse_strategy(raw_strategy)
    # 冒号参数优先（仅当策略为 tool_call/skill_call/agent_pass 时）
    tool_val = parsed.acquisition_tool
    if tool_val:
        return tool_val
    # 退回到独立字段
    return var_def.get("acquisition_tool")
