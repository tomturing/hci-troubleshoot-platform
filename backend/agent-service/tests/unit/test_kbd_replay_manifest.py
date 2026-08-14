"""KBD 最小 Replay manifest 契约测试。"""

from types import SimpleNamespace

from shared.cdd.replay_manifest import build_kbd_replay_manifest


def test_replay_manifest_only_references_terminal_artifact_and_never_embeds_output():
    step = SimpleNamespace(
        raw_output="sensitive log output must not be persisted here",
        exec_id="exec-001",
        acquisition_id="acq-001",
        tool_args={"host": "10.0.0.10", "keyword": "sensitive"},
    )
    manifest = build_kbd_replay_manifest(
        resource={"resource_type": "kbd", "resource_name": "30880", "revision": 17, "checksum": "a" * 64},
        plan_id="plan-001",
        snapshot_id="snapshot-001",
        environment={"node_ip": "10.0.0.10", "token": "must-not-appear"},
        signal_outcomes=[
            {
                "signal_ref_id": "30880:sig_001",
                "signal_id": "sig_001",
                "evaluation_id": "eval-001",
                "tool": "qfk_log",
                "outcome": "SATISFIED",
            }
        ],
        steps_by_signal={("30880", "sig_001"): step},
        kbd_id="30880",
    )

    evaluation = manifest["evaluations"][0]
    assert evaluation["artifact"] == {
        "store": "conversation-service.bridge_execution_artifacts",
        "lookup": {"exec_id": "exec-001"},
        "availability": "unverified",
    }
    assert evaluation["acquire_args_hash"]
    assert manifest["environment_hash"]
    assert manifest["readiness"]["evidence"] == "referenced"
    assert manifest["readiness"]["replayable"] is False
    assert "sensitive log output" not in str(manifest)
    assert "must-not-appear" not in str(manifest)
