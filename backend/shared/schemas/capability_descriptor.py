"""从 shared Signal 契约生成只读 Capability Descriptor。

这是轻治理阶段的最小发现面：它只声明代码仓库中已注册的参数契约，不把数据库
``tool_definition`` 的可编辑记录冒充为 Agent Handler 已部署。后续 Agent 可在同一结构
上补充 handler/validator/deployment 探测结果，无需建设 Registry 数据库。
"""

from __future__ import annotations

from typing import Any

from shared.schemas.acquirer_args import (
    ACQUIRER_ARGS_SCHEMA,
    CONDITIONAL_PRODUCERS,
    FRONTEND_TOOLS,
    SERVICE_DOMAIN_CATALOG,
    VM_CONSOLE_REQUIRED_TARGET_VARS,
)
from shared.schemas.log_source_catalog import LOG_MATCHER_TYPES, log_source_catalog_document

DESCRIPTOR_SCHEMA_VERSION = 1


def build_capability_descriptors() -> list[dict[str, Any]]:
    """按 capability_id 稳定排序生成代码契约描述。"""

    descriptors: list[dict[str, Any]] = []
    for capability_id, args_schema in sorted(ACQUIRER_ARGS_SCHEMA.items()):
        is_frontend = capability_id in FRONTEND_TOOLS
        is_conditional_producer = capability_id in CONDITIONAL_PRODUCERS
        # 效果验证生产者虽属条件型，但严格只读（观测全部委派只读原语），
        # 且先决变量随期望锚点动态声明，不固定为 HOST/VM_ID。
        is_effect_producer = capability_id == "qkv_effect"
        supported_matchers = (
            []
            if is_frontend or is_conditional_producer
            else list(LOG_MATCHER_TYPES)
            if capability_id == "qfk_log"
            else ["keyword", "regex", "state", "threshold", "delta", "trend", "exists"]
        )
        kind = (
            "producer"
            if is_frontend
            else "conditional_producer"
            if is_conditional_producer
            else "consumer"
        )
        descriptors.append(
            {
                "capability_id": capability_id,
                "version": "1",
                "kind": kind,
                "args_schema": args_schema,
                "supported_matchers": supported_matchers,
                "contract_status": "available",
                # shared/kb-service 不能冒充 agent-service 的实际部署探测结果。
                "runtime_status": "unknown",
                "verification_status": "contract_only",
                "safety": {
                    "declarative_only": True,
                    "free_shell": False,
                    # 截图阶段近似只读；近黑后的 sendkey down 属受控 Guest 交互，
                    # 不是只读操作，须运行时人工确认。qkv_effect 严格只读。
                    "read_only_intent": is_effect_producer or not is_conditional_producer,
                    "controlled_interaction": is_conditional_producer and not is_effect_producer,
                    "conditional": is_conditional_producer,
                    # qkv_effect 的先决变量随期望锚点动态声明（发布门禁校验来源可达），
                    # 不存在固定的目标变量集合。
                    "required_target_variables": (
                        []
                        if is_effect_producer
                        else sorted(VM_CONSOLE_REQUIRED_TARGET_VARS)
                        if is_conditional_producer
                        else []
                    ),
                },
                "source": "shared.schemas.acquirer_args",
                "limitations": (
                    ["复合取值能力：在当前主控 /sf/log/today 与 /sf/log/today/vt 检索弹框文本并提取 END/REQUEST_ID"]
                    if capability_id == "qkv_dialog"
                    else [
                        "条件型效果验证生产者：期望锚点（观测通道+封闭 matcher+窗口）必须结构化声明且变量来源可达；"
                        "严格只读，不得作为 KBD 唯一生产者；三态判定 achieved/not_achieved/inconclusive 禁止静默坍缩"
                    ]
                    if is_effect_producer
                    else [
                        "条件型实时视觉生产者：必须先具备可信 HOST 与 VM_ID 才可执行；"
                        "基线截图近似只读，唤醒重截（sendkey down）为受控交互且每诊断运行最多一次"
                    ]
                    if is_conditional_producer
                    else []
                ),
                "catalog": (
                    log_source_catalog_document()
                    if capability_id == "qfk_log"
                    else {"service_domains": SERVICE_DOMAIN_CATALOG}
                    if capability_id == "qfk_service"
                    else None
                ),
            }
        )
    return descriptors


def get_capability_descriptor(capability_id: str) -> dict[str, Any] | None:
    """获取单项 descriptor；未知能力返回 ``None``，由调用方 fail closed。"""

    return next(
        (item for item in build_capability_descriptors() if item["capability_id"] == capability_id),
        None,
    )


def capability_descriptor_document() -> dict[str, Any]:
    """生成 API/Prompt/Admin 共用的稳定文档信封。"""

    descriptors = build_capability_descriptors()
    return {
        "schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "source": "code",
        "capabilities": descriptors,
        "count": len(descriptors),
        "limitations": [
            "runtime_status=unknown 表示尚未从 agent-service 探测已部署 Handler/Validator",
            "数据库 tool_definition 可编辑记录不能单独证明 executable capability 已实现",
        ],
    }
