from shared.resolution import ResolutionStatus, SignalIntent, build_resolution_audit_snapshot, get_resolution_runtime
from shared.resolution.catalog import command_path_known, load_acli_catalog


def test_log_resolver_corrects_typo_and_expands_single_or_double_day_directory():
    runtime = get_resolution_runtime()
    plan = runtime.compile(
        SignalIntent(
            resolver_id="log",
            tool="qfk_log",
            args={"file": "vtpdeamon", "time_window": "2026-08-07 10:00:00"},
        )
    )
    assert plan.status is ResolutionStatus.COMPILED
    assert plan.canonical_args["file"] == "sfvt_vtpdaemon.log"
    assert [item["path"] for item in plan.candidates] == ["/sf/log/7/vt", "/sf/log/07/vt"]
    resolved = runtime.resolve(plan, {"path_exists": lambda path: path == "/sf/log/07/vt/sfvt_vtpdaemon.log"})
    assert resolved.status is ResolutionStatus.VERIFIED
    assert resolved.absolute_path == "/sf/log/07/vt/sfvt_vtpdaemon.log"
    assert resolved.evidence["aliases_used"] == ["vtpdeamon"]
    missing = runtime.resolve(plan, {"path_exists": lambda _path: False})
    assert missing.status is ResolutionStatus.NEEDS_PROBE


def test_log_resolver_accepts_partial_and_full_absolute_file_paths():
    runtime = get_resolution_runtime()
    partial = runtime.compile(
        SignalIntent(resolver_id="log", tool="qfk_log", args={"path": "vt/sfvt_vtpdaemon.log", "time_window": "2026-08-07"})
    )
    assert partial.canonical_args["file"] == "sfvt_vtpdaemon.log"
    assert partial.candidates[0]["path"] == "/sf/log/7/vt"

    full = runtime.compile(
        SignalIntent(
            resolver_id="log",
            tool="qfk_log",
            args={"path": "/sf/log/today/vt/sfvt_vtpdaemon.log", "time_window": "2026-08-07"},
        )
    )
    # today 只是逻辑别名；有 END 时必须改为目标日期目录。
    assert full.canonical_args["file"] == "sfvt_vtpdaemon.log"
    assert full.candidates[0]["path"] == "/sf/log/7/vt"


def test_log_archive_requires_explicit_precheck():
    runtime = get_resolution_runtime()
    plan = runtime.compile(
        SignalIntent(
            resolver_id="log",
            tool="qfk_log",
            args={"file": "messages", "include_archives": True},
        )
    )
    assert plan.status is ResolutionStatus.BLOCKED
    assert plan.issues[0].code == "LOG_ARCHIVE_PRECHECK_REQUIRED"


def test_domain_resolver_normalizes_dotted_command_to_argv():
    runtime = get_resolution_runtime()
    plan, resolved = runtime.compile_and_resolve(
        SignalIntent(
            resolver_id="domain",
            tool="qfk_vm",
            args={"command": "acli.vm.config.get", "command_args": ["--vm-id", "123"], "domain": "vm"},
        )
    )
    assert plan.canonical_args["argv"] == ["acli", "vm", "config", "get", "--vm-id", "123"]
    assert resolved.argv == ["acli", "vm", "config", "get", "--vm-id", "123"]
    assert resolved.command == "acli vm config get --vm-id 123"


def test_system_resolver_fails_closed_for_unknown_command_path():
    plan = get_resolution_runtime().compile(
        SignalIntent(resolver_id="system", tool="qfk_system", args={"command": "definitely_not_a_real_acli_command"})
    )
    assert plan.status is ResolutionStatus.BLOCKED
    assert plan.issues[0].code == "SYSTEM_COMMAND_UNKNOWN"


def test_shared_resolution_loads_acli_catalog_and_accepts_ps_path():
    assert load_acli_catalog()
    assert command_path_known(["acli", "system", "ps", "-p", "9527", "-o", "cmd="])


