from types import SimpleNamespace

from app.services.kbd_revision_service import (
    KBD_PAYLOAD_FIELDS,
    build_kbd_revision_payload,
    diff_revision_payloads,
    payload_checksum,
)


def _entry(**overrides):
    values = {field: "" for field in KBD_PAYLOAD_FIELDS}
    values.update(
        {
            "id": 1,
            "support_id": "37150",
            "signals_json": {"schema_version": 2, "signals": []},
            "images_json": [],
            "ai_category_conf": 0.8,
            "entry_metadata": {"source": "support"},
        }
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_revision_payload_contains_reviewable_knowledge_but_not_mutable_status():
    payload = build_kbd_revision_payload(_entry(status="draft", reviewer_id=1))

    assert payload["support_id"] == "37150"
    assert payload["metadata"] == {"source": "support"}
    assert payload["payload_schema_version"] == 1
    assert "status" not in payload
    assert "reviewer_id" not in payload


def test_revision_checksum_is_stable_for_key_order_and_changes_with_knowledge():
    first = build_kbd_revision_payload(_entry(entry_metadata={"a": 1, "b": 2}))
    second = build_kbd_revision_payload(_entry(entry_metadata={"b": 2, "a": 1}))
    changed = build_kbd_revision_payload(_entry(title="专家修正后的标题"))

    assert payload_checksum(first) == payload_checksum(second)
    assert payload_checksum(first) != payload_checksum(changed)


def test_revision_diff_uses_signal_id_instead_of_unstable_array_index():
    before = {
        "signals": [
            {"id": "sig_001", "acquire": {"tool": "qfk_system", "args": {"command": "ps"}}},
            {"id": "sig_002", "acquire": {"tool": "qkv_task", "args": {"keyword": "启动失败"}}},
        ]
    }
    after = {
        "signals": [
            {"id": "sig_002", "acquire": {"tool": "qkv_task", "args": {"keyword": "开机失败"}}},
            {"id": "sig_001", "acquire": {"tool": "qfk_system", "args": {"command": "ps"}}},
        ]
    }

    changes = diff_revision_payloads(before, after)

    assert changes == [
        {
            "operation": "replace",
            "path": "/signals/sig_002/acquire/args/keyword",
            "before": "启动失败",
            "after": "开机失败",
        }
    ]


def test_revision_diff_records_add_delete_and_replace():
    changes = diff_revision_payloads(
        {"title": "旧标题", "root_cause": "旧根因", "solution": "保留"},
        {"title": "新标题", "solution": "保留", "recommendations": "新增建议"},
    )

    assert {change["operation"] for change in changes} == {"add", "delete", "replace"}
    assert {change["path"] for change in changes} == {"/recommendations", "/root_cause", "/title"}
