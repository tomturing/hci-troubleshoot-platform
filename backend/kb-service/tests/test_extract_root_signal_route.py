"""根因信号生产链路单测：热加载契约、门禁失败不写库、冲突度实测、批量处理器与鉴权。

与抽取内核单测（test_root_signal_extractor.py）的分工：
- 内核单测管「契约与门禁语义」；
- 本文件管「路由装配」：Prompt 热加载是否真的每次调用、失败是否污染落库、
  冲突度是否按现场语料实测、批量处理器是否把业务失败如实抛给 kbd_batch_job_item。
"""

from __future__ import annotations

import json
import re
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
            "evidence": "df -h /run/lock 显示使用率 100%",
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

_ENTRY_ROW = {
    "id": 1,
    "support_id": "41480",
    "title": "【HCI】修改admin密码后root密码未同步",
    "root_cause": "df 显示 /run/lock 分区使用率 100%，占满导致虚拟机任务失败",
    "category_id": "虚拟机-009",
    "ai_category_id": None,
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
        # 落库只动 root_signal_json 一列
        assert len(updates) == 1
        assert "root_signal_json" in updates[0]["sql"] and "content_md" not in updates[0]["sql"]
        signal = json.loads(updates[0]["params"]["signal"])
        assert signal["schema_version"] == 1 and signal["role"] == "root_cause"
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
        assert json.loads(updates[0]["params"]["signal"])["acquire"] is None

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
        assert result["signal"] is None
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

    async def test_short_root_cause_rejected_before_llm(self, monkeypatch, prompt_template):
        """语料不达标的正路是治理根因，不是放宽门禁（对齐 200 篇实测结论）。"""
        updates: list[dict[str, Any]] = []
        db = _db_factory({**_ENTRY_ROW, "root_cause": "配置不正确"}, updates)
        await _stub_loader(monkeypatch, prompt_template)

        async def _llm(
            messages: list[dict[str, str]], *, prompt_revision: str, kbd_id: int, attempt: int
        ) -> dict[str, Any]:
            raise AssertionError("根因过短不应调用 LLM")

        monkeypatch.setattr(route, "_call_llm_messages", _llm)
        with pytest.raises(HTTPException) as exc:
            await route.extract_root_signal_for_kbd(db, 1, corpus=ConflictCorpus([]))
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "ROOT_SIGNAL_ROOT_CAUSE_TOO_SHORT"


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


class TestCrossLanguageContract:
    """跨语言常量不漂移：Admin UI 的前置体检阈值必须等于后端硬门禁阈值。

    放在后端侧是因为前端 CI 门禁用 `sparse-checkout: frontend` 的稀疏检出（见
    .github/workflows/ci.yml 「前端门禁不消费后端源码」），前端测试拿不到本仓库源码；
    而 unit-tests 作业为全量检出，两边文件都在。方向也符合依赖常理：后端合约作为
    事实源去审计前端镜像值，而不是反过来。
    """

    _UI_SOURCE = "frontend/admin/src/utils/rootSignal.ts"

    def test_min_root_cause_len_matches_admin_ui(self):
        ui_file = REPO_ROOT / self._UI_SOURCE
        assert ui_file.exists(), f"前端阈值文件缺失，无法校验跨语言契约: {ui_file}"
        matched = re.search(
            r"export const MIN_ROOT_CAUSE_LEN = (\d+)", ui_file.read_text(encoding="utf-8")
        )
        assert matched is not None, f"前端未再声明 MIN_ROOT_CAUSE_LEN，契约锚点丢失：{self._UI_SOURCE}"
        assert int(matched.group(1)) == route.MIN_ROOT_CAUSE_LEN, (
            "Admin UI 的「根因过短不抽取」前置体检与后端 422 门禁阈值不一致，"
            "会出现按钮放行但接口报错的脱同步体验"
        )
