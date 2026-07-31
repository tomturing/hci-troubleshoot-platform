"""最小 KBD Replay artifact manifest。

manifest 只把一次诊断重新定位到不可变 KBD revision、确定性编译计划和
Terminal Bridge artifact 的查找键；不复制命令、变量、stdout/stderr 或日志正文。
它不是执行重放器：artifact 可否读取、是否已脱敏、以及能否在相同环境重跑，必须
由后续 Evidence / Execution Replay 服务明确确认，不能从一次审计记录中臆断。
"""

from __future__ import annotations

from typing import Any

from shared.dynamic_resource.serialization import sha256_json

REPLAY_MANIFEST_SCHEMA_VERSION = 1
TERMINAL_ARTIFACT_STORE = "conversation-service.bridge_execution_artifacts"


def build_kbd_replay_manifest(
    *,
    resource: dict[str, Any],
    plan_id: str,
    snapshot_id: str,
    environment: dict[str, Any],
    signal_outcomes: list[dict[str, Any]],
    steps_by_signal: dict[tuple[str, str], Any],
    kbd_id: str,
) -> dict[str, Any]:
    """构造不含敏感正文的 replay manifest。

    ``exec_id`` 是 Terminal Bridge artifact 的稳定查找键。只有在本次诊断确有
    原始输出时才写入该引用；BLOCKED、编译失败等未执行场景不得伪造 artifact。
    该 manifest 所有可变输入只存 SHA-256，具体证据仍由 artifact 存储和其权限策略
    管理。
    """

    evaluations: list[dict[str, Any]] = []
    missing_evidence_refs: list[str] = []
    for row in signal_outcomes:
        signal_id = str(row.get("signal_id") or "")
        step = steps_by_signal.get((kbd_id, signal_id))
        attempted = step is not None and step.raw_output is not None
        artifact = None
        if attempted and step.exec_id:
            artifact = {
                "store": TERMINAL_ARTIFACT_STORE,
                "lookup": {"exec_id": step.exec_id},
                "availability": "unverified",
            }
        elif attempted:
            missing_evidence_refs.append(signal_id or str(row.get("signal_ref_id") or "unknown"))

        evaluations.append(
            {
                "signal_ref_id": row.get("signal_ref_id"),
                "signal_id": signal_id,
                "evaluation_id": row.get("evaluation_id"),
                "acquisition_id": getattr(step, "acquisition_id", None) if step is not None else None,
                "tool": row.get("tool"),
                "outcome": row.get("outcome"),
                "acquire_args_hash": sha256_json(getattr(step, "tool_args", {})) if step is not None else None,
                "artifact": artifact,
            }
        )

    artifact_refs = [item for item in evaluations if item["artifact"] is not None]
    evidence_status = "referenced" if artifact_refs and not missing_evidence_refs else "partial" if artifact_refs else "absent"
    manifest = {
        "schema_version": REPLAY_MANIFEST_SCHEMA_VERSION,
        "kind": "kbd_execution_replay_manifest",
        "resource": {
            "resource_type": resource.get("resource_type"),
            "resource_name": resource.get("resource_name"),
            "revision": resource.get("revision"),
            "checksum": resource.get("checksum"),
        },
        "plan": {"plan_id": plan_id, "snapshot_id": snapshot_id},
        "environment_hash": sha256_json(environment),
        "evaluations": evaluations,
        "readiness": {
            "evidence": evidence_status,
            "execution": "not_available",
            "replayable": False,
            "blockers": [
                "ARTIFACT_READ_AUTHORIZATION_NOT_CONFIRMED",
                "EXECUTION_REPLAY_RUNNER_NOT_IMPLEMENTED",
            ]
            + (["MISSING_ARTIFACT_REFERENCE"] if missing_evidence_refs else []),
        },
    }
    # 指纹用于以后取得 artifact 后验证“被重放的正是这一次输入/计划/判定”。
    manifest["manifest_id"] = sha256_json(manifest)
    return manifest
