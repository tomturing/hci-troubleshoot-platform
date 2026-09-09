#!/usr/bin/env python3
"""启动本机专用诊断样例验收栈；克隆数据库隔离全局采集资源同步的影响。"""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

from sqlalchemy.engine import make_url


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def inspect(name):
    return json.loads(run(["docker", "inspect", name], capture_output=True).stdout)[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--database-name", required=True, help="仅允许 hci_semantic_e2e_ 前缀的独立库")
    parser.add_argument("--runtime-instance", help="同时启动完整在线 Agent 仿真栈时使用的 diagnosis-lab 实例")
    parser.add_argument("--admin-port", type=int, default=3004)
    parser.add_argument("--sync-offline-resources", action="store_true", help="服务就绪后执行一次全量离线资源同步")
    parser.add_argument("--down", action="store_true", help="仅停止本批次隔离应用容器，保留数据库、卷和审计记录")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,40}", args.run_id):
        raise SystemExit("run-id 不合法")
    if not re.fullmatch(r"hci_semantic_e2e_[a-z0-9_]{1,40}", args.database_name):
        raise SystemExit("独立数据库名称不合法")
    root = Path(__file__).resolve().parents[2]
    state = json.loads((root / ".hci-sim-run/semantic-e2e" / args.run_id / "state.json").read_text())
    expected_case_count = int(state.get("expected_case_count", 5))
    assert state["run_id"] == args.run_id and len(state["cases"]) == expected_case_count
    if args.down:
        for name in (
            "semantic-e2e-admin",
            "semantic-e2e-terminal-bridge",
            "semantic-e2e-online-gateway",
            "semantic-e2e-agent",
            "semantic-e2e-conversation",
            "semantic-e2e-llm",
            "semantic-e2e-customer",
            "semantic-e2e-gateway",
            "semantic-e2e-kb",
            "semantic-e2e-case",
            "semantic-e2e-worker",
            "semantic-e2e-diagnosis",
        ):
            probe = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
            if probe.returncode:
                continue
            item = json.loads(probe.stdout)[0]
            if (item["Config"].get("Labels") or {}).get("com.hci.semantic-e2e") != args.run_id:
                raise SystemExit(f"{name} 属于其他验收批次，拒绝停止")
            run(["docker", "stop", name], capture_output=True)
            run(["docker", "rm", name], capture_output=True)
        print("隔离验收应用容器已停止；独立数据库、数据卷和审计记录已保留")
        return
    db_name = args.database_name
    postgres = inspect("hci-postgres")
    pg_env = dict(item.split("=", 1) for item in postgres["Config"]["Env"])
    user, source_db = pg_env["POSTGRES_USER"], pg_env["POSTGRES_DB"]

    def sql(statement, database=source_db):
        return run(
            [
                "docker",
                "exec",
                "hci-postgres",
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                user,
                "-d",
                database,
                "-Atc",
                statement,
            ],
            capture_output=True,
        ).stdout.strip()

    assert source_db != db_name
    if sql(f"SELECT 1 FROM pg_database WHERE datname='{db_name}'") != "1":
        sql(f'CREATE DATABASE "{db_name}"')
        # 数据仅在 Docker 内部管道复制，不把业务数据或凭据输出到终端、宿主机文件。
        dump = subprocess.Popen(
            ["docker", "exec", "hci-postgres", "pg_dump", "-U", user, "--no-owner", "--no-privileges", source_db],
            stdout=subprocess.PIPE,
        )
        restored = subprocess.run(
            ["docker", "exec", "-i", "hci-postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db_name],
            stdin=dump.stdout,
            stdout=subprocess.DEVNULL,
        )
        dump.stdout.close()
        assert dump.wait() == 0 and restored.returncode == 0, "克隆失败，不启动测试服务"
        ids = ",".join(str(int(case["kbd_id"])) for case in state["cases"])
        # 仅清空新克隆库的诊断作业，防止测试 worker 消费旧工单任务；原库绝不执行此语句。
        sql("TRUNCATE diagnosis_session CASCADE", db_name)
        sql(f"UPDATE kbd_entry SET status='draft' WHERE id NOT IN ({ids}) AND status='published'", db_name)
        sql(f"COMMENT ON DATABASE {db_name} IS '独立诊断样例验收；禁止作为业务库；{args.run_id}'", db_name)
        print("isolated_database_created=PASS", flush=True)
    else:
        marker = sql(f"SELECT obj_description(oid, 'pg_database') FROM pg_database WHERE datname='{db_name}'")
        accepted_markers = {
            f"独立诊断样例验收；禁止作为业务库；{args.run_id}",
            f"独立语义验收；禁止作为业务库；{args.run_id}",
        }
        if marker in accepted_markers:
            marker = "accepted"
        elif marker:
            raise SystemExit("已有独立数据库属于其他验收批次，拒绝复用；请使用新的 DATABASE_NAME")
    if sql(f"SELECT obj_description(oid, 'pg_database') FROM pg_database WHERE datname='{db_name}'") not in {
        f"独立诊断样例验收；禁止作为业务库；{args.run_id}",
        f"独立语义验收；禁止作为业务库；{args.run_id}",
    }:
        # 兼容早期脚本已创建但未写 COMMENT 的隔离库。容器会被 down 正常删除，
        # 因而不能把“旧测试容器仍在运行”作为再次 up 的必要证据；改为校验库内
        # 恰好只有本批次样例 published KBD，且全部带 test_only/run_id 标记。
        # 任一事实不匹配都继续拒绝，绝不凭数据库名猜测用途。
        ids = ",".join(str(int(case["kbd_id"])) for case in state["cases"])
        matching_rows = sql(
            f"SELECT count(*) FROM kbd_entry WHERE id IN ({ids}) "
            f"AND status='published' AND metadata->>'run_id'='{args.run_id}' "
            "AND metadata->>'test_only'='true'",
            db_name,
        )
        published_rows = sql("SELECT count(*) FROM kbd_entry WHERE status='published'", db_name)
        if matching_rows != str(expected_case_count) or published_rows != str(expected_case_count):
            raise SystemExit("已有数据库没有可验证的验收隔离事实，拒绝启动；请使用新的独立数据库名称")
        sql(f"COMMENT ON DATABASE {db_name} IS '独立诊断样例验收；禁止作为业务库；{args.run_id}'", db_name)
        print("isolated_database_legacy_marker_repaired=PASS", flush=True)

    def start(
        source_name,
        target_name,
        app_path,
        overrides,
        command,
        ports=(),
        volumes=(),
        extra_hosts=(),
        image=None,
        inherit_source_env=True,
    ):
        exists = subprocess.run(["docker", "inspect", target_name], capture_output=True).returncode == 0
        if exists:
            existing = inspect(target_name)
            if (existing["Config"].get("Labels") or {}).get("com.hci.semantic-e2e") != args.run_id:
                raise SystemExit(f"{target_name} 属于其他验收批次，请先人工处理旧测试栈")
            print(f"existing={target_name}", flush=True)
            return
        source = inspect(source_name) if source_name else None
        if inherit_source_env and source is None:
            raise SystemExit(f"缺少环境继承源容器：{source_name}")
        env = dict(item.split("=", 1) for item in source["Config"]["Env"]) if inherit_source_env else {}
        if "DATABASE_URL" in env:
            env["DATABASE_URL"] = (
                make_url(env["DATABASE_URL"]).set(database=db_name).render_as_string(hide_password=False)
            )
        env.update(overrides)
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            target_name,
            "--label",
            f"com.hci.semantic-e2e={args.run_id}",
            "--network",
            "hci-troubleshoot-platform_default",
        ]
        for key in env:
            cmd.extend(["-e", key])
        for port in ports:
            cmd.extend(["-p", port])
        for host in extra_hosts:
            cmd.extend(["--add-host", host])
        if app_path:
            cmd.extend(["-v", f"{root / app_path}:/app", "-v", f"{root / 'backend/shared'}:/app/shared"])
        for volume in volumes:
            cmd.extend(["-v", volume])
        selected_image = image or (source and source["Image"])
        if not selected_image:
            raise SystemExit(f"{target_name} 缺少可启动镜像")
        cmd.extend([selected_image, *command])
        run(cmd, env={**os.environ, **env}, capture_output=True)
        print(f"started={target_name}", flush=True)

    data_volume = f"semantic-e2e-data-{args.run_id}:/var/lib/hci-diagnosis"
    # 在线 AI 取值与离线 VM 控制台视觉观察都必须留在隔离测试网内；该桩只
    # 接受两种显式合同，未知请求 422，绝不回退到真实外部模型。
    start(
        "hci-agent-service",
        "semantic-e2e-llm",
        None,
        {"PYTHONUNBUFFERED": "1"},
        ["python", "/opt/hci-sim/semantic_llm_stub.py", "--port", "8009"],
        volumes=[f"{root / 'scripts/hci-sim/semantic_llm_stub.py'}:/opt/hci-sim/semantic_llm_stub.py:ro"],
        inherit_source_env=False,
    )
    vision_overrides = {
        "VM_CONSOLE_CAPTURE_ENABLED": "true",
        "VM_CONSOLE_VISION_ALLOWED": "true",
        "LLM_VISION_BASE_URL": "http://semantic-e2e-llm:8009/v1",
        "LLM_VISION_API_KEY": "signal-v2-e2e-local-contract",
        "VISION_MODEL": "signal-v2-vision-contract-stub",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "NO_PROXY": "*",
    }
    start(
        "hci-diagnosis-service",
        "semantic-e2e-diagnosis",
        "backend/diagnosis-service",
        {
            "DIAGNOSIS_ALLOWED_ORIGINS": "http://localhost:3003,http://localhost:3004",
            "KB_SERVICE_URL": "http://semantic-e2e-kb:8004",
            "LANGFUSE_SECRET_KEY": "",
            "LANGFUSE_PUBLIC_KEY": "",
            **vision_overrides,
        },
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"],
        ports=["127.0.0.1:18018:8008"],
        volumes=[data_volume],
    )
    start(
        "hci-diagnosis-worker",
        "semantic-e2e-worker",
        "backend/diagnosis-service",
        vision_overrides,
        ["python", "-m", "app.worker"],
        volumes=[data_volume],
    )
    start(
        "hci-case-service",
        "semantic-e2e-case",
        "backend/case-service",
        {},
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"],
    )
    start(
        "hci-kb-service",
        "semantic-e2e-kb",
        "backend/kb-service",
        {},
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"],
        ports=["127.0.0.1:18024:8004"],
    )
    if args.runtime_instance:
        runtime_state = json.loads(
            (root / ".hci-sim-run/lab" / args.runtime_instance / "state.json").read_text()
        )
        if not runtime_state.get("database_configured") or runtime_state.get("status") != "running":
            raise SystemExit("完整在线仿真要求 diagnosis-lab 使用独立 hci_sim 数据库并处于 running")
        runtime_name = runtime_state["container"]
        runtime_url = f"http://{runtime_name}:8080"
        token = os.getenv("INTERNAL_API_TOKEN", "hci-dev-internal-token")
        fixture_manifest = json.loads(
            (Path(runtime_state["connection_file"]).parent / "fixture-manifest.json").read_text()
        )
        fixture_variables = fixture_manifest.get("variables") or {}
        fixture_vm_id = str(fixture_variables.get("VM_ID") or "").strip()
        fixture_host = str(fixture_variables.get("HOST") or "").strip()
        sim_inventory = f"{fixture_vm_id}={fixture_host}" if fixture_vm_id and fixture_host else ""
        # 完整回归不得把测试工单、Prompt 或环境上下文发往外部模型服务。
        # 指向容器自身的关闭端口，使 S0 立即走已有确定性候选回退；KBD 的
        # 确定性 matcher/producer/consumer 链仍按生产代码执行。
        isolated_assistant_registry = json.dumps({
            "htp-agent": {
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "semantic-e2e-no-external",
                "model": "semantic-e2e-disabled",
                "enabled": True,
            }
        })
        isolated_agent_registry = json.dumps({
            "htp-agent": {
                "base_url": "http://semantic-e2e-llm:8009",
                "api_key": "semantic-e2e-local-contract",
                "model": "semantic-signal-v2-contract-stub",
                "enabled": True,
            }
        })
        start(
            "hci-conversation-service",
            "semantic-e2e-conversation",
            "backend/conversation-service",
            {
                "CASE_SERVICE_URL": "http://semantic-e2e-case:8001",
                "CONVERSATION_SERVICE_URL": "http://semantic-e2e-conversation:8002",
                "KB_SERVICE_URL": "http://semantic-e2e-kb:8004",
                "AGENT_SERVICE_URL": "http://semantic-e2e-agent:8005",
                "HCI_SIM_URL": runtime_url,
                "HCI_SIM_CONTROL_TOKEN": token,
                "LLM_BASE_URL": "http://127.0.0.1:9/v1",
                "LLM_API_KEY": "semantic-e2e-no-external",
                "ASSISTANT_REGISTRY_JSON": isolated_assistant_registry,
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
                "NO_PROXY": "*",
            },
            ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"],
        )
        start(
            "hci-agent-service",
            "semantic-e2e-agent",
            "backend/agent-service",
            {
                "CASE_SERVICE_URL": "http://semantic-e2e-case:8001",
                "CONVERSATION_SERVICE_URL": "http://semantic-e2e-conversation:8002",
                "KB_SERVICE_URL": "http://semantic-e2e-kb:8004",
                "AGENT_SERVICE_URL": "http://semantic-e2e-agent:8005",
                "LLM_BASE_URL": "http://semantic-e2e-llm:8009",
                "LLM_API_KEY": "semantic-e2e-local-contract",
                "LLM_MODEL": "semantic-signal-v2-contract-stub",
                "ASSISTANT_REGISTRY_JSON": isolated_agent_registry,
                "AI_COMPLETIONS_PATH": "/v1/chat/completions",
                "AI_COMPLETIONS_PATH_EXTERNAL": "/v1/chat/completions",
                "VM_CONSOLE_CAPTURE_ENABLED": "true",
                "VM_CONSOLE_VISION_ALLOWED": "true",
                "VM_CONSOLE_SIM_INVENTORY": sim_inventory,
                "EFFECT_VERIFICATION_ENABLED": "true",
                "LLM_VISION_BASE_URL": "http://semantic-e2e-llm:8009/v1",
                "LLM_VISION_API_KEY": "signal-v2-e2e-local-contract",
                "VISION_MODEL": "signal-v2-vision-contract-stub",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
                "NO_PROXY": "*",
            },
            ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8005"],
        )
    gateway_name = "semantic-e2e-online-gateway" if args.runtime_instance else "semantic-e2e-gateway"
    start(
        "hci-api-gateway",
        gateway_name,
        "backend/api-gateway",
        {
            "DIAGNOSIS_SERVICE_URL": "http://semantic-e2e-diagnosis:8008",
            "CASE_SERVICE_URL": "http://semantic-e2e-case:8001",
            "KB_SERVICE_URL": "http://semantic-e2e-kb:8004",
            "CONVERSATION_SERVICE_URL": (
                "http://semantic-e2e-conversation:8002" if args.runtime_instance else "http://conversation-service:8002"
            ),
            "AGENT_SERVICE_URL": "http://semantic-e2e-agent:8005" if args.runtime_instance else "http://agent-service:8005",
            **(
                {
                    "HCI_SIM_URL": runtime_url,
                    "HCI_SIM_CONTROL_TOKEN": token,
                }
                if args.runtime_instance
                else {}
            ),
        },
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
    )
    if args.runtime_instance:
        # 完整在线验收必须自带受管 Terminal Bridge，不能依赖开发机是否恰好
        # 在 127.0.0.1:9999 启动了桌面进程。Bridge 与 hci-sim 位于同一隔离
        # 网络，浏览器只通过 Admin UI 的同源 WebSocket 访问它。
        bridge_image = "hci-terminal-bridge:semantic-e2e-local"
        run([
            "docker", "build", "-t", bridge_image, "-f", "terminal_bridge/Dockerfile", ".",
        ])
        start(
            None,
            "semantic-e2e-terminal-bridge",
            None,
            {
                "HCI_BRIDGE_MODE": "cluster",
                "HCI_BRIDGE_ALLOWED_ORIGINS": f"http://localhost:{args.admin_port}",
                "HCI_BRIDGE_LOG_DIR": "/var/lib/terminal-bridge",
                "HCI_BRIDGE_OTEL_ENDPOINT": "",
                "PLATFORM_ARTIFACT_URL": "http://semantic-e2e-conversation:8002",
                "PLATFORM_INTERNAL_API_TOKEN": token,
            },
            [],
            image=bridge_image,
            inherit_source_env=False,
        )
        # admin-ui 是静态制品，不能像 Python 服务一样源码挂载；显式构建后按
        # Compose 镜像标签启动，保证浏览器使用本次工作树代码。
        run([
            "docker", "compose", "--env-file", ".env", "-f", "deploy/docker/docker-compose.yml",
            "build", "admin-ui",
        ])
        admin_source = inspect("hci-admin-ui")
        start(
            "hci-admin-ui",
            "semantic-e2e-admin",
            None,
            {
                "API_GATEWAY_UPSTREAM": f"http://{gateway_name}:8000",
                "DIAGNOSIS_SERVICE_UPSTREAM": "http://semantic-e2e-diagnosis:8008",
                "TERMINAL_BRIDGE_UPSTREAM": "http://semantic-e2e-terminal-bridge:9999",
            },
            ["nginx", "-g", "daemon off;"],
            ports=[f"127.0.0.1:{args.admin_port}:80"],
            image=admin_source["Config"]["Image"],
        )
        print(f"isolated_admin_url=http://localhost:{args.admin_port}/admin/simulation")
    start(
        "hci-customer-ui",
        "semantic-e2e-customer",
        None,
        {
            "API_GATEWAY_UPSTREAM": "http://semantic-e2e-gateway:8000",
            "DIAGNOSIS_SERVICE_UPSTREAM": "http://semantic-e2e-diagnosis:8008",
        },
        ["nginx", "-g", "daemon off;"],
        ports=["127.0.0.1:3003:80"],
    )
    if args.sync_offline_resources:
        for url in ("http://127.0.0.1:18024/health", "http://127.0.0.1:18018/health/ready"):
            for _ in range(90):
                probe = subprocess.run(
                    ["curl", "--fail", "--silent", "--show-error", url],
                    capture_output=True,
                    text=True,
                )
                if probe.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise SystemExit(f"离线资源同步前服务未就绪：{url}")
        run(
            [
                str(root / ".venv/bin/python"),
                str(root / "scripts/hci-sim/diagnosis-lab.py"),
                "sync-resources",
                "--mode",
                "full",
                "--diagnosis-url",
                "http://127.0.0.1:18018",
            ]
        )
        print(f"offline_resource_sync=PASS suite={state.get('suite_kind', 'semantic')}", flush=True)
    print("isolated_customer_url=http://localhost:3003")


if __name__ == "__main__":
    main()
