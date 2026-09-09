"""Signal v2 隔离闭环工具的套件边界回归。"""

from importlib import util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = ROOT / "scripts/hci-sim" / name
    spec = util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec and spec.loader
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_full_and_semantic_suites_have_distinct_runtime_identity():
    prepare = load_script("semantic-e2e-prepare.py")

    semantic = prepare.SUITES["semantic"]
    full = prepare.SUITES["full"]
    assert semantic["source_sample_suite"] == "kbd-semantic-entry-signal-v2"
    assert semantic["entry_mode"] == "semantic_fallback"
    assert full["source_sample_suite"] == "diagnosis-signal-matrix-v1"
    assert full["entry_mode"] == "strong_signal"
    assert semantic["runtime_suite_prefix"] != full["runtime_suite_prefix"]
    assert semantic["category_source"] != full["category_source"]


def test_full_and_semantic_aliases_use_the_same_fault_object_scope():
    offline = load_script("semantic-e2e-offline.py")

    for suffix in ("VM", "LOG", "HW-PLT", "CORE", "NET-STO"):
        assert offline.affected_objects(f"SAMPLE-V2-{suffix}") == offline.affected_objects(
            f"SAMPLE-SIG-{suffix}"
        )


def test_diagnostic_execution_contract_excludes_remediation_signals():
    prepare = load_script("semantic-e2e-prepare.py")
    document = {
        "signals": [
            {"id": "task", "orchestrate": {"phase": "diagnostic"}},
            {
                "id": "case-context",
                "acquire": {"tool": "qkv_case_context"},
                "orchestrate": {},
            },
            {"id": "effect", "orchestrate": {"phase": "remediation"}},
        ]
    }

    assert [item["id"] for item in prepare.diagnostic_signals(document)] == ["task", "case-context"]
    assert [item["id"] for item in prepare.executable_diagnostic_signals(document)] == ["task"]


def test_offline_matrix_is_bound_to_full_suite_and_all_four_stages():
    prepare = load_script("semantic-e2e-prepare.py")
    source = (ROOT / "scripts/hci-sim/signal-v2-offline-matrix.py").read_text(encoding="utf-8")
    expected_non_fixture = {"qkv_case_context", "qkv_vm_console", "qkv_effect"}

    assert expected_non_fixture == prepare.NON_FIXTURE_PRODUCER_TOOLS
    assert 'suite.get("suite_kind") != "full"' in source
    assert '("prepare", "collect", "upload", "verify")' in source
    assert "HCI_SIM_CAPABILITIES_URL" in source


def test_online_matrix_owns_terminal_bridge_and_uses_container_ssh_endpoint():
    stack = (ROOT / "scripts/hci-sim/semantic-e2e-stack.py").read_text(encoding="utf-8")
    online = (ROOT / "scripts/hci-sim/semantic-online-e2e.py").read_text(encoding="utf-8")
    lab = (ROOT / "scripts/hci-sim/diagnosis-lab.py").read_text(encoding="utf-8")

    assert '"semantic-e2e-terminal-bridge"' in stack
    assert '"TERMINAL_BRIDGE_UPSTREAM": "http://semantic-e2e-terminal-bridge:9999"' in stack
    assert '"HCI_BRIDGE_LOG_DIR": "/var/lib/terminal-bridge"' in stack
    assert "host.docker.internal:9999" not in stack
    assert '"HCI_SIM_CONNECTION_HOST": f"hci-diagnosis-lab-{INSTANCE}"' in online
    assert '"--connection-port", "2222"' in online
    assert 'f"HCI_SIM_SSH_PUBLIC_PORT={connection_port}"' in lab
    assert 'f"online-matrix-{attempt}.json"' in online
