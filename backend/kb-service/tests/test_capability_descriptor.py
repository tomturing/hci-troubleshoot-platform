from shared.schemas.acquirer_args import (
    AUTO_MODELING_SIGNAL_TOOLS,
    CONTEXT_INPUTS,
    EXECUTABLE_SIGNAL_TOOLS,
    EXPERT_MAINTAINED_SIGNAL_TOOLS,
    OFFLINE_NON_ACQUISITION_TOOLS,
)
from shared.schemas.capability_descriptor import capability_descriptor_document, get_capability_descriptor


def test_signal_tool_taxonomy_has_one_authoritative_partition():
    assert len(EXECUTABLE_SIGNAL_TOOLS) == 13
    assert len(AUTO_MODELING_SIGNAL_TOOLS) == 12
    assert {"qkv_effect"} == EXPERT_MAINTAINED_SIGNAL_TOOLS
    assert {"qkv_case_context"} == CONTEXT_INPUTS
    assert AUTO_MODELING_SIGNAL_TOOLS | EXPERT_MAINTAINED_SIGNAL_TOOLS == EXECUTABLE_SIGNAL_TOOLS
    assert AUTO_MODELING_SIGNAL_TOOLS.isdisjoint(EXPERT_MAINTAINED_SIGNAL_TOOLS)
    assert OFFLINE_NON_ACQUISITION_TOOLS == CONTEXT_INPUTS | EXPERT_MAINTAINED_SIGNAL_TOOLS


def test_descriptor_is_code_generated_and_does_not_claim_runtime_deployment():
    document = capability_descriptor_document()

    assert document["source"] == "code"
    assert document["count"] == 14
    assert {item["capability_id"] for item in document["capabilities"]} >= {
        "qkv_alert",
        "qkv_task",
        "qkv_dialog",
        "qkv_vm_console",
        "qkv_effect",
        "qkv_case_context",
        "qfk_system",
    }
    assert get_capability_descriptor("qkv_dialog")["runtime_status"] == "unknown"
    assert all(item["runtime_status"] == "unknown" for item in document["capabilities"])
    service_domains = get_capability_descriptor("qfk_service")["catalog"]["service_domains"]
    assert service_domains["asan"] == {
        "plane": "vs",
        "name": "虚拟存储",
        "runtime_exposed": False,
    }


def test_descriptor_exposes_schema_and_fails_closed_for_unknown_capability():
    descriptor = get_capability_descriptor("qfk_system")

    assert descriptor is not None
    assert descriptor["kind"] == "consumer"
    assert descriptor["args_schema"]["required"] == ["command"]
    assert descriptor["safety"]["free_shell"] is False
    assert get_capability_descriptor("filesystem_count_entries") is None


def test_vm_console_descriptor_is_conditional_producer_with_controlled_interaction():
    descriptor = get_capability_descriptor("qkv_vm_console")

    assert descriptor is not None
    # 条件型生产者：不是直接生产者（kind != producer），也不是消费者。
    assert descriptor["kind"] == "conditional_producer"
    assert descriptor["supported_matchers"] == []
    # 目标参数契约：HOST/VM_ID 必填，timeout 独立 1-60。
    assert descriptor["args_schema"]["required"] == ["host", "vm_id"]
    assert descriptor["args_schema"]["properties"]["timeout"]["maximum"] == 60
    # 安全语义：截图近似只读，但唤醒重截属受控 Guest 交互。
    safety = descriptor["safety"]
    assert safety["free_shell"] is False
    assert safety["controlled_interaction"] is True
    assert safety["read_only_intent"] is False
    assert safety["conditional"] is True
    assert safety["required_target_variables"] == ["HOST", "VM_ID"]


def test_effect_descriptor_is_read_only_conditional_producer_without_fixed_target_vars():
    descriptor = get_capability_descriptor("qkv_effect")

    assert descriptor is not None
    # 条件型生产者：不是直接生产者（kind != producer），也不是消费者。
    assert descriptor["kind"] == "conditional_producer"
    assert descriptor["supported_matchers"] == []
    # 期望锚点必填；单次观测 timeout 独立 1-60。
    assert descriptor["args_schema"]["required"] == ["expectation"]
    assert descriptor["args_schema"]["properties"]["timeout"]["maximum"] == 60
    # 安全语义：严格只读（观测全部委派只读原语），无受控交互；
    # 先决变量随期望锚点动态声明，不存在固定目标变量集合。
    safety = descriptor["safety"]
    assert safety["free_shell"] is False
    assert safety["controlled_interaction"] is False
    assert safety["read_only_intent"] is True
    assert safety["conditional"] is True
    assert safety["required_target_variables"] == []


def test_case_context_descriptor_is_non_executable_context_input():
    descriptor = get_capability_descriptor("qkv_case_context")

    assert descriptor is not None
    assert descriptor["kind"] == "context"
    assert descriptor["supported_matchers"] == []
    assert descriptor["safety"]["conditional"] is False
    assert descriptor["safety"]["controlled_interaction"] is False
    assert "不执行任何现场采集" in descriptor["limitations"][0]
