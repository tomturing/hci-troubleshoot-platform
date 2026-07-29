from shared.schemas.capability_descriptor import capability_descriptor_document, get_capability_descriptor


def test_descriptor_is_code_generated_and_does_not_claim_runtime_deployment():
    document = capability_descriptor_document()

    assert document["source"] == "code"
    assert document["count"] == 11
    assert {item["capability_id"] for item in document["capabilities"]} >= {
        "qkv_alert",
        "qkv_task",
        "qkv_dialog",
        "qfk_system",
    }
    assert all(item["runtime_status"] == "unknown" for item in document["capabilities"])


def test_descriptor_exposes_schema_and_fails_closed_for_unknown_capability():
    descriptor = get_capability_descriptor("qfk_system")

    assert descriptor is not None
    assert descriptor["kind"] == "consumer"
    assert descriptor["args_schema"]["required"] == ["command"]
    assert descriptor["safety"]["free_shell"] is False
    assert get_capability_descriptor("filesystem_count_entries") is None
