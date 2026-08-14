#!/usr/bin/env python3
"""可按需启停的在线/离线诊断样例实验室控制器。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = ROOT / ".hci-sim-run" / "lab"
STATE_ROOT = ROOT / ".hci-sim-state" / "lab"
PROFILE = ROOT / "hci_sim" / "testdata" / "sample-suites" / "diagnosis-signal-matrix-v1.json"
IMAGE = os.getenv("HCI_SIM_IMAGE", "hci-sim:diagnosis-lab")
CAPABILITIES_URL = os.getenv("HCI_SIM_CAPABILITIES_URL", "http://127.0.0.1:18004/api/kb/hci-sim/capabilities")
INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "hci-dev-internal-token")
VALID_VARIANTS = {
    "positive",
    "negative",
    "missing-evidence",
    "command-failed",
    "timeout",
    "version-incompatible",
}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


def run(command: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"无法读取 KBD Capability（能力快照）{url}：{exc}") from exc


def profile() -> dict[str, Any]:
    document = json.loads(PROFILE.read_text(encoding="utf-8"))
    if document.get("schema_version") != "1.0" or not isinstance(document.get("cases"), dict):
        raise SystemExit(f"场景画像无效：{PROFILE}")
    return document


def suite_ids() -> list[str]:
    return sorted(profile()["cases"])


def validate_name(name: str, label: str) -> str:
    if not SAFE_NAME.fullmatch(name):
        raise SystemExit(f"{label} 只允许字母、数字、点、下划线和连字符")
    return name


def default_instance(scenario: str) -> str:
    return scenario.lower()


def instance_dir(instance: str) -> Path:
    return RUN_ROOT / validate_name(instance, "INSTANCE")


def state_file(instance: str) -> Path:
    return instance_dir(instance) / "state.json"


def read_state(instance: str) -> dict[str, Any]:
    path = state_file(instance)
    if not path.is_file():
        raise SystemExit(f"实验室实例不存在：{instance}")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def append_audit(run_dir: Path, event: str, **fields: Any) -> None:
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    with (run_dir / "audit.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def docker_exists(container: str) -> bool:
    result = run(["docker", "inspect", container], capture=True, check=False)
    return result.returncode == 0


def docker_running(container: str) -> bool:
    result = run(["docker", "inspect", "--format", "{{.State.Running}}", container], capture=True, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def free_port(start: int, end: int) -> int:
    for port in range(start, end + 1):
        with socket.socket() as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"没有可用端口：{start}-{end}")


def build_image() -> None:
    digest = hashlib.sha256()
    source_root = ROOT / "hci_sim"
    for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(source_root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    source_digest = digest.hexdigest()
    inspected = run(
        ["docker", "image", "inspect", "--format", "{{ index .Config.Labels \"com.hci.diagnosis-lab.source-sha256\" }}", IMAGE],
        capture=True,
        check=False,
    )
    if inspected.returncode == 0 and inspected.stdout.strip() == source_digest:
        return
    run([
        "docker", "build", "--quiet", "--label", f"com.hci.diagnosis-lab.source-sha256={source_digest}",
        "-t", IMAGE, "-f", str(ROOT / "hci_sim" / "Dockerfile"), str(ROOT),
    ])


def container_capabilities_url() -> str:
    """将宿主机回环地址转换为 Docker 可访问地址，Linux/Mac 均可工作。"""

    parsed = urllib.parse.urlsplit(CAPABILITIES_URL)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return CAPABILITIES_URL
    host = "host.docker.internal"
    if parsed.port:
        host += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def capability(scenario: str) -> dict[str, Any]:
    return request_json(f"{CAPABILITIES_URL.rstrip('/')}/{urllib.parse.quote(scenario)}")


def check_scenario(scenario: str) -> dict[str, Any]:
    expected = profile()["cases"].get(scenario)
    if expected is None:
        raise SystemExit(f"场景画像不存在：{scenario}")
    result = capability(scenario)
    gaps = result.get("capability_gaps") or []
    resolved = result.get("resolved") or {}
    if result.get("status") != "ready_for_artifact_binding":
        message = "；".join(f"{item.get('code')}: {item.get('message')}" for item in gaps)
        raise SystemExit(f"场景尚不可启动：{message or result.get('status')}")
    metadata = resolved.get("metadata") or {}
    if metadata.get("sample_suite") != "diagnosis-signal-matrix-v1":
        raise SystemExit("KBD 不属于 diagnosis-signal-matrix-v1 样例集")
    signal_ids = {item["signal_id"] for item in resolved.get("synthetic_routes") or []}
    missing = set(expected["signals"]) - signal_ids
    extra = signal_ids - set(expected["signals"])
    if missing or extra:
        raise SystemExit(f"场景画像与已发布 KBD 漂移：missing={sorted(missing)} extra={sorted(extra)}")
    return result


def ensure_host_key(run_dir: Path) -> Path:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    stable = STATE_ROOT / "ssh_host_key"
    if not stable.is_file():
        run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(stable)])
        os.chmod(stable, 0o600)
    target = run_dir / "ssh_host_key"
    shutil.copyfile(stable, target)
    os.chmod(target, 0o600)
    return target


def scenario_card(run_dir: Path, state: dict[str, Any], connection: dict[str, Any]) -> None:
    card = profile()["cases"][state["scenario"]]
    commands = "\n".join(f"- `{command}`" for command in connection.get("recommended_commands") or [])
    text = f"""# Diagnosis Sample Lab（诊断样例实验室）场景卡

