"""根因信号生产链路单测：热加载契约、门禁失败不写库、冲突度实测、批量处理器与鉴权。

与抽取内核单测（test_root_signal_extractor.py）的分工：
- 内核单测管「契约与门禁语义」；
- 本文件管「路由装配」：Prompt 热加载是否真的每次调用、失败是否污染落库、
  冲突度是否按现场语料实测、批量处理器是否把业务失败如实抛给 kbd_batch_job_item。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.config import settings
from app.routes import extract_root_signal as route
from app.services.root_signal_extractor import (
    ROOT_SIGNAL_PLACEHOLDERS,
    ROOT_SIGNAL_PROMPT_NAME,
    ConflictCorpus,
    load_prompt_template_from_sql,
)
from fastapi import HTTPException, Request
from shared.utils.prompt_loader import PromptLoadError

REPO_ROOT = Path(__file__).resolve().parents[3]

# 现场语料：同一 /run/lock 分区满特性被另一篇 KBD 共用 → global_conflicts 应为 1
_CORPUS_ROWS = [
    ("41480", "admin密码修改后root密码未同步，/etc/shadow 中残留旧密码哈希", "虚拟机-001"),
    ("99999", "df 显示 /run/lock 分区使用率 100% 占满", "虚拟机-009"),
]

_EXECUTABLE_PAYLOAD = {
    "signal": {
        "match_type": "executable",
        "signal_text": "/run/lock 分区被占满导致虚拟机任务失败",
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "df", "command_args": ["-h", "/run/lock"], "host": "{{HOST}}"}},
        "match": {"type": "keyword", "mode": "all", "expected": ["100%"]},
        "provenance": {
            "source_section": "root_cause",
            "evidence": "使用率 100%",
            "confidence": 0.9,
            "needs_review": False,
        },
    }
}

_TEXT_PAYLOAD = {
    "signal": {
        "match_type": "text",
        "signal_text": "shadow 残留旧密码哈希",
        "acquire": None,
        "match": {"type": "keyword", "mode": "all", "expected": ["/etc/shadow", "旧密码哈希"]},
        "provenance": {
            "source_section": "root_cause",
            "evidence": "/etc/shadow 中残留旧密码哈希",
            "confidence": 0.85,
            "needs_review": False,
        },
    }
}

# 依赖 {{NODE_IP}} 的可执行根因（用于回喂重试：次轮改引 producer 已产出的 NODE_IP → closed）
_EXECUTABLE_NODEIP_PAYLOAD = {
    "signal": {
        "match_type": "executable",
        "signal_text": "/run/lock 分区被占满导致虚拟机任务失败",
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "df", "command_args": ["-h", "/run/lock"], "host": "{{NODE_IP}}"}},
        "match": {"type": "keyword", "mode": "all", "expected": ["100%"]},
        "provenance": {
            "source_section": "root_cause",
            "evidence": "使用率 100%",
            "confidence": 0.9,
            "needs_review": False,
        },
    }
}


# 混合产出：1 条合法 text（accepted）+ 1 条引用未产出 {{HOST}} 的 executable（悬空硬拒）
# → 闭包层 all_rejected（仅按 executable 判）为真，但 accepted 非空，须落库 text 而非整单丢弃
# text 的 evidence/expected 逐字取自 _ENTRY_ROW.root_cause（“/run/lock 分区使用率 100%”），否则被溯源门禁丢弃
_MIXED_TEXT_AND_DANGLING_PAYLOAD = {
    "signals": [
        {
            "match_type": "text",
            "signal_text": "/run/lock 分区被占满导致任务失败",
            "acquire": None,
            "match": {"type": "keyword", "mode": "all", "expected": ["/run/lock", "占满导致虚拟机任务失败"]},
            "provenance": {
                "source_section": "root_cause",
                "evidence": "/run/lock 分区使用率 100%，占满导致虚拟机任务失败",
                "confidence": 0.85,
                "needs_review": False,
            },
        },
        _EXECUTABLE_PAYLOAD["signal"],
    ]
}


# 两条可执行根因：弱判据 {"100%"} 是强判据 {"100%", "/run/lock"} 的严格子集 → L3 必剪弱留强。
# 两条 evidence 均可逐字溯源到 _ENTRY_ROW.root_cause，故均过形状/溯源门禁，只由最少性决定去留。
_MINIMALITY_PAYLOAD = {
    "signals": [
        {
            "match_type": "executable",
            "signal_text": "/run/lock 分区使用率 100% 且分区被占满",
            "acquire": {
                "tool": "qfk_system",
                "args": {"command": "df", "command_args": ["-h", "/run/lock"], "host": "{{HOST}}"},
            },
            "match": {"type": "keyword", "mode": "all", "expected": ["100%", "/run/lock"]},
            "provenance": {
                "source_section": "root_cause",
                "evidence": "df 显示 /run/lock 分区使用率 100%",
                "confidence": 0.9,
                "needs_review": False,
            },
        },
        {
            "match_type": "executable",
            "signal_text": "/run/lock 分区使用率 100%",
            "acquire": {
                "tool": "qfk_system",
                "args": {"command": "df", "command_args": ["-h", "/run/lock"], "host": "{{HOST}}"},
            },
            "match": {"type": "keyword", "mode": "all", "expected": ["100%"]},
            "provenance": {
                "source_section": "root_cause",
                "evidence": "分区使用率 100%",
                "confidence": 0.9,
                "needs_review": False,
            },
        },
    ]
}


def _producer(produces: list[str]) -> dict[str, Any]:
    """关键轨诊断生产者（qkv_task 形态）：orchestrate.produces 提供闭包可用变量池。"""
    return {
        "id": "s_producer",
        "acquire": {"tool": "qkv_task", "args": {"keyword": "取节点IP", "is_failed": True}},
        "match": None,
        "orchestrate": {"produces": [{"name": n} for n in produces]},
    }


_ENTRY_ROW = {
    "id": 1,
    "support_id": "41480",
    "title": "【HCI】修改admin密码后root密码未同步",
    "root_cause": "df 显示 /run/lock 分区使用率 100%，占满导致虚拟机任务失败",
    "content_md": "## 问题描述\nadmin 密码同步失败\n\n## 根因\ndf 显示 /run/lock 分区使用率 100%，占满导致虚拟机任务失败\n\n## 解决方案\n清理残留 lock 文件并扩容分区",
    "category_id": "虚拟机-009",
    "ai_category_id": None,
    "signals_json": [],  # 关键轨未抽（无 produces 上下文）：闭包 bootstrap 守卫保留可执行根因为 needs_review，不硬拒
    "status": "published",
}


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """按语句文本分流的最小 session（select 条目 / UPDATE 落库 / commit 计数）。"""

    def __init__(self, *, entry_row: dict[str, Any] | None, updates: list[dict[str, Any]]) -> None:
        self._entry_row = entry_row
        self._updates = updates
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        if "UPDATE kbd_entry" in sql:
            self._updates.append({"sql": sql, "params": params or {}})
            return _Result([])
        if "FROM kbd_entry" in sql:
            return _Result([] if self._entry_row is None else [self._entry_row])
        return _Result([])

    async def scalar(self, stmt: Any) -> None:
        # 分类路径查询返回 None：走「分类码即路径」回退分支
        return None

    async def commit(self) -> None:
        self.commits += 1


def _db_factory(entry_row: dict[str, Any] | None, updates: list[dict[str, Any]]) -> MagicMock:
    db = MagicMock()
    db.async_session_factory = lambda: _FakeSession(entry_row=entry_row, updates=updates)
    return db


def _make_request(method: str = "POST", path: str = "/api/admin/kbd/1/extract-root-signal") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"authorization", f"Bearer {settings.INTERNAL_API_TOKEN}".encode())],
        }
    )


@pytest.fixture()
def prompt_template() -> str:
    return load_prompt_template_from_sql(
        (REPO_ROOT / "database/seeds/02_system_prompts.sql").read_text(encoding="utf-8")
    )


@pytest.fixture()
def corpus() -> ConflictCorpus:
    return ConflictCorpus(_CORPUS_ROWS)


async def _stub_loader(monkeypatch: pytest.MonkeyPatch, template: str) -> dict[str, Any]:
    """把 StrictPromptLoader 换成可控实现，同时记录调用参数以断言「每次热加载」。"""
    calls: dict[str, Any] = {}

    async def _load(session: Any, name: str, expected: list[str], **kwargs: Any) -> str:
        calls["name"] = name
        calls["expected"] = list(expected)
        calls["consumer"] = kwargs.get("consumer")
        return template

    monkeypatch.setattr(route.StrictPromptLoader, "load_and_validate", staticmethod(_load))
    return calls


class TestExtractForKbd:
    async def test_executable_signal_persisted_with_live_conflicts(
        self, monkeypatch, prompt_template, corpus
    ):
        updates: list[dict[str, Any]] = []
        db = _db_factory(_ENTRY_ROW, updates)
        calls = await _stub_loader(monkeypatch, prompt_template)

        async def _llm(
            messages: list[dict[str, str]], *, prompt_revision: str, kbd_id: int, attempt: int
        ) -> dict[str, Any]:
            return _EXECUTABLE_PAYLOAD

        monkeypatch.setattr(route, "_call_llm_messages", _llm)

        result = await route.extract_root_signal_for_kbd(db, 1, corpus=corpus)

        assert result["success"] is True
        assert result["match_type"] == "executable"
        assert result["signals_count"] == 1
        # Prompt 按名字与占位符契约每次热加载（admin 改模板即生效）
        assert calls["name"] == ROOT_SIGNAL_PROMPT_NAME
        assert calls["expected"] == list(ROOT_SIGNAL_PLACEHOLDERS)
        assert calls["consumer"] == route.CONSUMER
        # 落库只动 root_signal_json 一列（v2 顶层数组）
        assert len(updates) == 1
        assert "root_signal_json" in updates[0]["sql"] and "content_md" not in updates[0]["sql"]
        signals = json.loads(updates[0]["params"]["signals"])
        assert isinstance(signals, list) and len(signals) == 1
        signal = signals[0]
        assert signal["schema_version"] == 2 and signal["role"] == "root_cause"
        assert signal["provenance"]["trace_tier"] == "root_cause"
        # 冲突度来自现场语料实测，不是 LLM 自报值
        assert signal["distinctiveness"] == {"category_conflicts": 1, "global_conflicts": 1}
        assert signal["provenance"]["source_sha256"]

    async def test_text_signal_persisted_and_review_flag_kept(self, monkeypatch, prompt_template, corpus):
        updates: list[dict[str, Any]] = []
        db = _db_factory(
            {**_ENTRY_ROW, "root_cause": "/etc/shadow 中残留旧密码哈希，导致 ssh 密钥认证失败"},
            updates,
        )
        await _stub_loader(monkeypatch, prompt_template)

        async def _llm(
            messages: list[dict[str, str]], *, prompt_revision: str, kbd_id: int, attempt: int
        ) -> dict[str, Any]:
            return _TEXT_PAYLOAD

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=corpus)

        assert result["success"] is True and result["match_type"] == "text"
        assert result["needs_review"] is False
        assert json.loads(updates[0]["params"]["signals"])[0]["acquire"] is None

    async def test_gate_reject_writes_nothing_and_keeps_previous_signal(
        self, monkeypatch, prompt_template, corpus
    ):
        """对抗性核心：门禁连拒两次时既不写空、也不清空既有信号。"""
        updates: list[dict[str, Any]] = []
        db = _db_factory(_ENTRY_ROW, updates)
        await _stub_loader(monkeypatch, prompt_template)
        payloads = iter(
            [
                {"signal": {"match_type": "text", "signal_text": "x", "acquire": None,
                            "match": {"type": "keyword", "mode": "all", "expected": ["0.95"]},
                            "provenance": {"source_section": "root_cause", "evidence": "e", "confidence": 0.5}}},
                "not-a-dict",
            ]
        )

        async def _llm(
            messages: list[dict[str, str]], *, prompt_revision: str, kbd_id: int, attempt: int
        ) -> Any:
            return next(payloads)

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=corpus)

        assert result["success"] is False
        assert result["attempts"] == 2
        assert result["signals"] == []
        assert updates == []

    async def test_prompt_hot_load_failure_blocks_extraction(self, monkeypatch, corpus):
        """Loader 抛错必须原样阻断（不降级到内置模板、不落库）。"""
        updates: list[dict[str, Any]] = []
        db = _db_factory(_ENTRY_ROW, updates)

        async def _boom(*args: Any, **kwargs: Any) -> str:
            raise PromptLoadError("system_prompt 表中未找到 kbd_extract_root_signal_v1")

        monkeypatch.setattr(route.StrictPromptLoader, "load_and_validate", staticmethod(_boom))
        with pytest.raises(PromptLoadError):
            await route.extract_root_signal_for_kbd(db, 1, corpus=corpus)
        assert updates == []

    async def test_missing_entry_returns_404(self, monkeypatch, prompt_template):
        updates: list[dict[str, Any]] = []
        db = _db_factory(None, updates)
        await _stub_loader(monkeypatch, prompt_template)
        with pytest.raises(HTTPException) as exc:
            await route.extract_root_signal_for_kbd(db, 404)
        assert exc.value.status_code == 404

    async def test_short_root_cause_still_extracts_from_content(self, monkeypatch, prompt_template):
        """v2 已取消根因长度硬门：根因短仍可读整篇抽取；仅能溯源到整篇其余章节时降为 content 兑底并强制复核。"""
        updates: list[dict[str, Any]] = []
        db = _db_factory({**_ENTRY_ROW, "root_cause": "配置不正确"}, updates)
        await _stub_loader(monkeypatch, prompt_template)
        llm_called = {"n": 0}

        async def _llm(
            messages: list[dict[str, str]], *, prompt_revision: str, kbd_id: int, attempt: int
        ) -> dict[str, Any]:
            llm_called["n"] += 1
            return _EXECUTABLE_PAYLOAD  # evidence 只存在于 content_md（根因“配置不正确”不含）

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=ConflictCorpus([]))

        assert llm_called["n"] >= 1  # 不再在 LLM 前被 422 拦下
        assert result["success"] is True and result["signals_count"] == 1
        assert result["needs_review"] is True  # content 兑底强制人工复核
        signal = json.loads(updates[0]["params"]["signals"])[0]
        assert signal["provenance"]["trace_tier"] == "content"


class TestClosureRouteIntegration:
    """路由级闭环集成：三分支落库（closed 双列 / 全拒仅写拒绝审计不清既有 / 回喂重试）。"""

    async def test_closed_with_producer_writes_both_columns(self, monkeypatch, prompt_template, corpus):
        """关键轨 producer 产出 HOST → 信号的 {{HOST}} 有源 → closed，两列同事务均写。"""
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["HOST"])], "verification_contract": None}}
        db = _db_factory(entry, updates)
        await _stub_loader(monkeypatch, prompt_template)

        async def _llm(messages, *, prompt_revision, kbd_id, attempt):
            return _EXECUTABLE_PAYLOAD

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=corpus)

        assert result["success"] is True
        assert result["attempts"] == 1
        assert result["closure"]["all_rejected"] is False
        assert result["rejected"] == []
        assert len(updates) == 1
        sql = updates[0]["sql"]
        assert "root_signal_json = CAST" in sql and "root_signal_rejected_json = CAST" in sql
        signal = json.loads(updates[0]["params"]["signals"])[0]
        assert signal["closure_status"] == "closed"
        assert signal["provenance"]["needs_review"] is False
        # 拒绝审计快照为空数组（非 None），与 accepted 同事务写入
        assert json.loads(updates[0]["params"]["rejected"]) == []

    async def test_all_rejected_writes_rejected_only_and_keeps_previous(self, monkeypatch, prompt_template):
        """有上下文却仍悬空（producer 只产 NODE_IP、信号要 HOST）→硬拒→全拒：
        仅写 root_signal_rejected_json、绝不清空 root_signal_json，显式收口 ROOT_SIGNAL_ALL_REJECTED。"""
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["NODE_IP"])], "verification_contract": None}}
        db = _db_factory(entry, updates)
        await _stub_loader(monkeypatch, prompt_template)

        async def _llm(messages, *, prompt_revision, kbd_id, attempt):
            return _EXECUTABLE_PAYLOAD  # 两轮都悬空 HOST → 回喂重试后仍全拒

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=ConflictCorpus([]))

        assert result["success"] is False
        assert result["code"] == "ROOT_SIGNAL_ALL_REJECTED"
        assert result["attempts"] == 2  # 硬拒触发回喂重试一轮
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["reason_code"] == "ROOT_CLOSURE_DANGLING"
        # 治本约束：全拒只落拒绝审计，UPDATE 语句不含 root_signal_json 赋值（既有信号不被清空）
        assert len(updates) == 1
        sql = updates[0]["sql"]
        assert "root_signal_rejected_json = CAST" in sql
        assert "root_signal_json = CAST" not in sql
        # 决策②：拒绝候选不降级 text——candidate 仍是原 executable 形态
        assert json.loads(updates[0]["params"]["rejected"])[0]["candidate"]["match_type"] == "executable"

    async def test_all_rejected_but_text_accepted_still_persists_text(self, monkeypatch, prompt_template):
        """回归（工单 38819）：executable 全悬空致 all_rejected=True，但同时有合法 text accepted →
        不得整单判失败丢弃 text；应落库 root_signal_json=[text] 且写拒绝审计，success=True。"""
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["NODE_IP"])], "verification_contract": None}}
        db = _db_factory(entry, updates)
        await _stub_loader(monkeypatch, prompt_template)

        async def _llm(messages, *, prompt_revision, kbd_id, attempt):
            return _MIXED_TEXT_AND_DANGLING_PAYLOAD  # 每轮都产出 text + 悬空 HOST executable

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=ConflictCorpus([]))

        # 闭包层仍判 executable 全拒（all_rejected=True），但 accepted 有 text → 路由不再走失败收口
        assert result["success"] is True
        assert result["closure"]["all_rejected"] is True
        assert [s["match_type"] for s in result["signals"]] == ["text"]
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["reason_code"] == "ROOT_CLOSURE_DANGLING"
        # 成功分支同事务写两列：accepted text 落 root_signal_json，悬空 executable 落拒绝审计
        assert len(updates) == 1
        sql = updates[0]["sql"]
        assert "root_signal_json = CAST" in sql and "root_signal_rejected_json = CAST" in sql
        saved = json.loads(updates[0]["params"]["signals"])
        assert len(saved) == 1 and saved[0]["match_type"] == "text"
        # 决策②：拒绝候选不降级 text——candidate 仍是原 executable 形态
        assert json.loads(updates[0]["params"]["rejected"])[0]["candidate"]["match_type"] == "executable"

    async def test_feedback_retry_resolves_on_second_attempt(self, monkeypatch, prompt_template, corpus):
        """首轮 {{HOST}} 无源硬拒 → 回喂重试；次轮改引 {{NODE_IP}}（producer 已产）→ closed，attempts==2。"""
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["NODE_IP"])], "verification_contract": None}}
        db = _db_factory(entry, updates)
        await _stub_loader(monkeypatch, prompt_template)
        payloads = iter([_EXECUTABLE_PAYLOAD, _EXECUTABLE_NODEIP_PAYLOAD])
        message_lengths: list[int] = []

        async def _llm(messages, *, prompt_revision, kbd_id, attempt):
            message_lengths.append(len(messages))
            return next(payloads)

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=corpus)

        assert result["success"] is True
        assert result["attempts"] == 2
        # 重试轮携带额外反馈消息（system+user+feedback = 3 条）
        assert message_lengths == [2, 3]
        signal = json.loads(updates[0]["params"]["signals"])[0]
        assert signal["closure_status"] == "closed"

    async def test_pruned_signals_persisted_into_rejected_audit(self, monkeypatch, prompt_template):
        """L3 剪枝不静默删信号：被冗余剪掉的候选必 落 root_signal_rejected_json（reason_code 区分）。

        与「拒绝」区分：剪枝项本身合法可闭环，只是同篇内被判据更强的信号完全蕴含。
        若只写 accepted、不写剪枝，审核人无法解释「LLM 明明给了 2 条、库里只剩 1 条」。
        """
        updates: list[dict[str, Any]] = []
        db = _db_factory({**_ENTRY_ROW, "signals_json": {"signals": [_producer(["HOST"])], "verification_contract": None}}, updates)
        await _stub_loader(monkeypatch, prompt_template)

        async def _llm(messages, *, prompt_revision, kbd_id, attempt):
            return _MINIMALITY_PAYLOAD

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        result = await route.extract_root_signal_for_kbd(db, 1, corpus=ConflictCorpus([]))

        assert result["success"] is True
        assert result["signals_count"] == 1
        saved = json.loads(updates[0]["params"]["signals"])
        assert len(saved) == 1
        # 保留的是判据更完整的强信号
        assert set(saved[0]["match"]["expected"]) == {"100%", "/run/lock"}
        audit = json.loads(updates[0]["params"]["rejected"])
        assert len(audit) == 1
        assert audit[0]["reason_code"] == "redundant_with_stronger_signal"
        assert audit[0]["redundant_with"] == saved[0]["signal_text"]


class TestCorpusCache:
    async def test_corpus_ttl_avoids_repeat_query(self, monkeypatch):
        queries: list[int] = []
        db = _db_factory(_ENTRY_ROW, [])

        async def _execute(self: Any, stmt: Any, params: Any = None) -> _Result:
            if "FROM kbd_entry" in str(stmt):
                queries.append(1)
                return _Result([("41480", "根因", "虚拟机-009")])
            return _Result([])

        monkeypatch.setattr(_FakeSession, "execute", _execute)
        monkeypatch.setattr(route, "_corpus_cache", None)
        first = await route.get_conflict_corpus(db)
        second = await route.get_conflict_corpus(db)
        assert first is second and len(queries) == 1
        third = await route.get_conflict_corpus(db, force=True)
        assert len(queries) == 2 and third is not first


class TestHttpSurface:
    async def test_single_endpoint_requires_internal_token(self):
        from fastapi import Request

        anonymous = Request({"type": "http", "method": "POST", "path": "/api/admin/kbd/1/extract-root-signal", "headers": []})
        with pytest.raises(HTTPException) as exc:
            await route.extract_root_signal_kbd_api(anonymous, 1)
        assert exc.value.status_code in (401, 403)

    async def test_batch_processor_raises_on_gate_failure(self, monkeypatch):
        """批量任务必须把业务失败如实抛出，才能被 kbd_batch_job_item 记为 failed 并可重试。"""
        from app.routes import admin

        async def _fail(db: Any, kbd_id: int, **kwargs: Any) -> dict[str, Any]:
            return {"success": False, "attempts": 2, "error": "match_type 必须是 ['executable', 'text']", "match_type": None}

        monkeypatch.setattr(route, "extract_root_signal_for_kbd", _fail)
        monkeypatch.setattr(admin, "_db_manager", MagicMock())
        with pytest.raises(HTTPException) as exc:
            await admin._extract_root_signal_one(1)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "ROOT_SIGNAL_GATE_REJECTED"

    async def test_backfill_rejects_non_live_statuses(self):
        from app.routes import admin

        body = admin.RootSignalBackfillRequest(statuses=["archived"])
        with pytest.raises(HTTPException) as exc:
            await admin.backfill_extract_root_signal(
                _make_request(method="POST", path="/api/admin/kbd/batch/extract-root-signal/backfill"),
                body,
                MagicMock(),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "ROOT_SIGNAL_BACKFILL_STATUS_INVALID"


class TestEditRootSignal:
    """人工编辑端点 PATCH /kbd/{id}/root-signal：保存即全量替换，复用抽取同一硬门禁。"""

    @staticmethod
    def _stub_corpus(monkeypatch, corpus):
        async def _corpus(db_manager: Any, *, force: bool = False) -> ConflictCorpus:
            return corpus

        monkeypatch.setattr(route, "get_conflict_corpus", _corpus)

    async def test_edit_persists_valid_executable_signal(self, monkeypatch, corpus):
        updates: list[dict[str, Any]] = []
        monkeypatch.setattr(route, "_db_manager", _db_factory(_ENTRY_ROW, updates))
        self._stub_corpus(monkeypatch, corpus)
        body = route.RootSignalEditRequest(signals=[_EXECUTABLE_PAYLOAD["signal"]])
        result = await route.edit_root_signal(
            _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1, body
        )
        assert result.success is True and result.signals_count == 1
        # 写回只动 root_signal_json 一列（v2 顶层数组），不触碰 content_md
        assert len(updates) == 1
        assert "root_signal_json" in updates[0]["sql"] and "content_md" not in updates[0]["sql"]
        signal = json.loads(updates[0]["params"]["signals"])[0]
        assert signal["schema_version"] == 2 and signal["role"] == "root_cause"
        # 冲突度按现场语料实测（非前端自报）；prompt_revision 标记为人工编辑
        assert signal["distinctiveness"] == {"category_conflicts": 1, "global_conflicts": 1}
        assert signal["prompt_revision"] == "manual-edit"

    async def test_edit_persists_text_signal_without_acquire(self, monkeypatch, corpus):
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "root_cause": "/etc/shadow 中残留旧密码哈希，导致 ssh 密钥认证失败"}
        monkeypatch.setattr(route, "_db_manager", _db_factory(entry, updates))
        self._stub_corpus(monkeypatch, corpus)
        body = route.RootSignalEditRequest(signals=[_TEXT_PAYLOAD["signal"]])
        result = await route.edit_root_signal(
            _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1, body
        )
        assert result.signals_count == 1
        assert json.loads(updates[0]["params"]["signals"])[0]["acquire"] is None

    async def test_edit_all_rejected_keeps_previous_signals(self, monkeypatch, corpus):
        """防绕过回潮 + 防静默清空：有上下文（producer 只产 NODE_IP）却悬空 HOST 的 executable
        全部不可闭环时，只落 rejected 列并 422，**绝不把 root_signal_json 写成空数组**。

        修复前的缺陷：该分支会 UPDATE root_signal_json = []，把专家原有的好信号静默抹掉；
        「清空」这一意图由 body.signals == [] 单独表达（见下方 clear 用例）。
        """
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["NODE_IP"])], "verification_contract": None}}
        monkeypatch.setattr(route, "_db_manager", _db_factory(entry, updates))
        self._stub_corpus(monkeypatch, corpus)
        with pytest.raises(HTTPException) as exc:
            await route.edit_root_signal(
                _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1,
                route.RootSignalEditRequest(signals=[_EXECUTABLE_PAYLOAD["signal"]]),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "ROOT_SIGNAL_ALL_REJECTED"
        assert exc.value.detail["retryable"] is True
        assert len(exc.value.detail["rejected"]) == 1
        assert exc.value.detail["rejected"][0]["reason_code"] == "ROOT_CLOSURE_DANGLING"
        # 只写拒绝审计一列：root_signal_json 一个字都不动，既有信号保持原样
        assert len(updates) == 1
        assert "root_signal_rejected_json = CAST" in updates[0]["sql"]
        assert "root_signal_json" not in updates[0]["sql"]
        # 决策②：拒绝候选不降级 text——candidate 仍是原 executable 形态
        assert json.loads(updates[0]["params"]["rejected"])[0]["candidate"]["match_type"] == "executable"

    async def test_edit_mixed_text_accepted_not_blocked_by_rejected_executable(self, monkeypatch, corpus):
        """边界（防过度收紧）：executable 全拒但 text 被 accepted 时不得触发全拒收口。

        `all_rejected` 仅按 executable 维度判定（executable_total>0 且无一 closed）；
        只要存在 accepted text 就应正常保存，不能把好的 text 根因连同拒绝一起丢弃。
        """
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["NODE_IP"])], "verification_contract": None}}
        monkeypatch.setattr(route, "_db_manager", _db_factory(entry, updates))
        self._stub_corpus(monkeypatch, corpus)
        result = await route.edit_root_signal(
            _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1,
            route.RootSignalEditRequest(signals=_MIXED_TEXT_AND_DANGLING_PAYLOAD["signals"]),
        )
        assert result.signals_count == 1
        assert result.signals[0]["match_type"] == "text"
        persisted = json.loads(updates[0]["params"]["signals"])
        assert len(persisted) == 1 and persisted[0]["match_type"] == "text"
        assert len(json.loads(updates[0]["params"]["rejected"])) == 1

    async def test_edit_closed_with_producer_retained(self, monkeypatch, corpus):
        """producer 产出 HOST → 人工保存的 {{HOST}} 可执行信号 closed，留在 accepted、不进 rejected。"""
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["HOST"])], "verification_contract": None}}
        monkeypatch.setattr(route, "_db_manager", _db_factory(entry, updates))
        self._stub_corpus(monkeypatch, corpus)
        result = await route.edit_root_signal(
            _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1,
            route.RootSignalEditRequest(signals=[_EXECUTABLE_PAYLOAD["signal"]]),
        )
        assert result.signals_count == 1
        assert result.rejected == []
        signal = json.loads(updates[0]["params"]["signals"])[0]
        assert signal["closure_status"] == "closed"
        assert json.loads(updates[0]["params"]["rejected"]) == []

    async def test_edit_rejects_untraceable_signal_and_writes_nothing(self, monkeypatch, corpus):
        """对抗核心：任一信号不过硬门禁即整体 422，绝不部分落库。"""
        updates: list[dict[str, Any]] = []
        monkeypatch.setattr(route, "_db_manager", _db_factory(_ENTRY_ROW, updates))
        self._stub_corpus(monkeypatch, corpus)
        bad = json.loads(json.dumps(_TEXT_PAYLOAD["signal"]))
        bad["provenance"]["evidence"] = "完全不存在于本 KBD 原文的伪造证据 ZZZ"
        bad["match"]["expected"] = ["伪造证据ZZZ"]
        with pytest.raises(HTTPException) as exc:
            await route.edit_root_signal(
                _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1, route.RootSignalEditRequest(signals=[bad])
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "ROOT_SIGNAL_NOT_CONFIRMING"
        assert updates == []

    async def test_edit_applies_minimality_and_audits_pruned(self, monkeypatch, corpus):
        """人工编辑与抽取同标准：一次提交冗余双条时剪弱留强，且剪枝项落拒绝审计（不 422）。"""
        updates: list[dict[str, Any]] = []
        entry = {**_ENTRY_ROW, "signals_json": {"signals": [_producer(["HOST"])], "verification_contract": None}}
        monkeypatch.setattr(route, "_db_manager", _db_factory(entry, updates))
        self._stub_corpus(monkeypatch, corpus)
        body = route.RootSignalEditRequest(signals=_MINIMALITY_PAYLOAD["signals"])
        result = await route.edit_root_signal(
            _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1, body
        )
        assert result.signals_count == 1
        assert set(result.signals[0]["match"]["expected"]) == {"100%", "/run/lock"}
        assert len(result.rejected) == 1
        assert result.rejected[0]["reason_code"] == "redundant_with_stronger_signal"
        audit = json.loads(updates[0]["params"]["rejected"])
        assert len(audit) == 1 and audit[0]["redundant_with"] == result.signals[0]["signal_text"]

    async def test_edit_empty_array_clears_signals(self, monkeypatch):
        updates: list[dict[str, Any]] = []
        monkeypatch.setattr(route, "_db_manager", _db_factory(_ENTRY_ROW, updates))
        result = await route.edit_root_signal(
            _make_request("PATCH", "/api/admin/kbd/1/root-signal"), 1, route.RootSignalEditRequest(signals=[])
        )
        assert result.signals_count == 0
        assert json.loads(updates[0]["params"]["signals"]) == []

    async def test_edit_missing_entry_returns_404(self, monkeypatch):
        updates: list[dict[str, Any]] = []
        monkeypatch.setattr(route, "_db_manager", _db_factory(None, updates))
        with pytest.raises(HTTPException) as exc:
            await route.edit_root_signal(
                _make_request("PATCH", "/api/admin/kbd/1/root-signal"),
                1,
                route.RootSignalEditRequest(signals=[_TEXT_PAYLOAD["signal"]]),
            )
        assert exc.value.status_code == 404

    async def test_edit_requires_internal_token(self):
        anonymous = Request(
            {"type": "http", "method": "PATCH", "path": "/api/admin/kbd/1/root-signal", "headers": []}
        )
        with pytest.raises(HTTPException) as exc:
            await route.edit_root_signal(anonymous, 1, route.RootSignalEditRequest(signals=[]))
        assert exc.value.status_code in (401, 403)


class TestBackfillWhereClause:
    """回刷候选筛选口径：v2 输入面已放开整篇 KBD，不得再用 root_cause 长度设门槛。

    回归背景：`char_length(root_cause) >= 30` 是 v1 遗留，会让根因 <30 字的 KBD
    永久漏出全库回刷范围（且无任何日志提示），与 extract_root_signal 的 v2 口径冲突。
    """

    async def test_backfill_where_has_no_root_cause_length_filter(self, monkeypatch):
        from app.routes import admin

        captured: list[str] = []

        class _Rows:
            def __init__(self, rows: list[Any]) -> None:
                self._rows = rows

            def all(self) -> list[Any]:
                return self._rows

            def scalar(self) -> Any:
                return self._rows[0][0] if self._rows else 0

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

            async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Rows:
                captured.append(str(stmt))
                return _Rows([[1]])

        db = MagicMock()
        db.async_session_factory = lambda: _Session()
        monkeypatch.setattr(admin, "_db_manager", db)

        async def _fake_submit(**kwargs: Any) -> Any:
            return MagicMock(
                batch_id="00000000-0000-0000-0000-000000000001",
                job_type="extract_root_signal",
                status="pending",
                message="ok",
            )

        monkeypatch.setattr(admin, "_submit_batch_job", _fake_submit)
        body = admin.RootSignalBackfillRequest(statuses=["published"], only_missing=True)
        await admin.backfill_extract_root_signal(
            _make_request(method="POST", path="/api/admin/kbd/batch/extract-root-signal/backfill"),
            body,
            MagicMock(),
        )
        selects = [sql for sql in captured if "SELECT" in sql]
        assert selects, "回刷必须实际查询候选集"
        assert all("char_length(root_cause)" not in sql for sql in selects)


class TestConflictCorpus:
    def test_tokens_from_acquire_resources_count_as_conflict(self):
        corpus = ConflictCorpus(_CORPUS_ROWS)
        signal = {
            "match_type": "executable",
            "match": {"expected": ["100%"]},
            "acquire": {"tool": "qfk_system", "args": {"command": "df", "command_args": ["-h", "/run/lock"]}},
            "provenance": {"source_category_id": "虚拟机-009"},
        }
        assert corpus.conflicts(signal, self_support_id="41480") == {"category_conflicts": 1, "global_conflicts": 1}

    def test_generic_expected_alone_does_not_conflict(self):
        """仅判定串（如 100%）不构成冲突：必须与采集资源同命中，否则不同目录案例互相误判。"""
        corpus = ConflictCorpus(_CORPUS_ROWS)
        signal = {
            "match_type": "executable",
            "match": {"expected": ["100%"]},
            "acquire": {"tool": "qfk_system", "args": {"command": "df", "command_args": ["-h", "/var/log"]}},
            "provenance": {"source_category_id": "虚拟机-009"},
        }
        assert corpus.conflicts(signal, self_support_id="41480") == {"category_conflicts": 0, "global_conflicts": 0}

    def test_empty_tokens_never_conflict(self):
        corpus = ConflictCorpus(_CORPUS_ROWS)
        signal = {"match_type": "text", "match": {"expected": []}, "acquire": None, "provenance": {}}
        assert corpus.conflicts(signal) == {"category_conflicts": 0, "global_conflicts": 0}


# v2 说明：原 TestCrossLanguageContract 锁定的 MIN_ROOT_CAUSE_LEN 跨语言契约已随服务端
# 422 硬门一同废除（抽取输入面放开为整篇 KBD，根因短不再阻塞），无需再钉值。

