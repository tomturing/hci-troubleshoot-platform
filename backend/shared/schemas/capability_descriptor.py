"""从 shared Signal 契约生成只读 Capability Descriptor。

这是轻治理阶段的最小发现面：它只声明代码仓库中已注册的参数契约，不把数据库
``tool_definition`` 的可编辑记录冒充为 Agent Handler 已部署。后续 Agent 可在同一结构
上补充 handler/validator/deployment 探测结果，无需建设 Registry 数据库。
"""

from __future__ import annotations

from typing import Any

from shared.schemas.acquirer_args import ACQUIRER_ARGS_SCHEMA, FRONTEND_TOOLS

DESCRIPTOR_SCHEMA_VERSION = 1


def build_capability_descriptors() -> list[dict[str, Any]]:
    """按 capability_id 稳定排序生成代码契约描述。"""

    descriptors: list[dict[str, Any]] = []
    for capability_id, args_schema in sorted(ACQUIRER_ARGS_SCHEMA.items()):
        is_frontend = capability_id in FRONTEND_TOOLS
        descriptors.append(
            {
                "capability_id": capability_id,
                "version": "1",
                "kind": "producer" if is_frontend else "consumer",
                "args_schema": args_schema,
                "supported_matchers": [] if is_frontend else ["keyword", "regex", "state", "exists"],
                "contract_status": "available",
                # shared/kb-service 不能冒充 agent-service 的实际部署探测结果。
                "runtime_status": "unknown",
                "verification_status": "contract_only",
                "safety": {
                    "declarative_only": True,
                    "free_shell": False,
                    "read_only_intent": True,
                },
                "source": "shared.schemas.acquirer_args",
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
