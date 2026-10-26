"""SOP 变量声明契约（单一事实源）。

背景（Q2026102600003 复盘）
--------------------------
`SOP.variable_schema` 长期以"自由 JSONB 数组 + 裸 dict 取值"的方式在
kb-service（生产）/ agent-service（消费）/ conversation-service（消费）之间流转，
没有任何结构契约。后果：

1. 变量名可以写成中文描述（如 depends_on=["任务原始数据"]），保存、发布、
   运行时全部静默通过；
2. `acquisition_strategy` 写错（如把工具名写进"说明"列）会被
   `parse_strategy` 静默兜底成 `user_input`，把"自动采集"变成"人工弹框"；
3. `tool_call` 缺 `acquisition_tool` 时运行时一路落到 `_request_user_input`，
   无任何告警，表现为"填了还要填"；
4. 依赖指向未声明变量时，运行时给出 `next_tool_call` 引导模型去请求一个
   根本不存在的变量，最终写入一个永远不会被消费的野键。

本模块把变量声明升级为受契约约束的值对象，供三道闸复用：
  - save     ：导入 / PATCH / LLM 抽取回写 —— 400 拒绝
  - publish  ：draft -> published          —— 拒绝发布
  - runtime  ：sop_request_variable 入口    —— fail-loud + 指标

设计原则
--------
1. **构造即校验**：`SopVariableSchema.parse()` 成功即代表数据可用，
   下游禁止再用 `var_def.get(...)` 裸取值。
2. **不做静默兜底**：未知策略、未知类型、非法标识符一律报 error，
   不允许"猜一个默认值继续跑"。
3. **兼容存量**：未知字段通过 `extra="allow"` 保留，避免契约上线即丢字段。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.utils.acquisition_strategy import (
    STRATEGY_AGENT_PASS,
    STRATEGY_DERIVED,
    STRATEGY_ENV_INJECTION,
    STRATEGY_JSON_EXTRACT,
    STRATEGY_LLM_INFERENCE,
    STRATEGY_PATH_TEMPLATE,
    STRATEGY_SKILL_CALL,
    STRATEGY_SOP_DEFAULT,
    STRATEGY_TOOL_CALL,
    STRATEGY_USER_CONFIRM,
    STRATEGY_USER_INPUT,
    is_known_strategy,
    parse_strategy,
)

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: 变量名必须是可安全内嵌进 Tool Calling 参数与日志的标识符。
#: 中文、空格、点号、$-前缀全部拒绝 —— 这是"任务原始数据"类污染的入口防线。
VAR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: 变量类型白名单（与现有存量数据取值对齐，新增类型需同步前端渲染）
VALID_TYPES: frozenset[str] = frozenset({"string", "int", "float", "boolean", "json", "date", "ip"})

#: 变量值类型到前端输入控件语义的映射（供 admin-ui 生成表单时参考）
TYPE_INPUT_HINTS: dict[str, str] = {
    "string": "text",
    "int": "number",
    "float": "number",
    "boolean": "switch",
    "json": "textarea-json",
    "date": "date",
    "ip": "text-ip",
}

#: 模板占位符，如 /sf/log/{task_day}/sfvt_qemu_{vmid}.log 中的 {task_day}
_TEMPLATE_TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

ValidationMode = Literal["save", "publish", "runtime"]


# ──────────────────────────────────────────────────────────────────────────────
# 校验结果
# ──────────────────────────────────────────────────────────────────────────────


class VariableIssue(BaseModel):
    """单条变量契约问题。

    severity=error 表示该变量声明不可用；warning 表示可用但存在风险。
    code 为稳定的机器可读错误码，便于前端定位与埋点聚合。
    """

    severity: Literal["error", "warning"] = "error"
    code: str = Field(description="稳定错误码，如 SOP_VAR_NAME_INVALID")
    variable: str | None = Field(default=None, description="相关变量名；集合级问题为 None")
    message: str = Field(description="面向人的错误说明")
    hint: str | None = Field(default=None, description="修复建议（可选）")


class ValidationReport(BaseModel):
    """一次变量 schema 校验的完整结果。"""

    mode: ValidationMode = "save"
    issues: list[VariableIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[VariableIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[VariableIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def has_error(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def extend(self, other: ValidationReport) -> ValidationReport:
        self.issues.extend(other.issues)
        return self

    def raise_if_invalid(self) -> None:
        """有 error 时抛出 SopVariableSchemaError（携带完整报告）。"""
        if self.has_error:
            raise SopVariableSchemaError(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "ok": not self.has_error,
            "errors": [i.model_dump() for i in self.errors],
            "warnings": [i.model_dump() for i in self.warnings],
        }


class SopVariableSchemaError(ValueError):
    """变量 schema 不满足契约。"""

    def __init__(self, report: ValidationReport):
        self.report = report
        detail = "; ".join(f"[{i.code}] {i.variable or '-'}: {i.message}" for i in report.errors[:10])
        extra = f"（共 {len(report.errors)} 项）" if len(report.errors) > 10 else ""
        super().__init__(f"SOP 变量声明不满足契约：{detail}{extra}")


# ──────────────────────────────────────────────────────────────────────────────
# 单变量定义
# ──────────────────────────────────────────────────────────────────────────────


class SopVariableSpec(BaseModel):
    """单个 SOP 变量声明（值对象，构造即校验单变量规则）。

    跨变量规则（依赖存在性、环、重名）由 :class:`SopVariableSchema` 负责。
    """

    # 兼容存量字段（display_name / auto_generated / deprecated 等），避免契约
    # 上线时丢失尚未显式建模的元数据。
    model_config = ConfigDict(extra="allow")

    name: str = Field(description="变量名，必须是小写标识符")
    type: str = Field(default="string", description="变量类型")
    acquisition_strategy: str = Field(default="user_input", description="获取策略")
    acquisition_tool: str | None = Field(default=None, description="工具/技能/环境变量名")
    acquisition_args_template: Any | None = Field(default=None, description="工具参数模板")
    output_path: str | None = Field(default=None, description="从工具/技能结果中提取值的路径")
    expression: str | None = Field(
        default=None, description="derived 表达式 / json_extract JSONPath / path_template 模板"
    )
    depends_on: list[str] = Field(default_factory=list, description="前置变量名列表")
    description: str = Field(default="", description="面向人的语义说明")
    display_name: str | None = Field(default=None, description="展示名")
    default_value: Any | None = Field(default=None, description="默认值（sop_default 必填）")
    fallback_strategy: str | None = Field(default=None, description="采集失败后的兜底策略")
    validation_pattern: str | None = Field(default=None, description="取值的正则校验规则")
    required: bool = Field(default=True, description="是否为节点推进的必需变量")
    deprecated: bool = Field(default=False, description="是否已废弃")

    # ── 派生属性 ────────────────────────────────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _normalize_raw(cls, data: Any) -> Any:
        """把存量数据的常见脏形态归一（字符串 depends_on / 空字符串转 None）。"""
        if not isinstance(data, dict):
            return data
        payload = dict(data)

        deps = payload.get("depends_on")
        if isinstance(deps, str):
            payload["depends_on"] = [item.strip() for item in re.split(r"[,，\s]+", deps) if item.strip()]
        elif deps is None:
            payload["depends_on"] = []

        for key in ("acquisition_tool", "output_path", "expression", "fallback_strategy", "validation_pattern"):
            value = payload.get(key)
            if isinstance(value, str) and not value.strip():
                payload[key] = None

        strategy = payload.get("acquisition_strategy")
        payload["acquisition_strategy"] = (
            strategy.strip() if isinstance(strategy, str) and strategy.strip() else "user_input"
        )
        return payload

    @model_validator(mode="after")
    def _check_single_variable(self) -> SopVariableSpec:
        """单变量规则：标识符 / 类型 / 策略可识别性 / 策略与字段矩阵。"""
        report = ValidationReport(mode="save")
        _collect_single_variable_issues(self, report)
        if report.has_error:
            # 把问题挂在异常上，由集合层统一收集，避免 Pydantic 包装成难读的
            # ValidationError 丢失结构化错误码。
            raise _SingleVariableInvalidError(report)
        return self

    # ── 便捷访问 ────────────────────────────────────────────────────────────

    @property
    def strategy(self) -> str:
        """归一后的策略名（如 "tool:acli_exec" -> "tool_call"）。"""
        return parse_strategy(self.acquisition_strategy).strategy

    @property
    def effective_tool(self) -> str | None:
        """工具/技能名：冒号参数优先，其次 acquisition_tool 字段。"""
        return parse_strategy(self.acquisition_strategy).parameter or self.acquisition_tool

    @property
    def is_manual(self) -> bool:
        """是否需要人工介入（user_input / user_confirm / 采集失败兜底）。"""
        return self.strategy in (STRATEGY_USER_INPUT, STRATEGY_USER_CONFIRM, STRATEGY_ENV_INJECTION)

    def template_tokens(self) -> set[str]:
        """path_template 的 expression 中引用的变量名集合。"""
        if self.strategy != STRATEGY_PATH_TEMPLATE or not self.expression:
            return set()
        return set(_TEMPLATE_TOKEN_RE.findall(self.expression))


class _SingleVariableInvalidError(Exception):
    """内部信号：单变量校验失败（携带结构化报告）。"""

    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("single variable invalid")


def _collect_single_variable_issues(spec: SopVariableSpec, report: ValidationReport) -> None:
    """单变量规则实现（独立函数，便于 parse 与 validate 复用同一套判定）。"""

    # 1) 标识符
    if not VAR_NAME_RE.match(spec.name or ""):
        report.issues.append(
            VariableIssue(
                code="SOP_VAR_NAME_INVALID",
                variable=spec.name,
                message=f"变量名 '{spec.name}' 不是合法标识符（要求 ^[a-z][a-z0-9_]{{0,63}}$）",
                hint="变量名只能是英文小写、数字、下划线，且以字母开头；中文描述应放到 description",
            )
        )
        # 名字都不合法，后续字段校验没有意义
        return

    # 2) 类型
    if spec.type not in VALID_TYPES:
        report.issues.append(
            VariableIssue(
                code="SOP_VAR_TYPE_INVALID",
                variable=spec.name,
                message=f"变量类型 '{spec.type}' 不在允许集合 {sorted(VALID_TYPES)} 内",
            )
        )

    # 3) 策略必须被别名表明确识别（否则 parse_strategy 会静默兜底成 user_input）
    if not is_known_strategy(spec.acquisition_strategy):
        report.issues.append(
            VariableIssue(
                code="SOP_VAR_STRATEGY_UNKNOWN",
                variable=spec.name,
                message=(
                    f"获取策略 '{spec.acquisition_strategy}' 无法识别；"
                    "若直接入库会被静默当作 user_input（自动采集退化为人工弹框）"
                ),
                hint="合法写法：tool:acli_exec / skill:hci-task-parsing / env:node_ip / derived / path_template / user_input",
            )
        )
        return

    # 已废弃变量不会再被求值，只需保证名字与类型合法；
    # 强制其满足策略矩阵只会逼作者保留一份"假装能用"的定义。
    # 真正的约束在集合层：废弃变量不得被非废弃变量依赖（publish 态 error）。
    if spec.deprecated:
        return

    strategy = spec.strategy
    tool = spec.effective_tool

    # 4) 策略与字段矩阵
    def _err(code: str, msg: str, hint: str | None = None) -> None:
        report.issues.append(VariableIssue(code=code, variable=spec.name, message=msg, hint=hint))

    def _warn(code: str, msg: str, hint: str | None = None) -> None:
        report.issues.append(VariableIssue(severity="warning", code=code, variable=spec.name, message=msg, hint=hint))

    # 4.1 tool_call / skill_call / agent_pass：必须有工具名
    if strategy in (STRATEGY_TOOL_CALL, STRATEGY_SKILL_CALL, STRATEGY_AGENT_PASS) and not tool:
        _err(
            "SOP_VAR_TOOL_MISSING",
            f"策略为 {strategy} 但未声明 acquisition_tool；运行时会静默降级为人工弹框且不产生告警",
            "补全 acquisition_tool（如 acli_exec），或改用 derived / path_template / user_input",
        )

    # 4.2 skill_call：必须声明 output_path（否则拿不到技能输出）
    if strategy == STRATEGY_SKILL_CALL and tool and not spec.output_path:
        _err(
            "SOP_VAR_OUTPUT_PATH_MISSING",
            "skill_call 未声明 output_path，技能输出无法提取",
            "填写形如 values.vm 的提取路径",
        )

    # 4.3 derived：必须 expression
    if strategy == STRATEGY_DERIVED and not (spec.expression or "").strip():
        _err("SOP_VAR_EXPRESSION_MISSING", "derived 策略必须声明 expression")

    # 4.4 json_extract：必须 expression + depends_on
    if strategy == STRATEGY_JSON_EXTRACT:
        if not (spec.expression or "").strip():
            _err("SOP_VAR_EXPRESSION_MISSING", "json_extract 策略必须声明 expression（JSONPath）")
        if not spec.depends_on:
            _err(
                "SOP_VAR_DEPENDS_MISSING",
                "json_extract 策略必须声明 depends_on（被抽取的父变量）",
            )

    # 4.5 path_template：必须 expression 且含占位符；depends_on 由模板推导
    if strategy == STRATEGY_PATH_TEMPLATE:
        if not (spec.expression or "").strip():
            _err(
                "SOP_VAR_EXPRESSION_MISSING",
                "path_template 策略必须声明 expression（如 /sf/log/{task_day}/kernel.log）",
            )
        elif not _TEMPLATE_TOKEN_RE.search(spec.expression):
            _warn(
                "SOP_VAR_TEMPLATE_NO_TOKEN",
                "path_template 的 expression 不含任何 {var} 占位符，等同于常量，建议改用 sop_default",
            )
        else:
            tokens = set(_TEMPLATE_TOKEN_RE.findall(spec.expression))
            declared_deps = set(spec.depends_on)
            if tokens - declared_deps:
                _warn(
                    "SOP_VAR_TEMPLATE_DEP_UNALIGNED",
                    f"模板引用了未在 depends_on 声明的变量：{sorted(tokens - declared_deps)}",
                    "建议保持 depends_on 与模板占位符一致，便于依赖链解析",
                )

    # 4.6 sop_default：必须 default_value
    if strategy == STRATEGY_SOP_DEFAULT and spec.default_value in (None, ""):
        _err("SOP_VAR_DEFAULT_MISSING", "sop_default 策略必须声明 default_value")

    # 4.7 人工输入类：不应携带自动采集字段（这类字段会让人误以为已自动取值）
    if strategy in (STRATEGY_USER_INPUT, STRATEGY_USER_CONFIRM):
        if spec.output_path:
            _err(
                "SOP_VAR_MANUAL_HAS_OUTPUT_PATH",
                "user_input/user_confirm 不应声明 output_path（无自动采集动作可供提取）",
            )
        if spec.acquisition_tool and strategy == STRATEGY_USER_INPUT:
            _err(
                "SOP_VAR_MANUAL_HAS_TOOL",
                "user_input 不应声明 acquisition_tool；若确实要自动采集请改用 tool_call:xxx",
            )
        if spec.depends_on:
            _err(
                "SOP_VAR_MANUAL_HAS_DEPENDS",
                "user_input/user_confirm 是链路起点，不应声明 depends_on",
                "若本意是'先补齐前置再让人填'，请把该变量改成 tool_call / skill_call / path_template",
            )

    # 4.8 env_injection：建议声明环境变量名（存量有缺失，先告警不阻断）
    if strategy == STRATEGY_ENV_INJECTION and not tool:
        _warn(
            "SOP_VAR_ENV_TOOL_MISSING",
            "env_injection 未声明 acquisition_tool（环境变量名），注入阶段无法定位取值来源",
        )

    # 4.9 llm_inference：不建议带 output_path
    if strategy == STRATEGY_LLM_INFERENCE and spec.output_path:
        _warn("SOP_VAR_LLM_HAS_OUTPUT_PATH", "llm_inference 不应声明 output_path")

    # 4.10 兜底策略本身必须可识别
    if spec.fallback_strategy and not is_known_strategy(spec.fallback_strategy):
        _err(
            "SOP_VAR_FALLBACK_UNKNOWN",
            f"fallback_strategy '{spec.fallback_strategy}' 无法识别",
        )


# ──────────────────────────────────────────────────────────────────────────────
# 变量集合
# ──────────────────────────────────────────────────────────────────────────────


class SopVariableSchema(BaseModel):
    """一份 SOP 的完整变量声明集合（构造即完成跨变量校验）。"""

    model_config = ConfigDict(extra="allow")

    variables: list[SopVariableSpec] = Field(default_factory=list, description="变量声明列表")

    @model_validator(mode="after")
    def _check_cross_variables(self) -> SopVariableSchema:
        report = ValidationReport(mode="save")
        _collect_cross_variable_issues(self.variables, report)
        if report.has_error:
            raise _SchemaInvalidError(report)
        return self

    # ── 访问器 ──────────────────────────────────────────────────────────────

    def get(self, name: str) -> SopVariableSpec | None:
        for spec in self.variables:
            if spec.name == name:
                return spec
        return None

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def names(self) -> list[str]:
        return [spec.name for spec in self.variables]

    def active(self) -> list[SopVariableSpec]:
        """排除已废弃变量。"""
        return [spec for spec in self.variables if not spec.deprecated]

    def topological_order(self) -> list[SopVariableSpec]:
        """按依赖拓扑排序，替代运行时递归解析。

        依赖存在性与无环性已在构造期保证，这里可直接假设输入合法。
        """
        by_name = {spec.name: spec for spec in self.variables}
        ordered: list[SopVariableSpec] = []
        visited: set[str] = set()
        temp: set[str] = set()

        def _visit(name: str) -> None:
            if name in visited or name not in by_name:
                return
            if name in temp:
                return  # 环已在构造期拦截，这里防御性跳过
            temp.add(name)
            for dep in by_name[name].depends_on:
                _visit(dep)
            temp.discard(name)
            visited.add(name)
            ordered.append(by_name[name])

        for spec in self.variables:
            _visit(spec.name)
        return ordered

    # ── 构造入口 ────────────────────────────────────────────────────────────

    @classmethod
    def validate(cls, raw: Any, mode: ValidationMode = "save") -> ValidationReport:
        """收集式校验：返回全部问题，不抛异常（供 save 闸拼装 400 响应）。"""
        report = ValidationReport(mode=mode)
        specs: list[SopVariableSpec] = []

        for index, item in enumerate(_iter_raw_items(raw, report)):
            try:
                specs.append(SopVariableSpec.model_validate(item))
            except _SingleVariableInvalidError as exc:
                report.extend(exc.report)
            except Exception as exc:  # noqa: BLE001 - 兜住 Pydantic 结构错误，转成结构化 issue
                report.issues.append(
                    VariableIssue(
                        code="SOP_VAR_STRUCTURE_INVALID",
                        variable=(item.get("name") if isinstance(item, dict) else None),
                        message=f"第 {index + 1} 个变量定义结构非法：{exc}",
                    )
                )

        if specs:
            # 构造成功的变量也要补跑一次单变量规则：构造期只对 error 做 fail-fast，
            # warning（如模板占位符未对齐 depends_on）需要在报告里完整呈现。
            for spec in specs:
                _collect_single_variable_issues(spec, report)
            _collect_cross_variable_issues(specs, report)

        if mode == "publish":
            _collect_publish_issues(specs, report)

        return report

    @classmethod
    def parse(cls, raw: Any, mode: ValidationMode = "save") -> SopVariableSchema:
        """严格构造：有任何 error 即抛 SopVariableSchemaError。"""
        report = cls.validate(raw, mode=mode)
        report.raise_if_invalid()
        specs = [SopVariableSpec.model_validate(item) for item in _iter_raw_items(raw, ValidationReport())]
        return cls(variables=specs)

    @classmethod
    def from_db(cls, raw: Any, mode: ValidationMode = "save") -> SopVariableSchema:
        """从数据库 JSONB 字段构造（别名，语义更清晰）。"""
        return cls.parse(raw, mode=mode)

    def to_db(self) -> list[dict[str, Any]]:
        """导出为可写入 JSONB 的扁平列表（保留 extra 字段）。"""
        return [spec.model_dump(exclude_none=False) for spec in self.variables]


class _SchemaInvalidError(Exception):
    """内部信号：集合校验失败。"""

    def __init__(self, report: ValidationReport):
        self.report = report
        super().__init__("schema invalid")


def _iter_raw_items(raw: Any, report: ValidationReport) -> Iterable[dict[str, Any]]:
    """把任意形态的入参归一成 dict 迭代器。

    兼容：
      - list[dict]（数据库标准形态）
      - {"variables": [...]}（变量包展开后的包装形态）
      - JSON 字符串（LLM 抽取回写可能残留）
      - None / 空
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            report.issues.append(VariableIssue(code="SOP_VAR_SCHEMA_NOT_JSON", message="变量声明不是合法 JSON"))
            return []

    if isinstance(raw, dict):
        raw = raw.get("variables") if "variables" in raw else [raw]

    if not isinstance(raw, list):
        report.issues.append(
            VariableIssue(
                code="SOP_VAR_SCHEMA_NOT_ARRAY",
                message=f"变量声明必须是数组，实际类型 {type(raw).__name__}",
            )
        )
        return []

    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            items.append(item)
        else:
            report.issues.append(
                VariableIssue(
                    code="SOP_VAR_ITEM_NOT_OBJECT",
                    message=f"第 {index + 1} 个变量定义不是对象（{type(item).__name__}）",
                )
            )
    return items


