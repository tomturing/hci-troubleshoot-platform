"""根因信号「完整闭环」评估器单测（PR-1 底座）。

设计文档 §10 测试守护，失败态优先：悬空/参数化/能力缺失/全拒 一律判拒绝候选（不自动降级 text），
合法兜底（省略 host）与 producer 填补判 closed。判据须与关键信号轨同源（复用 derive/produces 聚合）。
"""

from app.services.root_signal_closure import (
    CLOSURE_CAPABILITY,
    CLOSURE_CLOSED,
    CLOSURE_DANGLING,
    CLOSURE_LITERAL,
    CLOSURE_NOT_APPLICABLE,
    adjudicate_root_closure,
    apply_closure_fields,
    detect_hardcoded_literals,
    evaluate_root_closure,
    split_accepted_and_rejected,
    summarize_rejections,
)

CATALOG = {"qfk_system", "qfk_log", "qfk_network", "qfk_storage"}


def _exec_root(*, tool="qfk_system", args=None, match=None, signal_text="根因特征") -> dict:
    return {
        "schema_version": 2,
        "role": "root_cause",
        "match_type": "executable",
        "signal_text": signal_text,
        "acquire": {"tool": tool, "args": args or {}},
        "match": match or {"type": "keyword", "mode": "all", "expected": ["some-error"]},
    }


def _qkv_producer(signal_id, produces_names) -> dict:
    return {
        "id": signal_id,
        "acquire": {"tool": "qkv_task", "args": {"keyword": "添加IP别名失败", "is_failed": True}},
        "match": None,
        "orchestrate": {"produces": [{"name": n} for n in produces_names]},
    }


def _one(root, key_signals=(), **kw):
    return evaluate_root_closure([root], list(key_signals), capability_catalog=CATALOG, **kw)[0]


# ─── 决策①：省略 host = 合法闭环（不入 requires，不误判悬空）───────────────────
def test_omit_host_is_legal_closure():
    # 无 {{HOST}} 占位符 → requires 不含 HOST；但 command_args 写死 eth0 → 仅剩参数化态
    root = _exec_root(args={"command": "cat", "command_args": ["/sf/cfg/if.d/eth0"]})
    res = _one(root)
    assert "HOST" not in res["requires"]
    assert res["closure_status"] == CLOSURE_LITERAL


def test_local_op_without_placeholder_and_literal_is_closed():
    root = _exec_root(args={"command": "lsblk"}, match={"type": "keyword", "mode": "all", "expected": ["disk failure"]})
    res = _one(root)
    assert res["requires"] == []
    assert res["closure_status"] == CLOSURE_CLOSED
    assert res["reject"] is False


# ─── 悬空 require：显式写 {{HOST}} 无来源且非白名单 → 拒绝候选（不转 text）───────
def test_dangling_host_is_rejected_not_downgraded():
    root = _exec_root(args={"command": "cat", "host": "{{HOST}}", "command_args": ["/var/log/msg"]})
    res = _one(root)
    assert res["closure_status"] == CLOSURE_DANGLING
    assert res["unresolved"] == ["HOST"]
    assert res["reject"] is True
    assert res["reason_code"] == "ROOT_CLOSURE_DANGLING"
    # 决策②：绝不自动降级 text —— 状态不是 text/not_applicable，而是显式拒绝候选
    assert res["closure_status"] != CLOSURE_NOT_APPLICABLE


def test_dangling_filled_by_producer_is_closed():
    root = _exec_root(args={"command": "cat", "command_args": ["/sf/cfg/if.d/{{ETHX}}"]})
    res = _one(root, key_signals=[_qkv_producer("s1", ["ETHX"])])
    assert res["closure_status"] == CLOSURE_CLOSED
    ethx = next(r for r in res["resolved_by"] if r["var"] == "ETHX")
    assert ethx["by"] == "producer" and ethx["ref"] == "s1"


def test_dangling_filled_by_context_whitelist_is_closed():
    root = _exec_root(args={"command": "cat", "host": "{{HOST}}", "command_args": ["/var/log/msg"]})
    res = _one(root, context_whitelist=["HOST"])
    assert res["closure_status"] == CLOSURE_CLOSED
    host = next(r for r in res["resolved_by"] if r["var"] == "HOST")
    assert host["by"] == "context"


def test_dangling_filled_by_external_declaration_is_closed():
    root = _exec_root(args={"command": "cat", "command_args": ["/sf/cfg/if.d/{{ETHX}}"]})
    res = _one(root, verification_contract={"variables": {"ETHX": {"type": "string"}}})
    assert res["closure_status"] == CLOSURE_CLOSED
    ethx = next(r for r in res["resolved_by"] if r["var"] == "ETHX")
    assert ethx["by"] == "external"


