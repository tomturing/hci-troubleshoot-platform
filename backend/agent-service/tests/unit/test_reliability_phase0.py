from app.adapters.agents.htp.react_engine import ToolCallValidator, ToolResultEnvelope
from app.core.utils import smart_truncate
from app.tools.acli.executor import ExecResult, ExitCodeMeaning


def test_smart_truncate_under_limit():
    text = "Hello World\nLine 2"
    assert smart_truncate(text, 100) == text


def test_smart_truncate_over_limit_with_errors():
    # Construct a string with head, middle containing errors, and tail
    head = "Head context line 1\nHead context line 2\n"
    middle = (
        "\n".join([f"Generic line {i}" for i in range(100)])
        + "\nCRITICAL ERROR: VM disk space full!\n"
        + "\n".join([f"Generic line {i}" for i in range(100, 200)])
    )
    tail = "\nTail context line 1\nTail context line 2"

    output = head + middle + tail

    # Truncate to 1000 characters
    truncated = smart_truncate(output, 1000)

    assert len(truncated) <= 1000
    assert "CRITICAL ERROR: VM disk space full!" in truncated
    assert "Head context line 1" in truncated
    assert "Tail context line 2" in truncated
    assert "此处截断" in truncated


def test_tool_call_validator_required_fields():
    schema = {
        "type": "object",
        "required": ["vm_id", "reason"],
        "properties": {
            "vm_id": {"type": "string"},
            "reason": {"type": "string"},
        },
    }

    # Missing required field
    is_valid, msg = ToolCallValidator.validate("test_tool", {"vm_id": "vm-123"}, schema)
    assert not is_valid
    assert "缺少必填参数" in msg

    # All fields present
    is_valid, msg = ToolCallValidator.validate("test_tool", {"vm_id": "vm-123", "reason": "test"}, schema)
    assert is_valid
    assert msg is None


def test_tool_call_validator_type_checking():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "force": {"type": "boolean"},
            "ratio": {"type": "number"},
        },
    }

    # Wrong integer type (passing str)
    is_valid, msg = ToolCallValidator.validate("test_tool", {"count": "five"}, schema)
    assert not is_valid
    assert "count" in msg
    assert "期望 integer" in msg

    # Wrong boolean type (passing int/str)
    is_valid, msg = ToolCallValidator.validate("test_tool", {"force": 1}, schema)
    assert not is_valid
    assert "force" in msg

    # Valid types
    is_valid, msg = ToolCallValidator.validate("test_tool", {"count": 5, "force": True, "ratio": 0.8}, schema)
    assert is_valid


def test_tool_call_validator_ip_format():
    schema = {"type": "object", "properties": {"node_ip": {"type": "string", "format": "ipv4"}}}

    # Invalid IP format
    is_valid, msg = ToolCallValidator.validate("test_tool", {"node_ip": "192.168.1.300"}, schema)
    assert not is_valid
    assert "不是有效的 IPv4 地址" in msg

    # Valid IP format
    is_valid, msg = ToolCallValidator.validate("test_tool", {"node_ip": "192.168.1.1"}, schema)
    assert is_valid


def test_tool_result_envelope_from_exec_result_success():
    exec_res = ExecResult(
        stdout="Success stdout message",
        stderr="",
        exit_code=0,
        command="acli vm list",
        node="192.168.1.1",
        duration_ms=100,
        truncated=False,
        risk_level=1,
        exit_code_meaning=ExitCodeMeaning.SUCCESS,
    )

    envelope = ToolResultEnvelope.from_raw_result("acli_vm_list", "exec-123", exec_res)
    assert envelope.success is True
    assert envelope.exit_code == 0
    assert "Success stdout message" in envelope.stdout

    msg = envelope.to_llm_message()
    assert "🛠️ [Tool: acli_vm_list]" in msg
    assert "✅ SUCCESS" in msg
    assert "Success stdout message" in msg


def test_tool_result_envelope_from_exec_result_timeout():
    exec_res = ExecResult(
        stdout="",
        stderr="timeout error occurred",
        exit_code=-1,
        command="acli netdoctor",
        node="192.168.1.1",
        duration_ms=32000,
        truncated=False,
        risk_level=2,
        exit_code_meaning=ExitCodeMeaning.TIMEOUT,
    )

    envelope = ToolResultEnvelope.from_raw_result("acli_netdoctor", "exec-123", exec_res)
    assert envelope.success is False
    assert envelope.exit_code == -1
    assert envelope.exit_code_meaning == ExitCodeMeaning.TIMEOUT
    assert "超时" in envelope.interpretation

    msg = envelope.to_llm_message()
    assert "❌ FAILED" in msg
    assert "timeout" in msg
    assert "Interpretation:" in msg


def test_tool_result_envelope_from_dict():
    res_dict = {"node_id": "n-1", "title": "SOP Node Title"}
    envelope = ToolResultEnvelope.from_raw_result("get_sop_node", "exec-123", res_dict)

    assert envelope.success is True
    assert envelope.exit_code == 0
    assert "SOP Node Title" in envelope.stdout
