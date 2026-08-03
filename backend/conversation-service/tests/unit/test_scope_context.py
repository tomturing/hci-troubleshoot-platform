"""Conversation → Agent CDD Scope Context 契约回归。"""

from app.services.conversation_service import _with_scope_context


def test_scope_context_promotes_authoritative_cluster_and_category_fields():
    context = {
        "is_raw": True,
        "env_info": {"hci_version": "6.11.1_R1", "name": "test-hci"},
        "alert_logs": [],
        "task_logs": [],
    }

    assert _with_scope_context(context, category_id="虚拟机-003") == {
        **context,
        "product": "HCI",
        "version": "6.11.1_R1",
        "components": ["虚拟机"],
    }


def test_scope_context_preserves_explicit_environment_values_and_adds_confirmed_domain():
    context = {
        "product": "HCI",
        "version": "6.10.0_R2",
        "components": ["存储"],
        "topology": ["external_storage"],
        "env_info": {"hci_version": "6.11.1_R1"},
    }

    result = _with_scope_context(context, category_id="虚拟机-003")

    assert result is not None
    assert result["product"] == "HCI"
    assert result["version"] == "6.10.0_R2"
    assert result["components"] == ["存储", "虚拟机"]
    assert result["topology"] == ["external_storage"]


def test_scope_context_does_not_invent_scope_facts_without_cluster_or_confirmed_category():
    context = {"env_info": {"cluster_name": "only-a-name"}, "alert_logs": [], "task_logs": []}

    result = _with_scope_context(context, category_id=None)

    assert result == context


def test_scope_context_drops_invalid_configured_components_without_a_confirmed_domain():
    context = {
        "components": [None, "", "  ", "未知"],
        "env_info": {"cluster_name": "only-a-name"},
    }

    result = _with_scope_context(context, category_id="-003")

    assert result == {"env_info": {"cluster_name": "only-a-name"}}


def test_scope_context_discards_invalid_components_before_adding_confirmed_domain():
    context = {
        "components": [None, " ", "未知", "存储"],
        "env_info": {"hci_version": "6.11.1_R1"},
    }

    result = _with_scope_context(context, category_id="虚拟机-003")

    assert result is not None
    assert result["components"] == ["存储", "虚拟机"]


def test_scope_context_keeps_absent_context_absent():
    assert _with_scope_context(None, category_id="虚拟机-003") is None