- 场景：`{state['scenario']}`
- 实例：`{state['instance']}`
- 变体：`{state['variant']}`
- 产品版本：`{card['product_version']}`
- KBD Revision（修订）：`{state['kbd_revision']}`
- Bundle Digest（制品摘要）：`{state['bundle_digest']}`
- Lab Run ID（实验室运行标识）：`{state['lab_run_id']}`

## 工单故障描述

{card['fault_description']}

## 预期结论

{card['expected_conclusion']}

## 在线诊断

在 Customer UI 选择“仿真租约”，导入同目录下的 `connection.json`。

{commands}

## 离线诊断

在 Customer UI 创建无 SSH 工单，生成并下载 Verification Bundle（验证包），然后执行：

```bash
make diagnosis-lab-offline-run SCENARIO={state['scenario']} INSTANCE={state['instance']} BUNDLE=/absolute/bundle.zip FINGERPRINT=<可信根指纹>
```
"""
    (run_dir / "scenario-card.md").write_text(text, encoding="utf-8")


def up(args: argparse.Namespace) -> None:
    scenario = validate_name(args.scenario, "SCENARIO")
    instance = validate_name(args.instance or default_instance(scenario), "INSTANCE")
    if args.variant not in VALID_VARIANTS:
        raise SystemExit(f"VARIANT 必须是 {sorted(VALID_VARIANTS)}")
    resolved = check_scenario(scenario)["resolved"]
    run_dir = instance_dir(instance)
    container = f"hci-diagnosis-lab-{instance}"
    if docker_exists(container):
        raise SystemExit(f"实例容器已存在：{container}；请使用 status、renew、reset 或 down")
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(f"实例目录已存在且非空：{run_dir}；请显式 reset")
    run_dir.mkdir(parents=True, mode=0o700)
    (run_dir / "logs").mkdir(mode=0o700)
    (run_dir / "offline-inbox").mkdir(mode=0o700)
    (run_dir / "offline-output").mkdir(mode=0o700)
    build_image()
    ensure_host_key(run_dir)
    ssh_port = free_port(22000, 22999)
    http_port = free_port(18081, 18999)
    lab_run_id = f"lab-{instance}-{int(time.time())}"
    lease_key = secrets.token_hex(32)
    compiler_url = container_capabilities_url()
    bootstrap = run(
        [
            "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "--add-host", "host.docker.internal:host-gateway",
            "-e", f"HCI_SIM_CAPABILITIES_URL={compiler_url}", "-e", f"INTERNAL_API_TOKEN={INTERNAL_TOKEN}",
            "-e", f"HCI_SIM_LEASE_HMAC_KEY={lease_key}", "-v", f"{run_dir}:/run/hci-sim",
            "-v", f"{PROFILE}:/etc/hci-sim/scenario-profile.json:ro", IMAGE, "bootstrap",
            "--kbd-id", scenario, "--capabilities-url", compiler_url, "--api-token", INTERNAL_TOKEN,
            "--lease-key", lease_key, "--output-dir", "/run/hci-sim", "--connection-host", args.host,
            "--connection-port", str(ssh_port), "--ttl", args.ttl, "--variant", args.variant,
            "--scenario-profile", "/etc/hci-sim/scenario-profile.json",
        ],
        capture=True,
    )
    (run_dir / "bootstrap.json").write_text(bootstrap.stdout, encoding="utf-8")
    connection = json.loads((run_dir / "connection.json").read_text(encoding="utf-8"))
    run(
        [
            "docker", "run", "-d", "--name", container,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-p", f"127.0.0.1:{ssh_port}:2222", "-p", f"127.0.0.1:{http_port}:8080",
            "--label", "com.hci.diagnosis-lab=true", "--label", f"com.hci.diagnosis-lab.instance={instance}",
            "-e", "HCI_SIM_FIXTURE_MANIFEST=/run/hci-sim/fixture-manifest.json",
            "-e", "HCI_SIM_HOST_KEY_FILE=/run/hci-sim/ssh_host_key", "-e", "HCI_SIM_SSH_LISTEN=:2222",
            "-e", "HCI_SIM_HTTP_LISTEN=:8080", "-e", f"HCI_SIM_LEASE_HMAC_KEY={lease_key}",
            "-e", f"HCI_SIM_FIXTURE_VARIANT={args.variant}", "-e", f"HCI_SIM_LAB_RUN_ID={lab_run_id}",
            "-v", f"{run_dir}:/run/hci-sim:ro", IMAGE,
        ],
        capture=True,
    )
    ready = False
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/readyz", timeout=1):
                ready = True
                break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    if not ready:
        capture_container_logs(run_dir, container)
        run(["docker", "stop", container], check=False, capture=True)
        run(["docker", "rm", container], check=False, capture=True)
        raise SystemExit("hci-sim 场景容器未就绪")
    state = {
        "schema_version": "1.0", "instance": instance, "scenario": scenario, "variant": args.variant,
        "lab_run_id": lab_run_id, "container": container, "status": "running", "ssh_port": ssh_port,
        "http_port": http_port, "created_at": datetime.now(UTC).isoformat(), "kbd_revision": resolved["kbd_revision"],
        "kbd_checksum": resolved["kbd_checksum"], "tool_contract_revision": resolved["tool_contract_revision"],
        "bundle_digest": connection["bundle_digest"], "connection_file": str(run_dir / "connection.json"),
    }
    atomic_json(state_file(instance), state)
    scenario_card(run_dir, state, connection)
    append_audit(run_dir, "lab.started", **state)
    print(json.dumps({**state, "scenario_card": str(run_dir / "scenario-card.md")}, ensure_ascii=False, indent=2))


def sync_resources(args: argparse.Namespace) -> None:
    """显式触发并发布 KBD/Tool → Collector/Profile/Mapping 同步批次。"""

    base_url = args.diagnosis_url.rstrip("/") + "/api/internal/offline-resource-sync"
    headers = {
        "Authorization": f"Bearer {INTERNAL_TOKEN}",
        "Content-Type": "application/json",
        "X-Tenant-ID": args.tenant,
        "X-Actor-ID": args.actor,
    }

    def post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"离线资源同步失败 HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc

    candidate = post(base_url + "/preview", {"mode": args.mode})
    blocking = [item for item in candidate.get("validation_json") or [] if item.get("severity") == "error"]
    if blocking:
        raise SystemExit("离线资源同步存在阻断：" + json.dumps(blocking, ensure_ascii=False))
    published = post(
        f"{base_url}/{candidate['batch_id']}/publish",
        {"reason": "Diagnosis Sample Lab（诊断样例实验室）显式同步"},
    )
    print(json.dumps(published, ensure_ascii=False, indent=2))


def list_scenarios(_: argparse.Namespace) -> None:
    document = profile()
    try:
        remote = request_json(f"{CAPABILITIES_URL}?sample_suite=diagnosis-signal-matrix-v1")
        by_id = {item["support_id"]: item for item in remote.get("results") or []}
    except SystemExit:
        by_id = {}
    rows = []
    for scenario, card in sorted(document["cases"].items()):
        result = by_id.get(scenario) or {}
        rows.append({
            "scenario": scenario, "title": card["title"], "product_version": card["product_version"],
            "readiness": result.get("status", "platform-unavailable"),
            "gap_codes": [item.get("code") for item in result.get("capability_gaps") or []],
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def capture_container_logs(run_dir: Path, container: str) -> None:
    result = run(["docker", "logs", "--timestamps", container], capture=True, check=False)
    log_path = run_dir / "logs" / "runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")


def check(args: argparse.Namespace) -> None:
    results = []
    failed = False
    for scenario in [args.scenario] if args.scenario else suite_ids():
        try:
            resolution = check_scenario(scenario)
            resolved = resolution["resolved"]
            results.append({
                "scenario": scenario,
                "status": "ready",
                "kbd_revision": resolved["kbd_revision"],
                "route_count": len(resolved.get("synthetic_routes") or []),
            })
        except SystemExit as exc:
            failed = True
            results.append({"scenario": scenario, "status": "blocked", "reason": str(exc)})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit(1)


def status(args: argparse.Namespace) -> None:
    instances = [args.instance] if args.instance else [path.name for path in RUN_ROOT.iterdir()] if RUN_ROOT.exists() else []
    rows = []
    for instance in sorted(filter(None, instances)):
        try:
            state = read_state(instance)
        except SystemExit:
            continue
        state["container_running"] = docker_running(state["container"])
        rows.append(state)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def down(args: argparse.Namespace) -> None:
    state = read_state(args.instance)
    if docker_exists(state["container"]):
        run(["docker", "stop", state["container"]], check=False, capture=True)
        capture_container_logs(instance_dir(args.instance), state["container"])
        run(["docker", "rm", state["container"]], check=False, capture=True)
    state["status"] = "stopped"
    state["stopped_at"] = datetime.now(UTC).isoformat()
    atomic_json(state_file(args.instance), state)
    append_audit(instance_dir(args.instance), "lab.stopped", lab_run_id=state["lab_run_id"], instance=args.instance)
    print(f"已停止：{args.instance}；运行目录和审计记录保留在 {instance_dir(args.instance)}")


def reset(args: argparse.Namespace) -> None:
    run_dir = instance_dir(args.instance)
    if state_file(args.instance).is_file():
        state = read_state(args.instance)
        if docker_exists(state["container"]):
            run(["docker", "stop", state["container"]], check=False, capture=True)
            capture_container_logs(run_dir, state["container"])
            run(["docker", "rm", state["container"]], check=False, capture=True)
    if run_dir.exists():
        archive = RUN_ROOT / "archive" / f"{args.instance}-{int(time.time())}"
        archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.move(str(run_dir), str(archive))
        print(f"实例已复位；旧运行目录归档到 {archive}")
    else:
        print(f"实例不存在，无需复位：{args.instance}")


def renew(args: argparse.Namespace) -> None:
    state = read_state(args.instance)
    if not docker_running(state["container"]):
        raise SystemExit("实例容器未运行，不能续签")
    # 租约签名密钥由运行中 Runtime 持有，不能热替换。续签采用原地重建同一不可变 Bundle。
    scenario, variant = state["scenario"], state["variant"]
    down(argparse.Namespace(instance=args.instance))
    archived = RUN_ROOT / "archive" / f"{args.instance}-renew-{int(time.time())}"
    archived.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.move(str(instance_dir(args.instance)), str(archived))
    up(argparse.Namespace(scenario=scenario, instance=args.instance, variant=variant, host=args.host, ttl=args.ttl))


def connection(args: argparse.Namespace) -> None:
    state = read_state(args.instance)
    print(state["connection_file"])


def online_smoke(args: argparse.Namespace) -> None:
    state = read_state(args.instance)
    env = os.environ.copy()
    env.update(
        HCI_SIM_CONNECTION_JSON=state["connection_file"],
        HCI_SIM_BRIDGE_URL=args.bridge_url,
        HCI_SIM_BRIDGE_ORIGIN=args.bridge_origin,
    )
    result = subprocess.run(["go", "run", "./cmd/hci-sim-smoke"], cwd=ROOT / "hci_sim", env=env, text=True)
    append_audit(instance_dir(args.instance), "online.smoke", lab_run_id=state["lab_run_id"], exit_code=result.returncode)
    raise SystemExit(result.returncode)


def extract_bundle(bundle: Path, destination: Path) -> None:
    with zipfile.ZipFile(bundle) as archive:
        for item in archive.infolist():
            target = (destination / item.filename).resolve()
            if destination.resolve() not in target.parents:
                raise SystemExit("Verification Bundle 含越界路径")
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SystemExit("Verification Bundle 禁止包含符号链接")
            archive.extract(item, destination)
            if mode:
                os.chmod(target, stat.S_IMODE(mode))


def offline_run(args: argparse.Namespace) -> None:
    state = read_state(args.instance)
    bundle = Path(args.bundle).resolve()
    if not bundle.is_file():
        raise SystemExit(f"Verification Bundle 不存在：{bundle}")
    if not re.fullmatch(r"[0-9a-f]{64}", args.fingerprint):
        raise SystemExit("FINGERPRINT 必须是 64 位小写十六进制 SHA-256")
    run_dir = instance_dir(args.instance)
    offline_dir = run_dir / "offline-inbox" / f"run-{int(time.time())}"
    bundle_dir = offline_dir / "bundle"
    output_dir = offline_dir / "plaintext"
    bundle_dir.mkdir(parents=True, mode=0o700)
    output_dir.mkdir(mode=0o700)
    extract_bundle(bundle, bundle_dir)
    build_image()
    offline_manifest = offline_dir / "offline-fixture-manifest.json"
    run([
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{bundle}:/input/bundle.zip:ro", "-v", f"{run_dir}:/run/hci-sim", IMAGE,
        "offline-manifest", "--verification-bundle", "/input/bundle.zip", "--online-manifest",
        "/run/hci-sim/fixture-manifest.json", "--output", f"/run/hci-sim/{offline_manifest.relative_to(run_dir)}",
        "--variant", state["variant"],
    ])
    output_package = run_dir / "offline-output" / f"{args.instance}-{int(time.time())}.hci-eb"
    result = run([
        "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", f"HCI_SIM_FIXTURE_VARIANT={state['variant']}", "-e", f"HCI_SIM_LAB_RUN_ID={state['lab_run_id']}",
        "-e", "HCI_SIM_FIXTURE_MANIFEST=/scenario/offline-fixture-manifest.json",
        "-e", "HCI_SIM_AUDIT_FILE=/output/local-exec-audit.jsonl",
        "-v", f"{offline_dir}:/scenario:ro", "-v", f"{bundle_dir}:/bundle:ro", "-v", f"{output_dir}:/plaintext",
        "-v", f"{output_package.parent}:/output", "--entrypoint", "/bundle/hci-collect-linux-amd64", IMAGE,
        "--expected-root-fingerprint", args.fingerprint, "--bundle-dir", "/bundle", "--output-dir", "/plaintext",
        "--output", f"/output/{output_package.name}", "--yes",
    ], check=False)
    append_audit(
        run_dir, "offline.collection", lab_run_id=state["lab_run_id"], bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        evidence_path=str(output_package), exit_code=result.returncode,
    )
    if result.returncode:
        raise SystemExit(result.returncode)
    print(output_package)


def contract_smoke(_: argparse.Namespace) -> None:
    """CI 无需 Docker/运行中平台即可验证五场景画像与 KBD Signal 全量对齐。"""

    document = profile()
    seed = (ROOT / "database" / "seeds" / "04_kbd_diagnosis_samples.sql").read_text(encoding="utf-8")
    signal_documents = [
        json.loads(payload)
        for payload in re.findall(r"\$signals\$\s*(\{.*?\})\s*\$signals\$::jsonb", seed, re.DOTALL)
    ]
    by_id = {item["verification_contract"]["case_id"]: item for item in signal_documents}
    if set(by_id) != set(document["cases"]):
        raise SystemExit(f"五篇 KBD 与场景画像不一致：kbd={sorted(by_id)} profile={sorted(document['cases'])}")
    for support_id, signals_document in by_id.items():
        expected = {item["id"] for item in signals_document["signals"]}
        actual = set(document["cases"][support_id]["signals"])
        if expected != actual:
            raise SystemExit(f"{support_id} Signal 漂移：kbd={sorted(expected)} profile={sorted(actual)}")
        variables = {**document.get("variables", {}), **document["cases"][support_id].get("variables", {})}
        required = {
            variable
            for signal in signals_document["signals"]
            for variable in re.findall(r"\{\{([A-Z][A-Z0-9_]*)\}\}", json.dumps(signal, ensure_ascii=False))
        }
        missing = required - set(variables)
        if missing:
            raise SystemExit(f"{support_id} 缺少场景变量：{sorted(missing)}")
        for signal_id, outputs in document["cases"][support_id]["signals"].items():
            if not isinstance(outputs.get("positive_output"), str) or not isinstance(outputs.get("negative_output"), str):
                raise SystemExit(f"{support_id}/{signal_id} 缺少正/负场景输出")
    print(f"PASS sample_suite={document['sample_suite']} cases={len(by_id)} variants={len(VALID_VARIANTS)}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("list").set_defaults(handler=list_scenarios)
    commands.add_parser("contract-smoke").set_defaults(handler=contract_smoke)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--scenario")
    check_parser.set_defaults(handler=check)
    up_parser = commands.add_parser("up")
    up_parser.add_argument("--scenario", required=True)
    up_parser.add_argument("--instance")
    up_parser.add_argument("--variant", default="positive")
    up_parser.add_argument("--host", default=os.getenv("HCI_SIM_CONNECTION_HOST", "127.0.0.1"))
    up_parser.add_argument("--ttl", default="2h")
    up_parser.set_defaults(handler=up)
    sync = commands.add_parser("sync-resources")
    sync.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    sync.add_argument("--diagnosis-url", default=os.getenv("DIAGNOSIS_SERVICE_URL", "http://127.0.0.1:18008"))
    sync.add_argument("--tenant", default="diagnosis-lab")
    sync.add_argument("--actor", default="diagnosis-lab-admin")
    sync.set_defaults(handler=sync_resources)
    for name, handler in (("status", status), ("down", down), ("reset", reset), ("connection", connection)):
        command = commands.add_parser(name)
        command.add_argument("--instance", required=name != "status")
        command.set_defaults(handler=handler)
    renew_parser = commands.add_parser("renew")
    renew_parser.add_argument("--instance", required=True)
    renew_parser.add_argument("--host", default=os.getenv("HCI_SIM_CONNECTION_HOST", "127.0.0.1"))
    renew_parser.add_argument("--ttl", default="2h")
    renew_parser.set_defaults(handler=renew)
    online = commands.add_parser("online-smoke")
    online.add_argument("--instance", required=True)
    online.add_argument("--bridge-url", default=os.getenv("HCI_SIM_BRIDGE_URL", "ws://127.0.0.1:9999"))
    online.add_argument("--bridge-origin", default=os.getenv("HCI_SIM_BRIDGE_ORIGIN", "http://127.0.0.1"))
    online.set_defaults(handler=online_smoke)
    offline = commands.add_parser("offline-run")
    offline.add_argument("--instance", required=True)
    offline.add_argument("--bundle", required=True)
    offline.add_argument("--fingerprint", required=True)
    offline.set_defaults(handler=offline_run)
    return result


def main() -> None:
    arguments = parser().parse_args()
    arguments.handler(arguments)


if __name__ == "__main__":
    main()
