from types import SimpleNamespace

from app.services.kbd_revision_service import (
    KBD_PAYLOAD_FIELDS,
    build_kbd_revision_payload,
    derive_signal_review_facts,
    diff_revision_payloads,
    is_evaluation_candidate,
    payload_checksum,
    resolve_proposal_baseline,
    select_current_expert_pair,
    summarize_expert_signal_changes,
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


def test_expert_signal_summary_ignores_ai_generation_metadata_and_counts_signals_once():
    proposal = {
        "signals_json": {
            "generation_metadata": {"prompt_revision": "old"},
            "signals": [
                {"id": "sig_001", "acquire": {"tool": "qkv_task", "args": {"keyword": "启动"}}},
                {"id": "sig_002", "acquire": {"tool": "qfk_system", "args": {"command": "cat"}}},
            ],
        }
    }
    expert = {
        "signals_json": {
            "generation_metadata": {"prompt_revision": "new"},
            "signals": [
                {"id": "sig_001", "acquire": {"tool": "qkv_task", "args": {"keyword": "启动"}}},
                {
                    "id": "sig_002",
                    "acquire": {"tool": "qfk_system", "args": {"command": "cat", "timeout": 120}},
                    "review": {"notes": "专家确认"},
                },
                {"id": "sig_003", "acquire": {"tool": "qfk_system", "args": {"command": "ps"}}},
            ],
        }
    }

    summary = summarize_expert_signal_changes(
        proposal,
        expert,
        proposal_revision_id=41,
        expert_revision_id=42,
    )

    assert summary == {
        "status": "modified",
        "proposal_revision_id": 41,
        "expert_revision_id": 42,
        "changed_signal_count": 2,
        "added_signal_ids": ["sig_003"],
        "removed_signal_ids": [],
        "modified_signal_ids": ["sig_002"],
    }


def test_expert_signal_summary_is_zero_without_an_expert_draft():
    summary = summarize_expert_signal_changes(
        {"signals_json": {"signals": [{"id": "sig_001"}]}},
        None,
        proposal_revision_id=45,
        expert_revision_id=None,
    )

    assert summary["status"] == "no_expert_draft"
    assert summary["changed_signal_count"] == 0


def test_explicit_proposal_baseline_wins_over_history_order_and_parent_chain():
    old_proposal = SimpleNamespace(
        id=1,
        revision_type="proposal",
        parent_revision_id=None,
        baseline_proposal_revision_id=None,
    )
    current_proposal = SimpleNamespace(
        id=44,
        revision_type="proposal",
        parent_revision_id=1,
        baseline_proposal_revision_id=None,
    )
    expert = SimpleNamespace(
        id=45,
        revision_type="expert",
        parent_revision_id=1,
        baseline_proposal_revision_id=44,
    )

    assert resolve_proposal_baseline(
        expert,
        {1: old_proposal, 44: current_proposal, 45: expert},
    ) is current_proposal


def test_historical_expert_baseline_falls_back_to_parent_chain():
    proposal = SimpleNamespace(
        id=10,
        revision_type="proposal",
        parent_revision_id=None,
    )
    first_expert = SimpleNamespace(
        id=11,
        revision_type="expert",
        parent_revision_id=10,
    )
    latest_expert = SimpleNamespace(
        id=12,
        revision_type="expert",
        parent_revision_id=11,
    )

    assert resolve_proposal_baseline(
        latest_expert,
        {10: proposal, 11: first_expert, 12: latest_expert},
    ) is proposal


def test_only_approved_or_published_expert_revision_is_evaluation_candidate():
    working = SimpleNamespace(
        revision_type="expert",
        review_metadata={"review_state": "working"},
        generation_metadata={"origin": "admin_working_edit"},
    )
    approved = SimpleNamespace(
        revision_type="expert",
        review_metadata={"review_state": "approved"},
        generation_metadata={"origin": "admin_review"},
    )
    legacy_publish = SimpleNamespace(
        revision_type="expert",
        review_metadata={},
        generation_metadata={"origin": "admin_maintenance_publish"},
    )

    assert is_evaluation_candidate(working) is False
    assert is_evaluation_candidate(approved) is True
    assert is_evaluation_candidate(legacy_publish) is True


def test_current_expert_pair_uses_approved_revision_after_publish_but_not_after_reextract():
    proposal = SimpleNamespace(
        id=10,
        revision_no=10,
        revision_type="proposal",
        parent_revision_id=None,
        baseline_proposal_revision_id=None,
        review_metadata={},
        generation_metadata={},
    )
    approved = SimpleNamespace(
        id=11,
        revision_no=11,
        revision_type="expert",
        parent_revision_id=10,
        baseline_proposal_revision_id=10,
        review_metadata={"review_state": "approved"},
        generation_metadata={"origin": "admin_review"},
    )
    reextract = SimpleNamespace(
        id=12,
        revision_no=12,
        revision_type="proposal",
        parent_revision_id=10,
        baseline_proposal_revision_id=None,
        review_metadata={},
        generation_metadata={"origin": "signal_reextract"},
    )

    assert select_current_expert_pair(
        [proposal, approved],
        working_revision_id=None,
        latest_proposal_revision_id=10,
    ) == (approved, proposal)
    assert select_current_expert_pair(
        [proposal, approved, reextract],
        working_revision_id=None,
        latest_proposal_revision_id=12,
    ) == (None, None)


def _signal(signal_id: str, *, needs_review: bool = False, require_confirm: bool = False) -> dict:
    return {
        "id": signal_id,
        "acquire": {"tool": "qkv_task", "args": {"keyword": "启动失败"}},
        "provenance": {"needs_review": needs_review},
        "review": {"require_human_confirm": require_confirm},
    }


def _revision(
    revision_id: int,
    revision_type: str,
    signal: dict,
    *,
    parent_revision_id: int | None = None,
    baseline_proposal_revision_id: int | None = None,
    actor_type: str = "llm",
    review_state: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=revision_id,
        revision_no=revision_id,
        revision_type=revision_type,
        parent_revision_id=parent_revision_id,
        baseline_proposal_revision_id=baseline_proposal_revision_id,
        payload_json={"signals_json": {"schema_version": 2, "signals": [signal]}},
        actor_type=actor_type,
        review_metadata={"review_state": review_state} if review_state else {},
        generation_metadata={},
    )


def test_signal_review_facts_omit_signal_that_does_not_require_review():
    proposal_signal = _signal("sig-normal")
    proposal = _revision(1, "proposal", proposal_signal)

    facts = derive_signal_review_facts(
        {"signals": [proposal_signal]},
        [proposal],
        working_revision_id=None,
        latest_proposal_revision_id=1,
    )

    assert facts == {}


def test_signal_review_facts_mark_unreviewed_proposal_as_needing_review():
    proposal_signal = _signal("sig-review", needs_review=True)
    proposal = _revision(1, "proposal", proposal_signal)

    facts = derive_signal_review_facts(
        {"signals": [proposal_signal]},
        [proposal],
        working_revision_id=None,
        latest_proposal_revision_id=1,
    )

    assert facts == {"sig-review": {"status": "needs_review"}}


def test_signal_review_facts_turn_green_after_expert_working_save():
    proposal_signal = _signal("sig-review", require_confirm=True)
    proposal = _revision(1, "proposal", proposal_signal)
    working = _revision(
        2,
        "expert",
        proposal_signal,
        parent_revision_id=1,
        baseline_proposal_revision_id=1,
        actor_type="expert",
        review_state="working",
    )

    facts = derive_signal_review_facts(
        {"signals": [proposal_signal]},
        [proposal, working],
        working_revision_id=2,
        latest_proposal_revision_id=1,
    )

    assert facts == {"sig-review": {"status": "reviewed"}}


def test_signal_review_facts_do_not_treat_system_working_revision_as_expert_save():
    proposal_signal = _signal("sig-review", needs_review=True)
    proposal = _revision(1, "proposal", proposal_signal)
    system_working = _revision(
        2,
        "expert",
        proposal_signal,
        parent_revision_id=1,
        baseline_proposal_revision_id=1,
        actor_type="system",
        review_state="working",
    )

    facts = derive_signal_review_facts(
        {"signals": [proposal_signal]},
        [proposal, system_working],
        working_revision_id=2,
        latest_proposal_revision_id=1,
    )

    assert facts == {"sig-review": {"status": "needs_review"}}


def test_signal_review_facts_restore_original_requirement_after_publish_stamp():
    proposal_signal = _signal("sig-review", needs_review=True)
    published_signal = _signal("sig-review")
    proposal = _revision(1, "proposal", proposal_signal)
    approved = _revision(
        2,
        "expert",
        published_signal,
        parent_revision_id=1,
        baseline_proposal_revision_id=1,
        actor_type="expert",
        review_state="approved",
    )

    facts = derive_signal_review_facts(
        {"signals": [published_signal]},
        [proposal, approved],
        working_revision_id=None,
        latest_proposal_revision_id=1,
    )

    assert facts == {"sig-review": {"status": "reviewed"}}


def test_signal_review_facts_do_not_inherit_old_expert_save_after_reextract():
    old_signal = _signal("sig-review", needs_review=True)
    new_signal = _signal("sig-review", needs_review=True)
    new_signal["acquire"]["args"]["keyword"] = "新的启动失败"
    old_proposal = _revision(1, "proposal", old_signal)
    old_approved = _revision(
        2,
        "expert",
        old_signal,
        parent_revision_id=1,
        baseline_proposal_revision_id=1,
        actor_type="expert",
        review_state="approved",
    )
    new_proposal = _revision(3, "proposal", new_signal, parent_revision_id=1)

    facts = derive_signal_review_facts(
        {"signals": [new_signal]},
        [old_proposal, old_approved, new_proposal],
        working_revision_id=None,
        latest_proposal_revision_id=3,
    )

    assert facts == {"sig-review": {"status": "needs_review"}}
