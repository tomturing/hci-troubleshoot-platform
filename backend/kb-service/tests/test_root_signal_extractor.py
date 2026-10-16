"""根因信号抽取核心模块单测（test_root_signal_extractor）。

v2 覆盖：
1. 多信号门禁：接受 1..N 条数组、去重、封顶；空集拒收、非对象拒收；
2. executable 门禁：词表外 tool 拒收、qfk_var 拒收、写操作命令拒收、args 契约拒收；
3. text 门禁：关键词过短拒收、不可溯源降级 needs_review（不拒收）、携带 acquire 拒收；
4. 溯源分层：evidence 命中 root_cause=主锚、仅命中整篇=content 兑底（强制 needs_review）、整篇都溯源不到=拒收；
5. extract_root_signal 重试链路：主锚优先、无主锚才用兑底、部分门禁失败只丢单条；
6. golden 端到端归一化 + 冲突计数 + Prompt 种子占位符契约（与 StrictPromptLoader 同法校验）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.root_signal_extractor import (
    ROOT_SIGNAL_PLACEHOLDERS,
    build_root_signal_prompt,
    count_root_cause_conflicts,
    extract_root_signal,
    gate_signals,
    load_prompt_template_from_sql,
    normalize_root_signal,
)

_ROOT_CAUSE = (
    "由于check_redis_server_status.sh.lock文件持续增长至8M，占满/run/lock分区（使用率100%），"
    "导致脚本执行加锁失败，界面修改admin密码后回调脚本无法加锁。"
)


def _raw(**overrides) -> dict:
    base = {
        "match_type": "text",
        "signal_text": "/run/lock 分区被 lock 文件占满",
        "acquire": None,
        "match": {
            "type": "keyword",
            "mode": "all",
            "expected": ["check_redis_server_status.sh.lock", "/run/lock"],
        },
        "provenance": {
            "source_section": "root_cause",
            "evidence": "占满/run/lock分区（使用率100%）",
            "confidence": 0.9,
            "needs_review": False,
        },
    }
    base.update(overrides)
    return base


_CONTENT = _ROOT_CAUSE + "解决方案：删除残留 lock 文件并扩容 /run/lock 分区。"


def _normalize(raw: dict, root_cause: str = _ROOT_CAUSE, content: str | None = None) -> tuple[dict | None, str | None]:
    signal, error, _tier = normalize_root_signal(
        raw,
        root_cause=root_cause,
        content=content if content is not None else root_cause,
        category_id="虚拟机-001",
        prompt_revision="sha256:test",
    )
    return signal, error


# ─── 1. 多信号门禁 ────────────────────────────────────────────────────────────

class TestGateSignals:
    def test_accepts_multiple_signals(self):
        raws, error = gate_signals({"signals": [_raw(), _raw(signal_text="另一条定位信号特性")]})
        assert error is None and len(raws) == 2

    def test_dedups_by_signal_text(self):
        dup = _raw()
        raws, error = gate_signals({"signals": [dup, dict(dup)]})
        assert error is None and len(raws) == 1

    def test_caps_at_max_signals(self):
        signals = [_raw(signal_text=f"特性结论 {i}") for i in range(20)]
        raws, error = gate_signals({"signals": signals}, max_signals=8)
        assert error is None and len(raws) == 8

    def test_accepts_signals_array_of_one(self):
        raws, error = gate_signals({"signals": [_raw()]})
        assert error is None and len(raws) == 1

    def test_accepts_signal_object(self):
        raws, error = gate_signals({"signal": _raw()})
        assert error is None and raws[0]["match_type"] == "text"

    def test_rejects_empty_array(self):
        _, error = gate_signals({"signals": []})
        assert error is not None

    def test_rejects_non_dict(self):
        _, error = gate_signals(["not", "a", "dict"])
        assert error is not None


# ─── 2. executable 门禁 ───────────────────────────────────────────────────────

def _executable_raw(tool: str = "qfk_system", command: str = "df -h") -> dict:
    return {
        "match_type": "executable",
        "signal_text": "/run/lock 分区使用率 100%",
        "acquire": {"tool": tool, "args": {"command": command, "host": "{{HOST}}"}},
        "match": {"type": "keyword", "mode": "all", "expected": ["100%"]},
        "provenance": {"source_section": "root_cause", "evidence": "占满/run/lock分区", "confidence": 0.8},
    }


class TestExecutableGate:
    def test_tool_outside_catalog_rejected(self):
        _, error = _normalize(_executable_raw(tool="qfk_magic"))
        assert error and "不在采集器词表内" in error

    def test_qkv_tool_rejected(self):
        _, error = _normalize(_executable_raw(tool="qkv_alert"))
        assert error and "qfk_*" in error

    def test_qfk_var_rejected(self):
        _, error = _normalize(_executable_raw(tool="qfk_var"))
        assert error and "qfk_var" in error

    def test_write_command_rejected(self):
        # kill 命中写操作词表，应被只读安全门拒绝（而非 args 契约）
        _, error = _normalize(_executable_raw(command="kill"))
        assert error and "只读安全门" in error

    def test_missing_required_arg_rejected(self):
        raw = _executable_raw()
        raw["acquire"]["args"] = {"host": "{{HOST}}"}  # qfk_system 必填 command
        _, error = _normalize(raw)
        assert error and "command" in error

    def test_valid_executable_passes(self):
        signal, error = _normalize(_executable_raw())
        assert error is None
        assert signal is not None and signal["acquire"]["tool"] == "qfk_system"
        assert signal["provenance"]["trace_tier"] == "root_cause"

    def test_qfk_log_fullpath_file_coerced_to_basename(self):
        # v3 试点回归：模型惯性写全路径，服务端无损降级 basename 而非烧轮次拒收
        raw = {
            "match_type": "executable",
            "signal_text": "syslog 反复报 Kernel panic",
            "acquire": {"tool": "qfk_log", "args": {"file": "/var/log/messages"}},
            "match": {"type": "keyword", "mode": "all", "expected": ["Kernel panic"]},
            "provenance": {"source_section": "root_cause", "evidence": "Kernel panic", "confidence": 0.8},
        }
        signal, error = _normalize(raw, root_cause="系统日志 messages 中反复出现 Kernel panic")
        assert error is None
        assert signal is not None and signal["acquire"]["args"]["file"] == "messages"

    def test_executable_unsafe_file_still_rejected(self):
        # 归一化不降门禁：basename 降级后仍非法（空 basename）照样被 args 契约拒
        raw = {
            "match_type": "executable",
            "signal_text": "异常日志文件",
            "acquire": {"tool": "qfk_log", "args": {"file": "/"}},
            "match": {"type": "keyword", "mode": "all", "expected": ["error"]},
            "provenance": {"source_section": "root_cause", "evidence": "error", "confidence": 0.8},
        }
        _, error = _normalize(raw, root_cause="error 反复出现")
        assert error is not None


# ─── 3. text 门禁 ─────────────────────────────────────────────────────────────

class TestTextGate:
    def test_short_keyword_rejected(self):
        raw = _raw(match={"type": "keyword", "mode": "all", "expected": ["锁"]})
        _, error = _normalize(raw)
        assert error and "过短" in error

    def test_untraceable_keyword_downgrades_to_needs_review(self):
        raw = _raw(match={"type": "keyword", "mode": "all", "expected": ["内存条金手指氧化"]})
        signal, error = _normalize(raw)
        assert error is None
        assert signal is not None and signal["provenance"]["needs_review"] is True

    def test_traceable_keywords_keep_needs_review_false(self):
        signal, error = _normalize(_raw())
        assert error is None
        assert signal is not None and signal["provenance"]["needs_review"] is False

    def test_text_with_acquire_rejected(self):
        raw = _raw(acquire={"tool": "qfk_system", "args": {"command": "df"}})
        _, error = _normalize(raw)
        assert error and "不允许携带 acquire" in error

    def test_nested_array_keywords_flattened(self):
        # 试点回归：LLM 返回嵌套数组时不得把 list repr 当关键词落盘
        raw = _raw(match={"type": "keyword", "mode": "all", "expected": [["/run/lock", "check_redis_server_status.sh.lock"]]})
        signal, error = _normalize(raw)
        assert error is None
        assert signal is not None
        assert signal["match"]["expected"] == ["/run/lock", "check_redis_server_status.sh.lock"]

    def test_duplicate_keywords_deduped(self):
        raw = _raw(match={"type": "keyword", "mode": "all", "expected": ["/run/lock", "/RUN/lock", "/run/lock"]})
        signal, error = _normalize(raw)
        assert error is None
        assert signal is not None and signal["match"]["expected"] == ["/run/lock"]

    def test_pure_numeric_keyword_rejected(self):
        # 试点回归：「0.95」类纯数字关键词无区分度，必须拒收令模型重选
        raw = _raw(match={"type": "keyword", "mode": "all", "expected": ["0.95"]})
        _, error = _normalize(raw)
        assert error and "纯数字" in error

    def test_invalid_source_section_rejected(self):
        # v2：source_section 开放为章节枚举，枚举外的值仍拒收
        raw = _raw(provenance={**_raw()["provenance"], "source_section": "nonexistent_section"})
        _, error = _normalize(raw)
        assert error and "source_section" in error

    def test_untraceable_evidence_rejected(self):
        # 整篇都无法逐字溯源的 evidence 即拒收（绝不伪造），主锚/兑底均不命中
        raw = _raw(provenance={**_raw()["provenance"], "evidence": "完全不存在于本文的句子XYZ"})
        _, error = _normalize(raw, root_cause=_ROOT_CAUSE, content=_CONTENT)
        assert error and "无法逐字溯源" in error


# ─── 4/5. 抽取流水线（fake llm_call）─────────────────────────────────────────

def _fake_llm(*payloads):
    """按次序返回 payload / 抛异常，并记录每轮 messages。"""
    calls: list[list[dict]] = []

    async def _call(messages):
        calls.append(messages)
        item = payloads[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return _call, calls


@pytest.fixture(scope="module")
def prompt_template() -> str:
    """Prompt 种子事实源：database/seeds/02_system_prompts.sql（由 db-seed-job 自动应用）。"""
    sql = (Path(__file__).resolve().parents[3] / "database/seeds/02_system_prompts.sql").read_text(
        encoding="utf-8"
    )
    return load_prompt_template_from_sql(sql)


ENTRY = {
    "support_id": "41480",
    "title": "【HCI】修改admin密码后root密码未同步",
    "category_id": "虚拟机-001",
    "category_path": "HCI/虚拟机/密码管理",
    "root_cause": _ROOT_CAUSE,
    "kbd_content": _CONTENT,
}


def _content_only_raw() -> dict:
    """evidence/关键词仅能在整篇兑底章节（solution）逐字命中，root_cause 中不存在。"""
    return {
        "match_type": "text",
        "signal_text": "残留 lock 文件需删除",
        "acquire": None,
        "match": {"type": "keyword", "mode": "all", "expected": ["删除残留 lock 文件"]},
        "provenance": {
            "source_section": "solution",
            "evidence": "删除残留 lock 文件",
            "confidence": 0.6,
            "needs_review": False,
        },
    }


class TestExtractPipeline:
    async def test_golden_text_signal_end_to_end(self, prompt_template):
        llm, _ = _fake_llm({"signal": _raw()})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert outcome["error"] is None and outcome["attempts"] == 1
        assert len(outcome["signals"]) == 1
        signal = outcome["signals"][0]
        assert signal["schema_version"] == 2 and signal["role"] == "root_cause"
        assert signal["provenance"]["method"] == "llm_root_signal_v2"
        assert signal["provenance"]["trace_tier"] == "root_cause"
        assert signal["provenance"]["source_category_id"] == "虚拟机-001"
        assert signal["prompt_revision"].startswith("sha256:")

    async def test_multiple_signals_all_kept(self, prompt_template):
        second = _raw(signal_text="lock 文件持续增长至 8M", match={"type": "keyword", "mode": "all", "expected": ["check_redis_server_status.sh.lock"]})
        llm, _ = _fake_llm({"signals": [_raw(), second]})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert outcome["error"] is None and len(outcome["signals"]) == 2

    async def test_primary_anchor_preferred_over_fallback(self, prompt_template):
        # 同时有主锚与兑底：只采用主锚，不用兑底填充（防噪声稀释）
        llm, _ = _fake_llm({"signals": [_raw(), _content_only_raw()]})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert len(outcome["signals"]) == 1
        assert outcome["signals"][0]["provenance"]["trace_tier"] == "root_cause"

    async def test_fallback_only_adopted_with_needs_review(self, prompt_template):
        # 无主锚时才采用整篇兑底，并强制 needs_review
        llm, _ = _fake_llm({"signals": [_content_only_raw()]})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert len(outcome["signals"]) == 1
        sig = outcome["signals"][0]
        assert sig["provenance"]["trace_tier"] == "content" and sig["provenance"]["needs_review"] is True

    async def test_partial_gate_failure_drops_only_bad_signal(self, prompt_template):
        # 部分成功：一条非法 + 一条合法 → 只丢非法条，保留合法条（不整篇失败）
        bad = {"match_type": "bogus", "signal_text": "x"}
        llm, _ = _fake_llm({"signals": [bad, _raw()]})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert len(outcome["signals"]) == 1 and outcome["signals"][0]["schema_version"] == 2
        assert outcome["gate_errors"]  # 非法条被记录

    async def test_gate_failure_retries_once_with_feedback(self, prompt_template):
        bad = {"signals": [{"match_type": "bogus", "signal_text": "x"}]}
        llm, calls = _fake_llm(bad, {"signal": _raw()})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert outcome["signals"] and outcome["attempts"] == 2
        # 第二轮 messages 必须携带门禁失败原因回喂
        assert any("未通过服务端门禁" in m["content"] for m in calls[1])

    async def test_persistent_failure_returns_empty_without_fabrication(self, prompt_template):
        llm, calls = _fake_llm(ValueError("boom"), ValueError("boom"))
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert outcome["signals"] == [] and outcome["attempts"] == 2
        assert "LLM 调用失败" in (outcome["error"] or "")

    async def test_prompt_rendering_embeds_content_and_catalog(self, prompt_template):
        rendered = build_root_signal_prompt(
            prompt_template,
            {**{k: ENTRY.get(k, "") for k in ROOT_SIGNAL_PLACEHOLDERS}, "acquirer_catalog": "qfk_log: 日志"},
        )
        assert _ROOT_CAUSE in rendered and "qfk_log" in rendered and "不限条数" in rendered


# ─── 6. 冲突计数与模板契约 ────────────────────────────────────────────────────

class TestConflictsAndTemplate:
    def test_conflict_counting_excludes_self_and_counts_category(self):
        signal, error = _normalize(_raw())
        assert error is None and signal is not None
        corpus = {
            "41480": (_ROOT_CAUSE, "虚拟机-001"),  # 自身，排除
            "11111": (_ROOT_CAUSE, "虚拟机-001"),  # 同分类冲突
            "22222": ("完全无关的根因描述", "虚拟机-006"),
        }
        result = count_root_cause_conflicts(signal, corpus, self_support_id="41480")
        assert result == {"category_conflicts": 1, "global_conflicts": 1}

    def test_executable_conflict_requires_resource_object_hit(self):
        # v3 试点回归：executable 判定面 = expected ∧ 采集具体资源；
        # 另一篇根因只含通用判定串 "100%" 而无 /run/lock 路径，不得计为冲突
        raw = {
            "match_type": "executable",
            "signal_text": "/run/lock 分区使用率 100%",
            "acquire": {"tool": "qfk_system", "args": {"command": "df", "command_args": ["-h", "/run/lock"], "host": "{{HOST}}"}},
            "match": {"type": "keyword", "mode": "all", "expected": ["100%"]},
            "provenance": {"source_section": "root_cause", "evidence": "占满/run/lock分区", "confidence": 0.8},
        }
        signal, error = _normalize(raw, root_cause=_ROOT_CAUSE)
        assert error is None and signal is not None
        corpus = {
            "41480": (_ROOT_CAUSE, "虚拟机-001"),  # 自身，排除
            "99999": ("数据盘使用率达到100%导致写入失败", "虚拟机-006"),  # 仅判定串重合，非冲突
            "88888": ("/run/lock 分区使用率100%引发加锁失败", "虚拟机-001"),  # 同资源同判定，真冲突
        }
        result = count_root_cause_conflicts(signal, corpus, self_support_id="41480")
        assert result == {"category_conflicts": 1, "global_conflicts": 1}

    def test_seed_sql_placeholder_contract(self, prompt_template):
        from string import Formatter

        actual = {name for _, name, _, _ in Formatter().parse(prompt_template) if name is not None}
        assert actual == set(ROOT_SIGNAL_PLACEHOLDERS)

    def test_catalog_text_carries_args_contract(self):
        # 试点回归：目录文本必须内嵌 args 契约摘要，否则 executable 型全部被迫退化 text
        from app.services.root_signal_extractor import acquirer_catalog_text

        text = acquirer_catalog_text()
        assert "qfk_log" in text and "args 契约" in text
        assert "command*" in text  # qfk_system 必填字段带 * 标记
        assert "qfk_var" not in text
