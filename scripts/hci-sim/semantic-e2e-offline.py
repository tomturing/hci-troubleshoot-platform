#!/usr/bin/env python3
"""通过正式客户 API 验收独立诊断样例；不修改原 KBD 或主环境采集资源。"""

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
RESERVED_CONTEXT_KEYS = {"HOST", "VM_ID", "END", "semantic_context"}


def affected_objects(source_support_id: str) -> list[dict[str, str]]:
    """按样例真实对象范围构造工单，不用 VM 对象掩盖其它场景变量缺口。"""

    source_node = "SIM-HCI-NODE-01"
    normalized_id = source_support_id.replace("SAMPLE-V2-", "SAMPLE-SIG-")
    if normalized_id == "SAMPLE-SIG-VM":
        return [{"type": "vm", "id": "90010001", "source_node": source_node}]
    if normalized_id == "SAMPLE-SIG-LOG":
        return [{"type": "service", "id": "hci-api", "source_node": source_node}]
    if normalized_id == "SAMPLE-SIG-HW-PLT":
        return [{"type": "hardware", "id": "SIM-HARDWARE-01", "source_node": source_node}]
    # CORE 与 NET-STO 都是节点级联合检查，不能虚构某个 VM 为唯一故障对象。
    return [{"type": "node", "id": source_node, "source_node": source_node}]


