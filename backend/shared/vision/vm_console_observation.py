"""虚拟机控制台视觉提取的固定输出契约（设计文档 §3.5）。

视觉提取器只描述可观察事实，不给出根因结论。``display_state`` 仅允许受限词表，
词表与 ``display_state_vocabulary_revision`` 一同版本化：词表增删成员时必须提升
修订号，历史识别结果按采集时记录的词表修订回放对齐。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 受限词表（§3.5）：词表外值一律降级 unknown。
DISPLAY_STATE_VOCABULARY = frozenset(
    {
        "booting",
        "login_prompt",
        "desktop",
        "black_screen",
        "kernel_panic",
        "bsod",
        "installer_error",
        "application_error",
        "no_signal",
        "unknown",
    }
)

# 三个修订号常量：模型、提示词、词表。离线/在线必须使用同一组值。
VM_CONSOLE_MODEL_REVISION = "vision-runtime-v1"
VM_CONSOLE_PROMPT_REVISION = "vm-console-observation-v1"
VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION = "vm-console-display-state-v1"

# observation_status 受限取值：observed=获得有效观察；unavailable=政策拒绝、
# 模型失败或质量不足（不能声称"没有故障"）。
OBSERVATION_STATUSES = frozenset({"observed", "unavailable"})

# KBD 信号 produces 引用的固定变量名（§4.2）。
VM_CONSOLE_PRODUCED_VARIABLES = ("VM_CONSOLE_STATE", "VM_CONSOLE_SUMMARY", "VM_CONSOLE_CONFIDENCE", "VM_CONSOLE_ARTIFACT_ID")


class VmConsoleObservation(BaseModel):
    """视觉提取固定 Schema；低置信度/冲突/失败必须降级，不得虚构观察。"""

    model_config = ConfigDict(extra="forbid")

    observation_status: str = "observed"
    display_state: str = "unknown"
    summary: str = ""
    ocr_text: list[str] = Field(default_factory=list)
    visible_indicators: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_human_review: bool = False
    # 与制品库 SHA-256 记录一一对应；缺失会导致 VM_CONSOLE_ARTIFACT_ID 为空。
    artifact_id: str
    model_revision: str = VM_CONSOLE_MODEL_REVISION
    prompt_revision: str = VM_CONSOLE_PROMPT_REVISION
    display_state_vocabulary_revision: str = VM_CONSOLE_DISPLAY_STATE_VOCABULARY_REVISION

    @field_validator("observation_status")
    @classmethod
    def _status_known(cls, value: str) -> str:
        if value not in OBSERVATION_STATUSES:
            raise ValueError(f"observation_status 仅允许 {sorted(OBSERVATION_STATUSES)}")
        return value

    @field_validator("display_state")
    @classmethod
    def _display_state_in_vocabulary(cls, value: str) -> str:
        # 词表外值强制降级 unknown，而不是拒绝整个结果：观察结果本身仍可入库追溯。
        return value if value in DISPLAY_STATE_VOCABULARY else "unknown"

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id_required(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("artifact_id 不能为空（produces 通过 JSON path 引用该字段）")
        return str(value).strip()

    def to_produced_variables(self) -> dict[str, Any]:
        """映射为 KBD 信号 produces 声明的变量池键值。"""

        return {
            VM_CONSOLE_PRODUCED_VARIABLES[0]: self.display_state,
            VM_CONSOLE_PRODUCED_VARIABLES[1]: self.summary,
            VM_CONSOLE_PRODUCED_VARIABLES[2]: self.confidence,
            VM_CONSOLE_PRODUCED_VARIABLES[3]: self.artifact_id,
        }


def unavailable_observation(artifact_id: str, *, summary: str = "") -> VmConsoleObservation:
    """政策拒绝、模型调用失败或质量不足时的降级观察。

    语义：未获得可靠观察（UNKNOWN），不能被案例级结论解释为反证。
    """

    return VmConsoleObservation(
        observation_status="unavailable",
        display_state="unknown",
        summary=summary or "视觉观察不可用（政策拒绝、模型失败或图片质量不足）",
        confidence=0.0,
        needs_human_review=True,
        artifact_id=artifact_id,
    )
