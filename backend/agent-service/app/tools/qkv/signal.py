"""
QKV 前端信号数据模型与类型定义
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator
from shared.signals.qkv_output_processing import validate_output_processing


class FrontendQueryType(StrEnum):
    """前端信号查询类型"""

    ALERT = "alert"  # 告警信息
    TASK = "task"  # 操作任务
    DIALOG = "dialog"  # 对话/弹框日志
    # 条件型实时视觉生产者：虚拟机控制台截图。执行走专用适配器
    # （app/tools/vm_console/），绝不落入自由文本 qkv_exec 路径。
    VM_CONSOLE = "vm_console"
    # 条件型效果验证生产者：期望 × 观测的三态判定。执行走专用适配器
    # （app/tools/effect/），绝不落入自由文本 qkv_exec 路径。
    EFFECT = "effect"


# ─── 关键词清洗后缀映射 ───────────────────────────────────────────────────────
# 用于规范化 -k 参数值，避免 LLM 抽取时混入类型后缀
_KEYWORD_CLEAN_SUFFIXES: dict[str, str] = {
    "alert": "告警",
    "task": "失败",
    "dialog": "弹框",
}


def _clean_keyword(keyword: str, query_type: str) -> tuple[str, bool]:
    """清洗关键词，去掉类型后缀，并检测是否包含状态标识。

    Args:
        keyword: 原始关键词
        query_type: 查询类型 (alert/task/dialog)

    Returns:
        (cleaned_keyword, is_failed)
        - cleaned_keyword: 清洗后的关键词
        - is_failed: 是否包含"失败"状态（仅对 task 有效）
    """
    if not keyword:
        return keyword, False

    suffix = _KEYWORD_CLEAN_SUFFIXES.get(query_type, "")
    is_failed = False
    cleaned = keyword

    # task 特殊处理：检测"失败"并设置状态
    if query_type == "task":
        if "失败" in cleaned:
            is_failed = True
            # 去掉"失败"（无论在哪个位置）
            cleaned = cleaned.replace("失败", "")
        # 额外清洗"成功"、"运行中"等状态后缀
        for status_suffix in ["成功", "运行中"]:
            cleaned = cleaned.replace(status_suffix, "")

    # 清洗类型后缀
    if suffix and cleaned.endswith(suffix):
        cleaned = cleaned[: -len(suffix)]

    return cleaned.strip(), is_failed


class FrontendSignal(BaseModel):
    """
    前端信号模型（QKV 加载处理）
    """

    query: FrontendQueryType = Field(..., description="Q: 查什么，告警/任务/弹框/控制台截图")
    keyword: str = Field(default="", description="K: 匹配关键字（vm_console 不使用）")
    is_failed: bool = Field(default=False, description="是否只查失败任务 (仅在 query 为 task 时生效)")
    limit: int = Field(default=100, description="最大返回数据量限制")
    paths: list[str] = Field(
        default_factory=lambda: ["/sf/log/today", "/sf/log/today/vt"],
        description="qkv_dialog 固定日志搜索域",
    )
    context_lines: int = Field(default=2, ge=0, le=10, description="qkv_dialog 命中行上下文")
    produces: list[dict[str, str]] = Field(
        default_factory=list,
        description="产出变量规格：[{name: 'HOST', path: 'host'}, ...]，为空时 parser 走硬编码兜底",
    )
    output_processing: list[dict[str, Any]] = Field(
        default_factory=list,
        description="QKV produces 完成后执行的可选确定性后处理；不创建新的采集信号",
    )
    # ── qkv_vm_console 专用目标参数（其余 query 类型不使用）──
    host: str | None = Field(default=None, description="vm_console/effect 目标宿主机（{{HOST}} 或规范化节点标识）")
    vm_id: str | None = Field(default=None, description="vm_console 目标 VMID（{{VM_ID}} 或精确数值）")
    capture_mode: str = Field(
        default="baseline_then_optional_wake",
        description="vm_console 固定采集模式",
    )
    # ── qkv_effect 专用期望锚点（其余 query 类型不使用）──
    usage: str = Field(default="remediation_verify", description="effect 使用模式（修复后复核/症状确认）")
    expectation: dict[str, Any] | None = Field(
        default=None,
        description="effect 结构化期望锚点：observation + matcher + settle/window/max_recheck",
    )
    timeout: int = Field(default=60, ge=1, le=300, description="采集超时；vm_console/effect 运行期另行约束 1-60")

    @model_validator(mode="after")
    def _validate_dialog_scope(self) -> FrontendSignal:
        if self.query == FrontendQueryType.DIALOG:
            allowed = {"/sf/log/today", "/sf/log/today/vt"}
            if not self.paths or len(self.paths) > 2 or len(set(self.paths)) != len(self.paths):
                raise ValueError("qkv_dialog.paths 必须包含 1-2 个不重复的固定日志目录")
            if any(path not in allowed for path in self.paths):
                raise ValueError("qkv_dialog.paths 只允许 /sf/log/today 与 /sf/log/today/vt")
        return self

    @model_validator(mode="after")
    def _validate_vm_console_targets(self) -> FrontendSignal:
        """vm_console 必须显式携带 HOST/VM_ID 目标；其他 query 类型不受影响。"""
        if self.query == FrontendQueryType.VM_CONSOLE:
            if not str(self.host or "").strip():
                raise ValueError("qkv_vm_console 必须提供 host（{{HOST}} 或规范化节点标识）")
            if not str(self.vm_id or "").strip():
                raise ValueError("qkv_vm_console 必须提供 vm_id（{{VM_ID}} 或精确数值 VMID）")
            if self.capture_mode != "baseline_then_optional_wake":
                raise ValueError("qkv_vm_console.capture_mode 仅支持 baseline_then_optional_wake")
            if not 1 <= self.timeout <= 60:
                raise ValueError("qkv_vm_console.timeout 必须在 1-60（快速失败型采集）")
        return self

    @model_validator(mode="after")
    def _validate_effect_expectation(self) -> FrontendSignal:
        """effect 必须显式携带结构化期望锚点；其他 query 类型不受影响。"""
        if self.query == FrontendQueryType.EFFECT:
            if not isinstance(self.expectation, dict) or not self.expectation:
                raise ValueError("qkv_effect 必须提供结构化期望锚点 expectation")
            if not isinstance(self.expectation.get("observation"), dict):
                raise ValueError("qkv_effect.expectation.observation 必填（封闭观测通道引用）")
            if not isinstance(self.expectation.get("matcher"), dict):
                raise ValueError("qkv_effect.expectation.matcher 必填（封闭判定规则）")
            if self.usage not in ("remediation_verify", "symptom_confirm"):
                raise ValueError("qkv_effect.usage 仅支持 remediation_verify/symptom_confirm")
            if not 1 <= self.timeout <= 60:
                raise ValueError("qkv_effect.timeout 必须在 1-60（单次观测快速失败）")
        return self

    @model_validator(mode="after")
    def _validate_output_processing(self) -> FrontendSignal:
        available_inputs = {
            str(item.get(key) or "").strip().upper()
            for item in self.produces
            if isinstance(item, dict)
            for key in ("name", "alias")
            if str(item.get(key) or "").strip()
        }
        validate_output_processing(self.output_processing, available_inputs=available_inputs)
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrontendSignal:
        """从字典构建并校验，自动清洗关键词和检测状态。

        直接切 v2 列形态（RFC §4.4）：原生读取 acquire/args/orchestrate 段，
        无需任何 v1 扁平桥接还原——v2 嵌套信号由运行时直接消费。
        """
        if "acquire" in data:  # v2 嵌套信号
            a = data["acquire"]
            args = a.get("args", {}) or {}
            tool = a.get("tool", "")
            qmap = {
                "qkv_alert": "alert",
                "qkv_task": "task",
                "qkv_dialog": "dialog",
                "qkv_vm_console": "vm_console",
                "qkv_effect": "effect",
            }
            data = {
                "query": qmap.get(tool, "task"),
                "keyword": args.get("keyword", ""),
                "is_failed": bool(args.get("is_failed", False)),
                "limit": args.get("limit", 100),
                "paths": args.get("paths", ["/sf/log/today", "/sf/log/today/vt"]),
                "context_lines": args.get("context_lines", 2),
                "produces": (data.get("orchestrate") or {}).get("produces", []),
                "output_processing": (data.get("orchestrate") or {}).get("output_processing", []),
                "host": args.get("host"),
                "vm_id": args.get("vm_id"),
                "capture_mode": args.get("capture_mode", "baseline_then_optional_wake"),
                "usage": args.get("usage", "remediation_verify"),
                "expectation": args.get("expectation"),
                "timeout": args.get("timeout", 60),
            }
        # 自动清洗关键词和检测状态
        keyword = data.get("keyword", "")
        query_type = data.get("query", "")
        if keyword and query_type:
            cleaned_keyword, detected_failed = _clean_keyword(keyword, query_type)
            data = {**data, "keyword": cleaned_keyword}
            # 如果检测到"失败"，设置 is_failed（除非显式指定了 is_failed=False）
            if detected_failed and data.get("is_failed") is None:
                data["is_failed"] = True
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> FrontendSignal:
        """从 JSON 串反序列化并校验"""
        data = json.loads(json_str)
        return cls.from_dict(data)