def known_variables(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise SystemExit(f"known-variable 必须使用 NAME=VALUE：{raw}")
        name, value = raw.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value or name in result:
            raise SystemExit(f"known-variable 名称/值无效或重复：{name or raw}")
        if name in RESERVED_CONTEXT_KEYS:
            raise SystemExit(f"{name} 是会话保留变量，只能由已确认对象和时间生成")
        result[name] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--support-id", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--attempt", default="", help="重测时使用新后缀，保留失败工单证据")
    parser.add_argument("--known-variable", action="append", default=[], help="用户明确提供的 NAME=VALUE，可重复")
    parser.add_argument("--base-url", default="http://localhost:3003")
    parser.add_argument("--stage", choices=("prepare", "collect", "upload", "result", "verify"), required=True)
    args = parser.parse_args()
    if not all(
        value and "/" not in value and ".." not in value for value in (args.run_id, args.support_id, args.instance)
    ):
        raise SystemExit("验收标识不能包含目录路径")
    if "/" in args.attempt or ".." in args.attempt:
        raise SystemExit("attempt 不能包含目录路径")
    run_dir = ROOT / ".hci-sim-run/semantic-e2e" / args.run_id
    suite = json.loads((run_dir / "state.json").read_text())
    case = next(c for c in suite["cases"] if c["support_id"] == args.support_id)
    suffix = f"-{args.attempt}" if args.attempt else ""
    result_path = run_dir / f"offline-{args.support_id}{suffix}.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}

    def save():
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    with httpx.Client(base_url=args.base_url, timeout=180, follow_redirects=True) as client:

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if response.is_error:
                raise SystemExit(f"{method} {path}: {response.status_code} {response.text[:2500]}")
            return response.json()

        def post(path, body):
            return request("POST", path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})

        if args.stage == "prepare":
            if "case" not in result:
                result["case"] = post(
                    "/api/cases/",
                    {
                        "client_id": f"semantic-e2e-{uuid.uuid4()}",
                        "title": f"独立 Signal v2 离线验收 {case['source_support_id']}",
                        "description": case["description"],
                        "assistant_type": "htp-agent",
                    },
                )
                save()
            if "session" not in result:
                result["session"] = post(
                    "/api/diagnosis-sessions",
                    {
                        "case_id": result["case"]["case_id"],
                        "selected_scenario": case["category_id"],
                        "selected_category": case["category_id"],
                        "incident": {
                            "start_time": "2026-08-12T10:00:00Z",
                            "end_time": "2026-08-12T10:05:00Z",
                            "timezone": "Asia/Shanghai",
                        },
                        "affected_objects": affected_objects(case["source_support_id"]),
                        "impact_scope": "single_node",
                        "current_status": "ongoing",
                    },
                )
                save()
            prefix = f"/api/diagnosis-sessions/{result['session']['session_id']}"
            if "plan" not in result:
                result["plan"] = post(
                    prefix + "/collection-plans",
                    {
                        "product_version": "6.12.0",
                        "context": {
                            **known_variables(args.known_variable),
                            "semantic_context": {
                                "description": case["description"],
                                "product": "HCI",
                                "product_version": "6.12.0",
                            },
                        },
                    },
                )
                save()
            if "artifact" not in result:
                result["artifact"] = post(
                    prefix + "/collector-artifacts",
                    {
                        "collection_plan_id": result["plan"]["collection_plan_id"],
                        "target_node": "SIM-HCI-NODE-01",
                    },
                )
                save()
            bundle = client.get(result["artifact"]["verification_bundle_path"])
            bundle.raise_for_status()
            bundle_path = run_dir / f"verification-{args.support_id}{suffix}.zip"
            bundle_path.write_bytes(bundle.content)
            result["verification_bundle"] = str(bundle_path)
            save()
            commands = [i["rendered_command"] for i in result["artifact"]["items"]]
            if not case["source_support_id"].endswith("-VM"):
                assert all("90010001" not in command for command in commands), "非 VM 样例泄漏了 VM 对象标识"
            print(
                json.dumps(
                    {
                        "stage": "prepared",
                        "case_id": result["case"]["case_id"],
                        "session_id": result["session"]["session_id"],
                        "commands": commands,
                    },
                    ensure_ascii=False,
                )
            )
            return

        if args.stage == "collect":
            env = dict(os.environ)
            env.pop("GOROOT", None)
            env["HCI_SIM_SCENARIO_PROFILE"] = str(run_dir / "scenario-profile.json")
            completed = subprocess.run(
                [
                    str(ROOT / ".venv/bin/python"),
                    str(ROOT / "scripts/hci-sim/diagnosis-lab.py"),
                    "offline-run",
                    "--instance",
                    args.instance,
                    "--bundle",
                    result["verification_bundle"],
                    "--fingerprint",
                    result["artifact"]["public_key_fingerprint"],
                ],
                env=env,
            )
            if completed.returncode:
                raise SystemExit(completed.returncode)
            files = list((ROOT / ".hci-sim-run/lab" / args.instance / "offline-output").glob("*.hci-eb"))
            assert files, "未生成加密证据包"
            result["evidence_path"] = str(max(files, key=lambda item: item.stat().st_mtime_ns))
            save()
            return

        prefix = f"/api/diagnosis-sessions/{result['session']['session_id']}"
        if args.stage == "upload" and "bundle" not in result:
            evidence_path = Path(result["evidence_path"])
            data = evidence_path.read_bytes()
            upload = post(
                prefix + "/uploads",
                {
                    "bundle_type": "initial",
                    "collection_plan_id": result["plan"]["collection_plan_id"],
                    "collector_artifact_id": result["artifact"]["artifact_id"],
                    "file_name": evidence_path.name,
                    "media_type": "application/vnd.hci.evidence",
                    "total_size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
            for target in upload["upload_targets"]:
                number = target["part_number"]
                chunk = data[(number - 1) * upload["chunk_size_bytes"] : number * upload["chunk_size_bytes"]]
                request("PUT", target["upload_url"], content=chunk, headers={"X-Upload-Token": upload["upload_token"]})
            result["bundle"] = post(
                prefix + f"/uploads/{upload['upload_id']}/complete",
                {"parts": [t["part_number"] for t in upload["upload_targets"]]},
            )
            save()
        attempts = 90 if args.stage == "verify" else 10
        for _ in range(attempts):
            result["bundles"] = request("GET", prefix + "/bundles")
            result["runs"] = request("GET", prefix + "/runs")
            save()
            if result["runs"] and (
                args.stage != "verify" or result["runs"][-1]["status"] in {"completed", "failed", "cancelled"}
            ):
                break
            if args.stage == "result":
                break
            time.sleep(2)
        if result["runs"]:
            result["candidates"] = request("GET", prefix + f"/runs/{result['runs'][-1]['run_id']}/candidates")
            result["signals"] = request("GET", prefix + f"/runs/{result['runs'][-1]['run_id']}/signals")
            save()
        if args.stage == "verify":
            assert result.get("runs") and result["runs"][-1]["status"] == "completed", "诊断尚未完成"
            assert len(result["candidates"]) == 1 and result["candidates"][0]["support_id"] == args.support_id
            snapshot = result["candidates"][0]["kbd_snapshot"]
            entry_mode = case.get("entry_mode") or (
                "semantic_fallback" if case["source_support_id"].startswith("SAMPLE-V2-") else "strong_signal"
            )
            if entry_mode == "semantic_fallback":
                assert snapshot["semantic_route"]["decision"] == "executable"
            else:
                assert not snapshot.get("semantic_route"), "强信号样例不应绕入语义兜底"
            expected = {}
            for signal in snapshot["signals_json"]["signals"]:
                tool = signal["acquire"]["tool"]
                phase = (signal.get("orchestrate") or {}).get("phase", "diagnostic")
                if tool == "qkv_case_context" or phase != "diagnostic":
                    continue
                state = "MATCHED"
                if signal.get("role") == "exclude":
                    state = "NOT_MATCHED"
                expected[f"kbd:{args.support_id}:{signal['id']}"] = state
            actual = {signal["signal_id"]: signal["state"] for signal in result["signals"]}
            assert actual == expected, {"expected": expected, "actual": actual}
            result["verification"] = {
                "status": "PASS",
                "signal_count": len(actual),
                "expected_signal_count": len(expected),
                "entry_mode": entry_mode,
                "source_support_id": case["source_support_id"],
                "case_id": result["case"]["case_id"],
            }
            save()
            print(json.dumps(result["verification"]))
            return
        print(
            json.dumps(
                {
                    "support_id": args.support_id,
                    "bundles": [
                        {k: b.get(k) for k in ("bundle_id", "processing_status", "failure_code", "failure_message")}
                        for b in result["bundles"]
                    ],
                    "runs": [{k: r.get(k) for k in ("run_id", "status")} for r in result["runs"]],
                    "candidates": result.get("candidates", []),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
