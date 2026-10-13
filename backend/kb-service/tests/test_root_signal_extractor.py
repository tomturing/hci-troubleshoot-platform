"""根因信号抽取核心模块单测（test_root_signal_extractor）。

覆盖：
1. 「一 KBD 一信号」门禁：多条拒收、非对象拒收；
2. executable 门禁：词表外 tool 拒收、qfk_var 拒收、写操作命令拒收、args 契约拒收；
3. text 门禁：关键词过短拒收、不可溯源降级 needs_review（不拒收）、携带 acquire 拒收；
4. extract_root_signal 重试链路：门禁失败带原因回喂一次、仍失败返回 None（不伪造）；
5. golden 端到端归一化（executable / text 各一条，经真实种子 SQL 模板渲染）；
6. 冲突计数与 Prompt 种子占位符契约（与 StrictPromptLoader 同法校验）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.root_signal_extractor import (
    ROOT_SIGNAL_PLACEHOLDERS,
    build_root_signal_prompt,
    count_root_cause_conflicts,
    extract_root_signal,
    gate_single_signal,
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


def _normalize(raw: dict, root_cause: str = _ROOT_CAUSE) -> tuple[dict | None, str | None]:
    return normalize_root_signal(raw, root_cause=root_cause, category_id="虚拟机-001", prompt_revision="sha256:test")


# ─── 1. 单信号门禁 ────────────────────────────────────────────────────────────

class TestGateSingleSignal:
    def test_rejects_multiple_signals(self):
        _, error = gate_single_signal({"signals": [_raw(), _raw()]})
        assert error and "1 条" in error

    def test_accepts_signals_array_of_one(self):
        raw, error = gate_single_signal({"signals": [_raw()]})
        assert error is None and raw is not None

    def test_accepts_signal_object(self):
        raw, error = gate_single_signal({"signal": _raw()})
        assert error is None and raw["match_type"] == "text"

    def test_rejects_non_dict(self):
        _, error = gate_single_signal(["not", "a", "dict"])
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
        _, error = _normalize(_executable_raw(tool="qfk_magic"), root_cause="分区使用率100%导致加锁失败")
        assert error and "不在采集器词表内" in error

    def test_qkv_tool_rejected(self):
        _, error = _normalize(_executable_raw(tool="qkv_alert"), root_cause="分区使用率100%")
        assert error and "qfk_*" in error

    def test_qfk_var_rejected(self):
        _, error = _normalize(_executable_raw(tool="qfk_var"), root_cause="分区使用率100%")
        assert error and "qfk_var" in error

    def test_write_command_rejected(self):
        # kill 命中写操作词表，应被只读安全门拒绝（而非 args 契约）
        _, error = _normalize(
            _executable_raw(command="kill"), root_cause="分区使用率100%导致加锁失败"
        )
        assert error and "只读安全门" in error

    def test_missing_required_arg_rejected(self):
        raw = _executable_raw()
        raw["acquire"]["args"] = {"host": "{{HOST}}"}  # qfk_system 必填 command
        _, error = _normalize(raw, root_cause="分区使用率100%")
        assert error and "command" in error

    def test_valid_executable_passes(self):
        signal, error = _normalize(_executable_raw(), root_cause="分区使用率100%导致加锁失败")
        assert error is None
        assert signal is not None and signal["acquire"]["tool"] == "qfk_system"

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

    def test_non_root_cause_source_section_rejected(self):
        raw = _raw(provenance={**_raw()["provenance"], "source_section": "steps_text"})
        _, error = _normalize(raw)
        assert error and "root_cause" in error


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
}


class TestExtractPipeline:
    async def test_golden_text_signal_end_to_end(self, prompt_template):
        llm, _ = _fake_llm({"signal": _raw()})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert outcome["error"] is None and outcome["attempts"] == 1
        signal = outcome["signal"]
        assert signal["schema_version"] == 1 and signal["role"] == "root_cause"
        assert signal["provenance"]["method"] == "llm_root_signal_v1"
        assert signal["provenance"]["source_category_id"] == "虚拟机-001"
        assert signal["prompt_revision"].startswith("sha256:")

    async def test_gate_failure_retries_once_with_feedback(self, prompt_template):
        bad = {"signals": [_raw(), _raw()]}
        llm, calls = _fake_llm(bad, {"signal": _raw()})
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert outcome["signal"] is not None and outcome["attempts"] == 2
        # 第二轮 messages 必须携带门禁失败原因回喂
        assert any("未通过服务端门禁" in m["content"] for m in calls[1])

    async def test_persistent_failure_returns_none_without_fabrication(self, prompt_template):
        llm, calls = _fake_llm(ValueError("boom"), ValueError("boom"))
        outcome = await extract_root_signal(ENTRY, prompt_template=prompt_template, llm_call=llm)
        assert outcome["signal"] is None and outcome["attempts"] == 2
        assert "LLM 调用失败" in (outcome["error"] or "")

    async def test_prompt_rendering_embeds_root_cause_and_catalog(self, prompt_template):
        rendered = build_root_signal_prompt(
            prompt_template,
            {**{k: ENTRY.get(k, "") for k in ROOT_SIGNAL_PLACEHOLDERS}, "acquirer_catalog": "qfk_log: 日志"},
        )
        assert _ROOT_CAUSE in rendered and "qfk_log" in rendered and "有且仅有一条" in rendered


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