# ─── 能力缺失：tool 不在能力目录 → capability_missing（最优先）──────────────────
def test_capability_missing_precedes_dangling():
    root = _exec_root(tool="qfk_unknown", args={"command": "cat", "host": "{{HOST}}"})
    res = _one(root)
    assert res["closure_status"] == CLOSURE_CAPABILITY
    assert res["reason_code"] == "ROOT_CLOSURE_CAPABILITY_MISSING"


def test_capability_missing_precedes_literal():
    root = _exec_root(tool="qfk_docker", args={"command": "cat", "command_args": ["/dev/sda"]})
    res = _one(root)
    assert res["closure_status"] == CLOSURE_CAPABILITY


# ─── 参数化门禁：随现场变化实体检出 + 稳定值不误判 ──────────────────────────────
def test_detect_hardcoded_interface_ip_device():
    root = _exec_root(
        args={"command": "cat", "command_args": ["/sf/cfg/if.d/eth0"]},
        match={"type": "regex", "mode": "all", "pattern": "address = 10.0.0.5 /dev/sda"},
    )
    res = _one(root)
    assert res["closure_status"] == CLOSURE_LITERAL
    entities = {f["entity"] for f in res["literals"]}
    assert {"interface", "ipv4", "device"} <= entities
    assert res["reason_code"] == "ROOT_CLOSURE_LITERAL_NOT_PARAMETERIZED"


def test_placeholder_inside_string_not_flagged():
    # {{IP}}/{{ETHX}} 是占位符，不应被当作写死字面量
    root = _exec_root(args={"command": "cat", "command_args": ["/sf/cfg/if.d/{{ETHX}}"]})
    assert detect_hardcoded_literals(root) == []


def test_stable_paths_and_commands_not_flagged():
    root = _exec_root(
        args={"command": "smartctl", "command_args": ["-a", "/dev/null", "/sf/log/messages"]},
        match={"type": "keyword", "mode": "all", "expected": ["ethtool 6.11.1 已发布"]},
    )
    # /dev/null 稳定、ethtool 命令词、6.11.1 三段版本号均不应命中紧口径实体
    assert detect_hardcoded_literals(root) == []


def test_disk_device_flagged_but_null_not():
    assert detect_hardcoded_literals(_exec_root(args={"command": "x", "command_args": ["/dev/sda1"]}))
    assert detect_hardcoded_literals(_exec_root(args={"command": "x", "command_args": ["/dev/nvme0n1"]}))
    assert detect_hardcoded_literals(_exec_root(args={"command": "x", "command_args": ["/dev/null"]})) == []


# ─── text 形态无闭包义务 ────────────────────────────────────────────────────────
def test_text_form_is_not_applicable():
    txt = {
        "schema_version": 2,
        "role": "root_cause",
        "match_type": "text",
        "signal_text": "关键词锚点",
        "acquire": None,
        "match": {"type": "keyword", "mode": "all", "expected": ["介质驱动缺失"]},
    }
    res = _one(txt)
    assert res["closure_status"] == CLOSURE_NOT_APPLICABLE
    assert res["reject"] is False


# ─── 全拒收口：存在 executable 候选但无一 closed ────────────────────────────────
def test_all_rejected_when_no_executable_closes():
    dangling = _exec_root(args={"command": "cat", "host": "{{HOST}}"})
    lit = _exec_root(args={"command": "cat", "command_args": ["/sf/cfg/if.d/eth0"]})
    results = evaluate_root_closure([dangling, lit], [], capability_catalog=CATALOG)
    summary = summarize_rejections(results)
    assert summary["executable_total"] == 2
    assert summary["closed"] == 0
    assert summary["all_rejected"] is True
    assert len(summary["rejected"]) == 2


def test_partial_closure_is_not_all_rejected():
    dangling = _exec_root(args={"command": "cat", "host": "{{HOST}}"})
    ok = _exec_root(args={"command": "lsblk"}, match={"type": "keyword", "mode": "all", "expected": ["fail"]})
    results = evaluate_root_closure([dangling, ok], [], capability_catalog=CATALOG)
    summary = summarize_rejections(results)
    assert summary["closed"] == 1
    assert summary["all_rejected"] is False


# ─── 与关键轨同源：跨轨 produces（remediation 阶段产出不计入可用来源）────────────
def test_remediation_produce_not_available_for_diagnostic_closure():
    root = _exec_root(args={"command": "cat", "command_args": ["/sf/cfg/if.d/{{ETHX}}"]})
    rem = {
        "id": "r1",
        "acquire": {"tool": "qkv_task", "args": {"keyword": "x"}},
        "match": None,
        "orchestrate": {"phase": "remediation", "produces": [{"name": "ETHX"}]},
    }
    res = _one(root, key_signals=[rem])
    assert res["closure_status"] == CLOSURE_DANGLING


