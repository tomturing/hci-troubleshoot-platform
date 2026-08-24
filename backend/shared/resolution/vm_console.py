"""qkv_vm_console 条件型生产者的 Shared Resolution Runtime 支持。

设计来源：`docs/solution/agent/虚拟机控制台视觉生产者信号设计与需求.md` §5.2。

与其他 Resolver 的根本差异：本 Resolver **不返回字符串命令**，而返回不可变的
Capture Intent（采集意图）。截图与唤醒的等价 `vtpsh` 操作固定在执行端代码中
（在线为 terminal_bridge 固定操作，离线为 Go 采集器内置执行器），KBD、运营页面
和 LLM 均不能写入任意 Monitor 指令、宿主机路径或按键。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from shared.resolution.catalog import resolution_catalog_version
from shared.resolution.models import (
    ResolutionIssue,
    ResolutionPlan,
    ResolutionStatus,
    ResolvedAcquisition,
    SignalIntent,
)
from shared.schemas.acquirer_args import (
    DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    VM_CONSOLE_HOST_LITERAL_PATTERN,
    VM_CONSOLE_HOST_PLACEHOLDER,
    VM_CONSOLE_VM_ID_LITERAL_PATTERN,
    VM_CONSOLE_VM_ID_PLACEHOLDER,
)

VM_CONSOLE_TOOL = "qkv_vm_console"
VM_CONSOLE_RESOLVER_ID = "vm_console"
# 唯一允许的两个固定操作；任何未在此枚举中的 operation 直接 Fail Closed。
VM_CONSOLE_OPERATIONS = frozenset({"capture_baseline", "wake_down_key"})
VM_CONSOLE_OPERATION_CAPTURE = "capture_baseline"
VM_CONSOLE_OPERATION_WAKE = "wake_down_key"
VM_CONSOLE_CAPTURE_MODE = "baseline_then_optional_wake"
VM_CONSOLE_ARTIFACT_POLICY = "vm_console_v1"
VM_CONSOLE_TIMEOUT_RANGE = (1, 60)
# 设计文档 §4.1 禁止参数清单。acquirer_args 的 additionalProperties:false 已在
# 保存期拦截；Resolver 再次检查属于纵深防御，防止绕过保存期的直接编译调用。
VM_CONSOLE_FORBIDDEN_ARGS = frozenset(
    {
        "command",
        "monitor_command",
        "path",
        "key",
        "sleep",
        "shell",
        "url",
        "filename",
        "file",
    }
)
# 目标变量名（占位符 {{HOST}} / {{VM_ID}} 引用的变量池键）。
VM_CONSOLE_HOST_VARIABLE = "HOST"
VM_CONSOLE_VM_ID_VARIABLE = "VM_ID"


def _issue(code: str, message: str, *, field: str | None = None) -> ResolutionIssue:
    return ResolutionIssue(code=code, message=message, field=field, level="error")


class VmConsoleCaptureIntent(BaseModel):
    """不可变的虚拟机控制台采集意图。

    执行端（在线 Bridge 固定操作 / 离线 Go 执行器）只接受本结构，operation 必须
    在 ``VM_CONSOLE_OPERATIONS`` 枚举内，artifact_policy 固定；构造即校验，
    未知值 fail-closed。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    host_ref: str
    vm_ref: str
    artifact_policy: str = VM_CONSOLE_ARTIFACT_POLICY
    timeout_seconds: int = Field(default=DEFAULT_SIGNAL_TIMEOUT_SECONDS, ge=1, le=60)
    catalog_revision: str = "unknown"
    signal_id: str | None = None

    @field_validator("operation")
    @classmethod
    def _operation_must_be_known(cls, value: str) -> str:
        if value not in VM_CONSOLE_OPERATIONS:
            raise ValueError(f"未知的虚拟机控制台操作（fail-closed）: {value}")
        return value

    @field_validator("artifact_policy")
    @classmethod
    def _artifact_policy_fixed(cls, value: str) -> str:
        if value != VM_CONSOLE_ARTIFACT_POLICY:
            raise ValueError(f"制品策略固定为 {VM_CONSOLE_ARTIFACT_POLICY}，不接受自定义值")
        return value

    @field_validator("host_ref", "vm_ref")
    @classmethod
    def _ref_not_empty(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("host_ref/vm_ref 不能为空")
        return str(value).strip()


def build_wake_intent(
    host_ref: str,
    vm_ref: str,
    *,
    timeout_seconds: int = DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    catalog_revision: str | None = None,
    signal_id: str | None = None,
) -> VmConsoleCaptureIntent:
    """构造唤醒（sendkey down）意图。

    注意：本函数只负责构造；**调度前置条件**（用户确认已记录、每诊断运行最多
    一次）由服务端适配器强制，KBD 不得绕过。
    """

    return VmConsoleCaptureIntent(
        operation=VM_CONSOLE_OPERATION_WAKE,
        host_ref=host_ref,
        vm_ref=vm_ref,
        timeout_seconds=timeout_seconds,
        catalog_revision=catalog_revision or resolution_catalog_version(),
        signal_id=signal_id,
    )


def capture_intent_from(acquisition: ResolvedAcquisition) -> VmConsoleCaptureIntent | None:
    """从 verified acquisition 还原 Capture Intent；还原失败即拒绝执行。"""

    payload = (acquisition.evidence or {}).get("capture_intent")
    if not isinstance(payload, dict):
        return None
    try:
        return VmConsoleCaptureIntent.model_validate(payload)
    except PydanticValidationError:
        return None


class VmConsoleResolver:
    """把 qkv_vm_console 信号编译为受控 Capture Intent（不产出命令字符串）。"""

    resolver_id = VM_CONSOLE_RESOLVER_ID

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        args = dict(intent.args)
        catalog_version = resolution_catalog_version()

        def blocked(issues: list[ResolutionIssue]) -> ResolutionPlan:
            return ResolutionPlan(
                resolver_id=self.resolver_id,
                tool=intent.tool or VM_CONSOLE_TOOL,
                canonical_args=dict(intent.args),
                catalog_version=catalog_version,
                status=ResolutionStatus.BLOCKED,
                issues=issues,
            )

        forbidden = sorted(set(args) & VM_CONSOLE_FORBIDDEN_ARGS)
        if forbidden:
            return blocked(
                [
                    _issue(
                        "VM_CONSOLE_ARGS_FORBIDDEN",
                        f"qkv_vm_console 不接受自由执行参数: {', '.join(forbidden)}",
                        field="acquire.args",
                    )
                ]
            )

        capture_mode = str(args.get("capture_mode") or VM_CONSOLE_CAPTURE_MODE)
        if capture_mode != VM_CONSOLE_CAPTURE_MODE:
            return blocked(
                [
                    _issue(
                        "VM_CONSOLE_CAPTURE_MODE_INVALID",
                        f"capture_mode 仅支持 {VM_CONSOLE_CAPTURE_MODE}: {capture_mode}",
                        field="acquire.args.capture_mode",
                    )
                ]
            )

        host = str(args.get("host") or "").strip()
        vm_id = str(args.get("vm_id") or "").strip()
        if not host or (host != VM_CONSOLE_HOST_PLACEHOLDER and not VM_CONSOLE_HOST_LITERAL_PATTERN.fullmatch(host)):
            return blocked(
                [
                    _issue(
                        "VM_CONSOLE_HOST_INVALID",
                        "host 仅允许 {{HOST}} 占位符或系统规范化节点标识",
                        field="acquire.args.host",
                    )
                ]
            )
        if not vm_id or (
            vm_id != VM_CONSOLE_VM_ID_PLACEHOLDER and not VM_CONSOLE_VM_ID_LITERAL_PATTERN.fullmatch(vm_id)
        ):
            return blocked(
                [
                    _issue(
                        "VM_CONSOLE_VM_ID_INVALID",
                        "vm_id 仅允许 {{VM_ID}} 占位符或精确数值型 VMID",
                        field="acquire.args.vm_id",
                    )
                ]
            )

        timeout_raw = args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS)
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
            return blocked([_issue("VM_CONSOLE_TIMEOUT_INVALID", "timeout 必须是整数（秒，1-60）", field="acquire.args.timeout")])
        low, high = VM_CONSOLE_TIMEOUT_RANGE
        if not low <= timeout_raw <= high:
            return blocked(
                [
                    _issue(
                        "VM_CONSOLE_TIMEOUT_INVALID",
                        f"timeout 必须在 {low}-{high}（快速失败型采集）: {timeout_raw}",
                        field="acquire.args.timeout",
                    )
                ]
            )

        capture_intent = VmConsoleCaptureIntent(
            operation=VM_CONSOLE_OPERATION_CAPTURE,
            host_ref=host,
            vm_ref=vm_id,
            timeout_seconds=timeout_raw,
            catalog_revision=catalog_version,
        )
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool or VM_CONSOLE_TOOL,
            canonical_args={
                "capture_intent": capture_intent.model_dump(mode="json"),
                "capture_mode": capture_mode,
                "timeout_seconds": timeout_raw,
            },
            # 刻意为空：本 Resolver 不产出任何命令字符串。
            argv_template=[],
            catalog_version=catalog_version,
        )

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        if plan.status is ResolutionStatus.BLOCKED:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=plan.issues,
            )

        payload = plan.canonical_args.get("capture_intent")
        try:
            intent = VmConsoleCaptureIntent.model_validate(payload)
        except PydanticValidationError as exc:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=[_issue("VM_CONSOLE_INTENT_INVALID", f"Capture Intent 不可还原: {exc}")],
            )

        values = dict((context or {}).get("variables") or {})
        host, host_unresolved = self._substitute(
            intent.host_ref, values, VM_CONSOLE_HOST_PLACEHOLDER, VM_CONSOLE_HOST_VARIABLE
        )
        vm_id, vm_unresolved = self._substitute(
            intent.vm_ref, values, VM_CONSOLE_VM_ID_PLACEHOLDER, VM_CONSOLE_VM_ID_VARIABLE
        )
        unresolved = sorted({name for name in (host_unresolved, vm_unresolved) if name})
        if unresolved:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.NEEDS_PROBE,
                catalog_version=plan.catalog_version,
                issues=[
                    ResolutionIssue(
                        code="VARIABLE_UNRESOLVED",
                        message=f"控制台截图目标变量未解析: {', '.join(unresolved)}",
                        field=",".join(unresolved),
                        # needs_probe 是运行时待解析项，不是静态错误（review.py 头注）；
                        # 发布/专家审查保留为待确认，Agent 执行前由 verified 门禁阻断。
                        level="warning",
                    )
                ],
                evidence={"capture_intent": intent.model_dump(mode="json")},
            )

        if not VM_CONSOLE_HOST_LITERAL_PATTERN.fullmatch(host):
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=[_issue("VM_CONSOLE_HOST_INVALID", f"解析后的节点标识不安全: {host}", field="host")],
                evidence={"capture_intent": intent.model_dump(mode="json")},
            )
        if not VM_CONSOLE_VM_ID_LITERAL_PATTERN.fullmatch(vm_id):
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=[_issue("VM_CONSOLE_VM_ID_INVALID", f"解析后的 VMID 不是精确数值标识: {vm_id}", field="vm_id")],
                evidence={"capture_intent": intent.model_dump(mode="json")},
            )

        resolved_intent = intent.model_copy(update={"host_ref": host, "vm_ref": vm_id})
        variables_used: dict[str, Any] = {}
        if host_unresolved is None and intent.host_ref != host:
            variables_used[VM_CONSOLE_HOST_VARIABLE] = host
        if vm_unresolved is None and intent.vm_ref != vm_id:
            variables_used[VM_CONSOLE_VM_ID_VARIABLE] = vm_id
        return ResolvedAcquisition(
            resolver_id=self.resolver_id,
            tool=plan.tool,
            status=ResolutionStatus.VERIFIED,
            # argv 为空、command 为 None 是刻意设计：执行端只消费 capture_intent。
            argv=[],
            command=None,
            resolution_rule="vm-console-fixed-capture-intent",
            catalog_version=plan.catalog_version,
            variables_used=variables_used,
            evidence={"capture_intent": resolved_intent.model_dump(mode="json")},
        )

    @staticmethod
    def _substitute(
        value: str, values: dict[str, Any], placeholder: str, variable_name: str
    ) -> tuple[str, str | None]:
        """把 ``{{HOST}}``/``{{VM_ID}}`` 占位符替换为变量池取值。

        返回 ``(resolved_value, unresolved_variable_name)``；字面量原样返回且
        unresolved 为 None。变量缺失/空值时返回原占位符与变量名（fail-closed）。
        """

        if value != placeholder:
            return value, None

        resolved = values.get(variable_name)
        if resolved in (None, ""):
            # 容忍大小写不一致的变量池键（如 host），与既有变量解析保持同等克制：
            # 仅在唯一匹配时采用，多个同名变体视为未解析。
            candidates = {key: item for key, item in values.items() if key.upper() == variable_name}
            if len(candidates) == 1:
                resolved = next(iter(candidates.values()))
        if resolved in (None, ""):
            return value, variable_name
        return str(resolved).strip(), None
