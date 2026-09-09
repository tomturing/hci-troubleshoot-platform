#!/usr/bin/env python3
"""启停可重复使用的 Signal v2 完整在线 Agent 仿真栈。"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NETWORK = "hci-troubleshoot-platform_default"
POSTGRES_CONTAINER = "semantic-e2e-hci-sim-postgres"
POSTGRES_VOLUME = "semantic-e2e-hci-sim-postgres-data"
INSTANCE = "semantic-online"
LABEL = "com.hci.semantic-online-e2e=true"
STATE_PATH = ROOT / ".hci-sim-state" / "semantic-online-e2e.json"


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, check=True, text=True, **kwargs)


def inspect(name: str) -> dict | None:
    result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def write_state(value: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_PATH)


def read_state() -> dict:
    if not STATE_PATH.is_file():
        raise SystemExit("完整在线仿真尚未初始化")
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def wait_http(url: str, label: str, attempts: int = 90) -> None:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(1)
    raise SystemExit(f"{label} 未在预期时间内就绪：{url}")


def ensure_postgres(password: str) -> None:
    existing = inspect(POSTGRES_CONTAINER)
    if existing:
        if (existing["Config"].get("Labels") or {}).get("com.hci.semantic-online-e2e") != "true":
            raise SystemExit(f"{POSTGRES_CONTAINER} 不是本工具创建的容器，拒绝复用")
        if not existing["State"]["Running"]:
            run(["docker", "start", POSTGRES_CONTAINER], capture_output=True)
    else:
        source = inspect("hci-postgres")
        image = source["Config"]["Image"] if source else "postgres:16-alpine"
        run(
            [
                "docker", "run", "-d", "--name", POSTGRES_CONTAINER, "--label", LABEL,
                "--network", NETWORK, "-e", "POSTGRES_USER=hci_sim", "-e", "POSTGRES_PASSWORD",
                "-e", "POSTGRES_DB=hci_sim", "-v", f"{POSTGRES_VOLUME}:/var/lib/postgresql/data", image,
            ],
            env={**os.environ, "POSTGRES_PASSWORD": password},
            capture_output=True,
        )
    for _ in range(60):
        result = subprocess.run(
            ["docker", "exec", POSTGRES_CONTAINER, "pg_isready", "-U", "hci_sim", "-d", "hci_sim"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise SystemExit("独立 hci_sim PostgreSQL 未就绪")
    for migration in sorted((ROOT / "database/hci-sim-migrations").glob("*.sql")):
        run(
            [
                "docker", "exec", "-i", POSTGRES_CONTAINER, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                "-U", "hci_sim", "-d", "hci_sim",
            ],
            input=migration.read_text(encoding="utf-8"),
            stdout=subprocess.DEVNULL,
        )


def up(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", args.run_id):
        raise SystemExit("run-id 不合法")
    if not re.fullmatch(r"hci_semantic_e2e_[a-z0-9_]{1,40}", args.database_name):
        raise SystemExit("database-name 必须是独立 hci_semantic_e2e_ 数据库")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", args.scenario):
        raise SystemExit("scenario 不是安全的 KBD 案例编号")
    semantic_state_path = ROOT / ".hci-sim-run/semantic-e2e" / args.run_id / "state.json"
    semantic_state = json.loads(semantic_state_path.read_text(encoding="utf-8"))
    selected = next(
        (item for item in semantic_state.get("cases", []) if str(item.get("support_id")) == args.scenario),
        None,
    )
    if selected is None:
        raise SystemExit("scenario 不属于该隔离验收批次，拒绝把 TestRun 目标偷偷注入生产候选集")
    base_profile_path = ROOT / "hci_sim/testdata/sample-suites/diagnosis-signal-matrix-v1.json"
    derived_profile = json.loads(base_profile_path.read_text(encoding="utf-8"))
    source_id = str(selected["source_support_id"])
    base_id = (derived_profile.get("support_aliases") or {}).get(source_id, source_id)
    if base_id not in derived_profile["cases"]:
        raise SystemExit(f"场景画像没有为源样例 {source_id} 定义证据")
    frozen_routes = (
        semantic_state.get("results", {})
        .get(args.scenario, {})
        .get("capability", {})
        .get("resolved", {})
        .get("synthetic_routes", [])
    )
    route_ids = {str(item.get("signal_id")) for item in frozen_routes if item.get("signal_id")}
    if not route_ids:
        raise SystemExit("隔离验收状态缺少冻结 synthetic_routes，不能猜测 Runtime 命令范围")
    derived_profile["cases"][base_id]["signals"] = {
        signal_id: value
        for signal_id, value in derived_profile["cases"][base_id]["signals"].items()
        if signal_id in route_ids
    }
    if set(derived_profile["cases"][base_id]["signals"]) != route_ids:
        raise SystemExit("场景画像无法完整覆盖独立副本的冻结 synthetic_routes")
    derived_profile.setdefault("support_aliases", {})[args.scenario] = base_id
    compatible_suites = derived_profile.setdefault("compatible_sample_suites", [])
    if semantic_state.get("sample_suite") not in compatible_suites:
        compatible_suites.append(semantic_state["sample_suite"])
    derived_profile_path = semantic_state_path.parent / "online-scenario-profile.json"
    derived_profile_path.write_text(json.dumps(derived_profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(derived_profile_path, 0o600)
    previous = json.loads(STATE_PATH.read_text()) if STATE_PATH.is_file() else {}
    password = previous.get("postgres_password") or secrets.token_urlsafe(32)
    # 密码必须先于容器创建原子落盘；否则在 Runtime 启动前崩溃，重试会生成
    # 新密码并永久失去对已创建测试库的访问能力。
    write_state(
        {
            "status": "initializing",
            "run_id": args.run_id,
            "database_name": args.database_name,
            "scenario": args.scenario,
            "variant": args.variant,
            "admin_port": args.admin_port,
            "postgres_password": password,
        }
    )
    ensure_postgres(password)
    database_url = f"postgresql://hci_sim:{password}@{POSTGRES_CONTAINER}:5432/hci_sim"
    lab_state = inspect(f"hci-diagnosis-lab-{INSTANCE}")
    env = {
        **os.environ,
        "HCI_SIM_DATABASE_URL": database_url,
        "HCI_SIM_DOCKER_NETWORK": NETWORK,
        # 在线验收使用同一 Docker 网络内的受管 Bridge，连接地址必须是
        # Runtime 容器名和容器端口，不能依赖宿主机 127.0.0.1 的随机映射。
        "HCI_SIM_CONNECTION_HOST": f"hci-diagnosis-lab-{INSTANCE}",
        "HCI_SIM_SCENARIO_PROFILE": str(derived_profile_path),
    }
    if lab_state:
        persisted_lab = json.loads((ROOT / ".hci-sim-run/lab" / INSTANCE / "state.json").read_text())
        if (
            (lab_state["Config"].get("Labels") or {}).get("com.hci.diagnosis-lab") != "true"
            or not lab_state["State"]["Running"]
            or persisted_lab.get("scenario") != args.scenario
            or persisted_lab.get("variant") != args.variant
            or not persisted_lab.get("database_configured")
        ):
            raise SystemExit(f"{INSTANCE} 已存在但与本次场景不一致；请先执行 down")
        print("runtime_crash_recovery_reused=PASS")
    else:
        run(
            [
                ".venv/bin/python", "scripts/hci-sim/diagnosis-lab.py", "up", "--scenario", args.scenario,
                "--instance", INSTANCE, "--variant", args.variant, "--connection-port", "2222",
            ],
            env=env,
        )
    run(
        [
            ".venv/bin/python", "scripts/hci-sim/semantic-e2e-stack.py", "--run-id", args.run_id,
            "--database-name", args.database_name, "--runtime-instance", INSTANCE,
            "--admin-port", str(args.admin_port),
        ],
        env=env,
    )
    write_state(
        {
            "status": "ready",
            "run_id": args.run_id,
            "database_name": args.database_name,
            "scenario": args.scenario,
            "variant": args.variant,
            "admin_port": args.admin_port,
            "postgres_password": password,
        }
    )
    wait_http(f"http://127.0.0.1:{args.admin_port}/admin/simulation", "Admin UI")
    print(json.dumps({
        "status": "ready",
        "admin_url": f"http://localhost:{args.admin_port}/admin/simulation",
        "scenario": args.scenario,
        "runtime_instance": INSTANCE,
        "database": "isolated hci_sim",
    }, ensure_ascii=False, indent=2))


def status(_: argparse.Namespace) -> None:
    state = read_state()
    names = [
        POSTGRES_CONTAINER, f"hci-diagnosis-lab-{INSTANCE}", "semantic-e2e-terminal-bridge", "semantic-e2e-agent",
        "semantic-e2e-conversation", "semantic-e2e-llm", "semantic-e2e-online-gateway", "semantic-e2e-admin",
    ]
    state["containers"] = {
        name: bool((item := inspect(name)) and item["State"]["Running"])
        for name in names
    }
    state.pop("postgres_password", None)
    print(json.dumps(state, ensure_ascii=False, indent=2))


def down(_: argparse.Namespace) -> None:
    state = read_state()
    # 只处理带本验收标签/固定名称的容器；数据库卷和 lab 审计目录均保留。
    for name in (
        "semantic-e2e-admin", "semantic-e2e-terminal-bridge", "semantic-e2e-online-gateway", "semantic-e2e-agent", "semantic-e2e-conversation",
        "semantic-e2e-llm",
        "semantic-e2e-customer", "semantic-e2e-gateway", "semantic-e2e-kb", "semantic-e2e-case",
        "semantic-e2e-worker", "semantic-e2e-diagnosis",
    ):
        item = inspect(name)
        if item and (item["Config"].get("Labels") or {}).get("com.hci.semantic-e2e") == state["run_id"]:
            run(["docker", "stop", name], capture_output=True)
            run(["docker", "rm", name], capture_output=True)
    if inspect(f"hci-diagnosis-lab-{INSTANCE}"):
        run([".venv/bin/python", "scripts/hci-sim/diagnosis-lab.py", "down", "--instance", INSTANCE])
    lab_directory = ROOT / ".hci-sim-run/lab" / INSTANCE
    if lab_directory.exists():
        run([".venv/bin/python", "scripts/hci-sim/diagnosis-lab.py", "reset", "--instance", INSTANCE])
    print("完整在线/离线仿真应用容器已停止；独立数据库、数据卷和审计记录已保留")


def verify(_: argparse.Namespace) -> dict:
    """用真实浏览器执行当前场景的完整在线 Agent 结果判定。"""

    state = read_state()
    if state.get("status") != "ready":
        raise SystemExit("完整在线仿真栈未处于 ready 状态")
    suite_path = ROOT / ".hci-sim-run/semantic-e2e" / state["run_id"] / "state.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    selected = next(
        (item for item in suite.get("cases", []) if str(item.get("support_id")) == state["scenario"]),
        None,
    )
    if selected is None:
        raise SystemExit("当前场景不属于隔离验收批次")
    node = os.environ.get("HCI_E2E_NODE") or shutil.which("node")
    module_path = os.environ.get("PLAYWRIGHT_MODULE")
    browser_path = os.environ.get("HCI_E2E_BROWSER_PATH")
    if not node or not module_path or not browser_path:
        raise SystemExit(
            "缺少浏览器运行契约：请设置 HCI_E2E_NODE、PLAYWRIGHT_MODULE、HCI_E2E_BROWSER_PATH"
        )
    env = {
        **os.environ,
        "HCI_E2E_SUPPORT_ID": str(selected["support_id"]),
        "HCI_E2E_CATEGORY": str(selected["category_id"]),
        "HCI_E2E_DESCRIPTION": str(selected["description"]),
        "HCI_E2E_TITLE": str(selected["title"]),
        "HCI_E2E_ADMIN_URL": f"http://localhost:{state['admin_port']}",
        "HCI_E2E_EXPECT_VM_CONSOLE": (
            "1" if "qkv_vm_console" in set((selected.get("signal_tools") or {}).values()) else "0"
        ),
    }
    completed = run(
        [node, "scripts/verify/semantic-online-agent-browser.cjs"],
        env=env,
        capture_output=True,
    )
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("check") == "semantic-online-agent-browser" and payload.get("status") == "PASS":
            return payload
    raise SystemExit("浏览器验收已结束，但缺少可追溯的 PASS 结果")


def matrix(args: argparse.Namespace) -> None:
    """逐场景重建隔离 Runtime，验收整套样例的完整在线链路。"""

    suite_path = ROOT / ".hci-sim-run/semantic-e2e" / args.run_id / "state.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    cases = suite.get("cases", [])
    expected_case_count = int(suite.get("expected_case_count", 5))
    if len(cases) != expected_case_count:
        raise SystemExit(f"在线矩阵要求恰好 {expected_case_count} 篇独立样例，实际 {len(cases)} 篇")
    attempt = args.attempt or f"matrix-{int(time.time())}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", attempt):
        raise SystemExit("attempt 不合法")
    report_path = suite_path.parent / f"online-matrix-{attempt}.json"
    report = {
        "check": "signal-v2-online-matrix" if suite.get("suite_kind") == "full" else "semantic-online-matrix",
        "status": "running",
        "run_id": args.run_id,
        "attempt": attempt,
        "suite_kind": suite.get("suite_kind", "semantic"),
        "source_sample_suite": suite.get("source_sample_suite", "kbd-semantic-entry-signal-v2"),
        "cases": [],
    }

    def save_report() -> None:
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, report_path)

    save_report()
    if STATE_PATH.is_file():
        with contextlib.suppress(Exception):
            down(argparse.Namespace())
    try:
        for index, item in enumerate(cases):
            scenario_args = argparse.Namespace(
                run_id=args.run_id,
                database_name=args.database_name,
                scenario=str(item["support_id"]),
                variant=args.variant,
                admin_port=args.admin_port,
            )
            try:
                up(scenario_args)
                browser_result = verify(argparse.Namespace())
                row = {
                    "index": index + 1,
                    "total": len(cases),
                    "support_id": item["support_id"],
                    "source_support_id": item["source_support_id"],
                    "entry_mode": item.get("entry_mode", suite.get("suite_kind", "semantic")),
                    "expected_signal_count": item.get("expected_signal_count"),
                    "case_id": browser_result["case_id"],
                    "command_count": browser_result["command_count"],
                    "vm_console_operation_count": browser_result["vm_console_operation_count"],
                    "vm_console_observation_count": browser_result["vm_console_observation_count"],
                    "status": "PASS",
                }
                report["cases"].append(row)
                save_report()
                print(json.dumps({"check": "semantic-online-agent-matrix-case", **row}, ensure_ascii=False), flush=True)
            finally:
                if index < len(cases) - 1 or not args.leave_running:
                    with contextlib.suppress(Exception):
                        down(argparse.Namespace())
        report["status"] = "PASS"
        report["passed"] = len(report["cases"])
        report["support_ids"] = [str(item["support_id"]) for item in report["cases"]]
        save_report()
        print(json.dumps(report, ensure_ascii=False), flush=True)
    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        save_report()
        if not args.leave_running:
            with contextlib.suppress(Exception):
                down(argparse.Namespace())
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    start = commands.add_parser("up")
    start.add_argument("--run-id", required=True)
    start.add_argument("--database-name", required=True)
    start.add_argument("--scenario", required=True)
    start.add_argument("--variant", default="positive")
    start.add_argument("--admin-port", type=int, default=3004)
    start.set_defaults(handler=up)
    commands.add_parser("status").set_defaults(handler=status)
    commands.add_parser("verify").set_defaults(handler=verify)
    matrix_command = commands.add_parser("matrix")
    matrix_command.add_argument("--run-id", required=True)
    matrix_command.add_argument("--database-name", required=True)
    matrix_command.add_argument("--variant", default="positive")
    matrix_command.add_argument("--admin-port", type=int, default=3004)
    matrix_command.add_argument("--attempt", default="", help="结果文件后缀；默认使用时间戳")
    matrix_command.add_argument("--leave-running", action="store_true")
    matrix_command.set_defaults(handler=matrix)
    commands.add_parser("down").set_defaults(handler=down)
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    arguments.handler(arguments)