# ─── §6 契约原语：标注扩字段 + accepted/rejected 拆分（与 rejected_candidates 同构）───
def test_apply_closure_fields_only_annotates_executable():
    dangling = _exec_root(args={"command": "cat", "host": "{{HOST}}"})
    txt = {
        "schema_version": 2, "match_type": "text", "signal_text": "锚", "acquire": None,
        "match": {"type": "keyword", "mode": "all", "expected": ["错误"]},
    }
    results = evaluate_root_closure([dangling, txt], [], capability_catalog=CATALOG)
    annotated = apply_closure_fields([dangling, txt], results)
    assert annotated[0]["closure_status"] == CLOSURE_DANGLING
    assert annotated[0]["requires"] == ["HOST"]
    assert "closure_status" not in annotated[1]  # text 无闭包义务
    assert "closure_status" not in dangling  # 不改变传入对象


def test_split_accepted_rejected_isomomorphic_with_key_track():
    dangling = _exec_root(args={"command": "cat", "host": "{{HOST}}"}, signal_text="悬空")
    ok = _exec_root(args={"command": "lsblk"}, match={"type": "keyword", "mode": "all", "expected": ["fail"]})
    results = evaluate_root_closure([dangling, ok], [], capability_catalog=CATALOG)
    accepted, rejected = split_accepted_and_rejected([dangling, ok], results)
    assert accepted == [ok]
    assert len(rejected) == 1
    cand = rejected[0]
    # 与 signals_json.rejected_candidates 同构：{candidate, reason, reason_code}
    assert {"candidate", "reason", "reason_code"} <= set(cand)
    assert cand["candidate"] is dangling
    assert cand["reason_code"] == "ROOT_CLOSURE_DANGLING"
    # 决策②：拒绝候选不等于降级 text——candidate 仍是原 executable 形态，未被转 text
    assert cand["candidate"]["match_type"] == "executable"
    assert "悬空" in cand["reason"]


# ─── adjudicate_root_closure（PR-3 抽取裁决：bootstrap 守卫 + 硬拒 + 全拒 + 回喂）───────
def _host_root() -> dict:
    return _exec_root(args={"command": "df", "command_args": ["-h", "/run/lock"], "host": "{{HOST}}"})


def test_adjudicate_closed_with_producer_retained() -> None:
    res = adjudicate_root_closure([_host_root()], [_qkv_producer("s1", ["HOST"])])
    assert res["has_context"] is True
    assert res["hard_reject_count"] == 0
    assert len(res["accepted"]) == 1
    assert res["accepted"][0]["closure_status"] == CLOSURE_CLOSED
    assert res["rejected"] == []
    assert res["feedback"] == ""
    assert res["all_rejected"] is False


def test_adjudicate_dangling_without_context_is_bootstrap_kept() -> None:
    # 关键轨空（无 produces 上下文）：显式 {{HOST}} 悬空但走 bootstrap 保守保留，不硬拒、标 needs_review
    res = adjudicate_root_closure([_host_root()], [])
    assert res["has_context"] is False
    assert res["hard_reject_count"] == 0
    assert res["bootstrap_kept"] == [1]
    assert len(res["accepted"]) == 1
    assert res["accepted"][0]["closure_status"] == CLOSURE_DANGLING
    assert res["accepted"][0]["provenance"]["needs_review"] is True
    assert res["rejected"] == []
    # bootstrap 保留不算全拒（避免无关键轨时误报 ROOT_SIGNAL_ALL_REJECTED）
    assert res["all_rejected"] is False


def test_adjudicate_dangling_with_context_is_hard_rejected() -> None:
    # 关键轨有 produces（上下文存在）却不产 HOST → 真悬空 → 硬拒（移出 accepted，入 rejected）
    res = adjudicate_root_closure([_host_root()], [_qkv_producer("s1", ["ETHX"])])
    assert res["has_context"] is True
    assert res["hard_reject_count"] == 1
    assert res["accepted"] == []
    assert len(res["rejected"]) == 1
    assert res["rejected"][0]["reason_code"] == "ROOT_CLOSURE_DANGLING"
    assert res["rejected"][0]["candidate"]["match_type"] == "executable"  # 决策②：未转 text
    assert res["all_rejected"] is True
    assert "HOST" in res["feedback"]


def test_adjudicate_literal_hard_rejected_regardless_of_context() -> None:
    # 含现场字面量（eth0）与上下文无关 → 即使无关键轨上下文也恒硬拒
    root = _exec_root(args={"command": "ethtool", "command_args": ["-k", "eth0"]})
    res = adjudicate_root_closure([root], [])
    assert res["hard_reject_count"] == 1
    assert res["rejected"][0]["reason_code"] == "ROOT_CLOSURE_LITERAL_NOT_PARAMETERIZED"
    assert res["all_rejected"] is True
    assert "{{" in res["feedback"]  # 反馈含标准变量占位提示