def _collect_cross_variable_issues(specs: list[SopVariableSpec], report: ValidationReport) -> None:
    """跨变量规则：重名 / 依赖存在性 / 无环 / 废弃变量被引用。"""

    seen: dict[str, int] = {}
    for spec in specs:
        seen[spec.name] = seen.get(spec.name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            report.issues.append(
                VariableIssue(
                    code="SOP_VAR_DUPLICATE",
                    variable=name,
                    message=f"变量 {name} 重复声明 {count} 次",
                    hint="后定义会覆盖先定义，实际生效定义不可预期",
                )
            )

    declared = {spec.name: spec for spec in specs}

    # 依赖必须指向已声明变量 —— 直接拦截 "任务原始数据" 这类自然语言依赖。
    # 已废弃变量不会被求值，其历史依赖不参与校验（否则会逼着去"修"一份死配置）。
    for spec in specs:
        if spec.deprecated:
            continue
        for dep in spec.depends_on:
            if not VAR_NAME_RE.match(dep or ""):
                report.issues.append(
                    VariableIssue(
                        code="SOP_VAR_DEP_NOT_IDENTIFIER",
                        variable=spec.name,
                        message=f"依赖项 '{dep}' 不是合法变量名（疑似把自然语言描述写进了依赖列）",
                        hint="依赖列只能写已声明的变量名，如 task_logs",
                    )
                )
            elif dep not in declared:
                report.issues.append(
                    VariableIssue(
                        code="SOP_VAR_DEP_UNDECLARED",
                        variable=spec.name,
                        message=f"依赖项 '{dep}' 未在变量声明中定义",
                        hint=(
                            "可选相近变量：" + ", ".join(_similar_names(dep, declared.keys()))
                            if _similar_names(dep, declared.keys())
                            else "请先声明该变量，或删除此依赖"
                        ),
                    )
                )
            elif dep == spec.name:
                report.issues.append(
                    VariableIssue(
                        code="SOP_VAR_DEP_SELF",
                        variable=spec.name,
                        message="变量依赖自身",
                    )
                )

    # 依赖成环（DFS 三色标记）
    state: dict[str, int] = {}  # 0=未访问 1=访问中 2=完成

    def _dfs(name: str, path: list[str]) -> None:
        spec = declared.get(name)
        if spec is None:
            return
        state[name] = 1
        for dep in spec.depends_on:
            if dep not in declared:
                continue
            if state.get(dep) == 1:
                cycle = " -> ".join([*path[path.index(dep) :], dep]) if dep in path else f"{name} -> {dep}"
                report.issues.append(
                    VariableIssue(
                        code="SOP_VAR_DEP_CYCLE",
                        variable=name,
                        message=f"依赖成环：{cycle}",
                    )
                )
            elif state.get(dep, 0) == 0:
                _dfs(dep, [*path, dep])
        state[name] = 2

    for spec in specs:
        if state.get(spec.name, 0) == 0:
            _dfs(spec.name, [spec.name])

    # 废弃变量仍被依赖 —— 提示但不阻断（存量存在此类情况）
    for spec in specs:
        for dep in spec.depends_on:
            target = declared.get(dep)
            if target is not None and target.deprecated:
                report.issues.append(
                    VariableIssue(
                        severity="warning",
                        code="SOP_VAR_DEP_DEPRECATED",
                        variable=spec.name,
                        message=f"依赖了已废弃变量 '{dep}'",
                    )
                )


def _collect_publish_issues(specs: list[SopVariableSpec], report: ValidationReport) -> None:
    """发布态额外规则：比保存态更严。"""
    # 只有"仍在使用中的变量"引用废弃变量才是问题；
    # 整条废弃链路（如 SOP#2 的 task_* 系列）内部互相引用属于自洽状态。
    referenced = {dep for spec in specs if not spec.deprecated for dep in spec.depends_on}
    for spec in specs:
        if spec.deprecated and spec.name in referenced:
            report.issues.append(
                VariableIssue(
                    code="SOP_VAR_PUBLISH_DEPRECATED_REFERENCED",
                    variable=spec.name,
                    message=f"已废弃变量 '{spec.name}' 仍被其他变量依赖，不可发布",
                )
            )
        if spec.deprecated:
            continue
        # 非废弃变量必须有一条明确的取值路径，否则上线即"填了还要填"
        if spec.required and not _has_acquisition_path(spec):
            report.issues.append(
                VariableIssue(
                    code="SOP_VAR_PUBLISH_NO_ACQUISITION_PATH",
                    variable=spec.name,
                    message=f"必需变量 '{spec.name}' 没有任何可执行的取值路径（策略 {spec.strategy}）",
                    hint="补全工具/表达式/默认值，或改为 user_input 并写清 description",
                )
            )


def _has_acquisition_path(spec: SopVariableSpec) -> bool:
    """判断变量是否存在确定性的取值路径。"""
    strategy = spec.strategy
    if strategy == STRATEGY_USER_INPUT:
        # 人工输入也是合法路径，但必须写清楚要填什么（否则用户无从下手）
        return bool((spec.description or "").strip())
    if strategy == STRATEGY_USER_CONFIRM:
        return bool(spec.effective_tool or spec.description.strip())
    if strategy in (STRATEGY_TOOL_CALL, STRATEGY_SKILL_CALL, STRATEGY_AGENT_PASS):
        return bool(spec.effective_tool) and (strategy != STRATEGY_SKILL_CALL or bool(spec.output_path))
    if strategy in (STRATEGY_DERIVED, STRATEGY_JSON_EXTRACT, STRATEGY_PATH_TEMPLATE):
        return bool((spec.expression or "").strip())
    if strategy == STRATEGY_SOP_DEFAULT:
        return spec.default_value not in (None, "")
    if strategy == STRATEGY_ENV_INJECTION:
        return True  # 由部署期注入，运行时不要求可自取
    if strategy == STRATEGY_LLM_INFERENCE:
        return bool(spec.description.strip())
    return False


def _similar_names(name: str, candidates: Iterable[str], limit: int = 3) -> list[str]:
    """为错误提示提供相近变量名（标识符级相似度，中文不会参与比较）。"""

    def _bigrams(text: str) -> set[str]:
        return {text[i : i + 2] for i in range(len(text) - 1)} or {text}

    target = _bigrams(name.lower())
    scored: list[tuple[float, str]] = []
    for candidate in candidates:
        other = _bigrams(candidate.lower())
        score = len(target & other) / max(len(target | other), 1)
        if score > 0.0:
            scored.append((score, candidate))
    scored.sort(key=lambda item: -item[0])
    return [candidate for _, candidate in scored[:limit]]


# ──────────────────────────────────────────────────────────────────────────────
# 模块级便捷入口
# ──────────────────────────────────────────────────────────────────────────────


def validate_variable_schema(raw: Any, mode: ValidationMode = "save") -> ValidationReport:
    """校验任意形态的变量声明（不抛异常）。"""
    return SopVariableSchema.validate(raw, mode=mode)


def parse_variable_schema(raw: Any, mode: ValidationMode = "save") -> SopVariableSchema:
    """严格解析变量声明（有 error 抛 SopVariableSchemaError）。"""
    return SopVariableSchema.parse(raw, mode=mode)


__all__ = [
    "TYPE_INPUT_HINTS",
    "VALID_TYPES",
    "VAR_NAME_RE",
    "SopVariableSchema",
    "SopVariableSchemaError",
    "SopVariableSpec",
    "ValidationMode",
    "ValidationReport",
    "VariableIssue",
    "parse_variable_schema",
    "validate_variable_schema",
]
