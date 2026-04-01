"""
tests/unit/kbd/test_importer.py — kbd/importer.py 单元测试

覆盖：
  - import_entry：API 调用成功（created / idempotent）
  - import_entry：converter 返回 None 时输出 "error"
  - import_entry：API 返回错误时输出 "error"
  - import_batch：批量统计计数
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_scripts_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
)
if _scripts_root not in sys.path:
    sys.path.insert(0, _scripts_root)


def _make_client(response_json: dict, status_code: int = 200):
    """构造一个 httpx AsyncClient mock"""
    client = MagicMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=response_json)
    response.raise_for_status = MagicMock()

    if status_code >= 400:
        from httpx import HTTPStatusError, Request, Response
        request = Request("POST", "http://test/api")
        response.raise_for_status = MagicMock(
            side_effect=HTTPStatusError(
                message=f"HTTP {status_code}",
                request=request,
                response=Response(status_code=status_code, request=request),
            )
        )

    client.post = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


# ─── import_entry ────────────────────────────────────────────────────────────

class TestImportEntry:
    """测试 import_entry API 调用逻辑"""

    @pytest.fixture
    def minimal_rows(self):
        """最小案例数据 - 使用正确的 DOM 结构"""
        return {
            "id": "36156",
            "name": "测试案例",
            "content": """
                <div class="mceNonEditable">
                    <input value="*问题描述">
                    <div><div>问题描述内容</div></div>
                </div>
                <div class="mceNonEditable">
                    <input value="有效排查步骤">
                    <div><div>有效排查步骤内容</div></div>
                </div>
                <div class="mceNonEditable">
                    <input value="*解决方案">
                    <div><div>解决方案内容</div></div>
                </div>
            """,
        }

    @pytest.mark.asyncio
    async def test_creates_new_entry(self, tmp_path, minimal_rows):
        """新案例调用 API 成功，返回 'created'"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )

        # API 返回成功创建
        client = _make_client({
            "success": True,
            "kbd_id": 123,
            "status": "draft",
            "message": "创建成功",
        })

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.KB_SERVICE_URL", "http://kb-service"),
            patch("kbd.importer.settings.INTERNAL_API_TOKEN", "test-token"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("36156", client)

        assert result == "created"
        client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotent_when_already_exists(self, tmp_path, minimal_rows):
        """API 返回已存在时，返回 'idempotent'"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )

        # API 返回已存在
        client = _make_client({
            "success": True,
            "kbd_id": 123,
            "status": "draft",
            "message": "KBD 条目已存在",
        })

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.KB_SERVICE_URL", "http://kb-service"),
            patch("kbd.importer.settings.INTERNAL_API_TOKEN", "test-token"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("36156", client)

        assert result == "idempotent"

    @pytest.mark.asyncio
    async def test_returns_error_when_converter_fails(self, tmp_path):
        """converter 返回 None 时，返回 'error'"""
        # 不创建 raw.json，converter 会返回 None

        client = _make_client({"success": True})

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.KB_SERVICE_URL", "http://kb-service"),
            patch("kbd.importer.settings.INTERNAL_API_TOKEN", "test-token"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("36156", client)

        assert result == "error"

    @pytest.mark.asyncio
    async def test_returns_error_when_api_fails(self, tmp_path, minimal_rows):
        """API 返回失败时，返回 'error'"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )

        # API 返回失败
        client = _make_client({
            "success": False,
            "message": "内部错误",
        })

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.KB_SERVICE_URL", "http://kb-service"),
            patch("kbd.importer.settings.INTERNAL_API_TOKEN", "test-token"),
        ):
            from kbd.importer import import_entry
            result = await import_entry("36156", client)

        assert result == "error"


# ─── import_batch ─────────────────────────────────────────────────────────────

class TestImportBatch:
    """测试 import_batch 批量统计"""

    @pytest.fixture
    def minimal_rows(self):
        """最小案例数据 - 使用正确的 DOM 结构"""
        return {
            "id": "36156",
            "name": "测试案例",
            "content": """
                <div class="mceNonEditable">
                    <input value="*问题描述">
                    <div><div>问题描述</div></div>
                </div>
                <div class="mceNonEditable">
                    <input value="有效排查步骤">
                    <div><div>排查步骤</div></div>
                </div>
                <div class="mceNonEditable">
                    <input value="*解决方案">
                    <div><div>解决方案</div></div>
                </div>
            """,
        }

    @pytest.mark.asyncio
    async def test_counts_correctly(self, tmp_path, minimal_rows):
        """批量导入正确统计"""
        # 创建两个案例
        for sid in ["36156", "36157"]:
            case_dir = tmp_path / sid
            case_dir.mkdir(parents=True)
            data = {**minimal_rows, "id": sid}
            (case_dir / "raw.json").write_text(json.dumps(data), encoding="utf-8")

        # 创建 mock client
        client = MagicMock(spec=httpx.AsyncClient)
        # 第一个返回 created，第二个返回 idempotent
        responses = [
            MagicMock(
                status_code=200,
                json=MagicMock(return_value={"success": True, "kbd_id": 1, "status": "draft", "message": "创建成功"})
            ),
            MagicMock(
                status_code=200,
                json=MagicMock(return_value={"success": True, "kbd_id": 2, "status": "draft", "message": "KBD 条目已存在"})
            ),
        ]
        client.post = AsyncMock(side_effect=responses)
        client.aclose = AsyncMock()

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.importer.settings.KB_SERVICE_URL", "http://kb-service"),
            patch("kbd.importer.settings.INTERNAL_API_TOKEN", "test-token"),
        ):
            from kbd.importer import import_batch
            result = await import_batch(["36156", "36157"], client=client)

        assert result["created"] == 1
        assert result["idempotent"] == 1
        assert result["error"] == 0

    @pytest.mark.asyncio
    async def test_empty_list_returns_zero_stats(self):
        """空列表返回零统计"""
        with (
            patch("kbd.importer.settings.KB_SERVICE_URL", "http://kb-service"),
            patch("kbd.importer.settings.INTERNAL_API_TOKEN", "test-token"),
        ):
            from kbd.importer import import_batch
            result = await import_batch([], client=None)

        assert result == {"created": 0, "idempotent": 0, "skipped": 0, "error": 0}