def test_domain_resolver_keeps_domain_boundary_and_catalog_warning():
    runtime = get_resolution_runtime()
    plan, resolved = runtime.compile_and_resolve(
        SignalIntent(resolver_id="domain", tool="qfk_vm", args={"command": "list", "domain": "vm"})
    )
    assert resolved.argv[:3] == ["acli", "vm", "list"]
    assert resolved.status in {ResolutionStatus.VERIFIED, ResolutionStatus.NEEDS_PROBE}


def test_domain_resolver_requires_vm_id_for_vm_config_get():
    runtime = get_resolution_runtime()
    plan = runtime.compile(
        SignalIntent(resolver_id="domain", tool="qfk_vm", args={"command": "config get", "domain": "vm"})
    )
    assert plan.status is ResolutionStatus.BLOCKED
    assert plan.issues[0].code == "DOMAIN_REQUIRED_ARGUMENT"
    plan = runtime.compile(
        SignalIntent(
            resolver_id="domain",
            tool="qfk_vm",
            args={"command": "config get", "command_args": ["--vm-id", "123"], "domain": "vm"},
        )
    )
    assert plan.status is ResolutionStatus.COMPILED


def test_qkv_action_catalog_canonicalizes_reviewed_alias_with_bounded_candidates():
    runtime = get_resolution_runtime()
    plan, resolved = runtime.compile_and_resolve(
        SignalIntent(resolver_id="qkv", tool="qkv_task", args={"query": "task", "keyword": "开启虚拟机", "limit": 10})
    )
    assert plan.status is ResolutionStatus.COMPILED
    assert plan.canonical_args["action_id"] == "vm.power_on"
    assert plan.canonical_args["canonical_keyword"] == "启动虚拟机"
    assert plan.canonical_args["keyword_candidates"][:2] == ["启动虚拟机", "开启虚拟机"]
    assert resolved.status is ResolutionStatus.VERIFIED
    assert resolved.argv[6] == "启动虚拟机"
    assert resolved.evidence["matched_as"] == "alias"


def test_qkv_unknown_keyword_stays_exact_and_does_not_receive_unreviewed_fuzzy_guess():
    plan = get_resolution_runtime().compile(
        SignalIntent(resolver_id="qkv", tool="qkv_task", args={"query": "task", "keyword": "完全未知动作"})
    )
    assert plan.canonical_args["keyword_candidates"] == ["完全未知动作"]
    assert "action_id" not in plan.canonical_args


def test_resolution_audit_snapshot_is_deterministic_and_immutable():
    runtime = get_resolution_runtime()
    plan, acquisition = runtime.compile_and_resolve(
        SignalIntent(resolver_id="qkv", tool="qkv_task", args={"query": "task", "keyword": "启动虚拟机"})
    )
    first = build_resolution_audit_snapshot(plan, acquisition)
    second = build_resolution_audit_snapshot(plan, acquisition)
    assert first.snapshot_id == second.snapshot_id
    assert first.resolver_id == "qkv"
    assert first.catalog_version == acquisition.catalog_version


def test_service_resolver_is_read_only_and_variable_resolver_fails_closed():
    runtime = get_resolution_runtime()
    blocked = runtime.compile(
        SignalIntent(resolver_id="service", tool="qfk_service", args={"service": "asv-manager", "action": "restart"})
    )
    assert blocked.status is ResolutionStatus.BLOCKED
    assert blocked.issues[0].code == "SERVICE_READ_ONLY_REQUIRED"

    variable_plan = runtime.compile(
        SignalIntent(resolver_id="variable", args={"path": "/sf/log/{{END}}/{{VM}}.log"})
    )
    unresolved = runtime.resolve(variable_plan, {"variables": {"END": "7"}})
    assert unresolved.status is ResolutionStatus.NEEDS_PROBE
    assert unresolved.issues[0].field == "VM"
