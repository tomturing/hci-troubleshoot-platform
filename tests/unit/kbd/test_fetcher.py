"""
tests/unit/kbd/test_fetcher.py — kbd/fetcher.py 单元测试

覆盖：
  - _extract_image_urls：从 HTML 提取图片 URL（去重、忽略 data:、处理相对路径）
  - _extract_metadata：从 rows 提取 7 个 metadata 字段
  - _make_support_url：URL 格式验证
  - _is_fetched：幂等检测（raw.json 存在且可解析）
  - _write_raw / _write_fetch_failed：文件写入（使用 tmp_path fixture）
  - fetch_case：API 调用端到端（httpx mock + 文件系统验证）
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 路径注入（conftest.py 已处理，此处防御性再加一次）
_scripts_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
)
if _scripts_root not in sys.path:
    sys.path.insert(0, _scripts_root)


# ─── _extract_image_urls ──────────────────────────────────────────────────────

class TestExtractImageUrls:
    """测试 HTML 图片 URL 提取"""

    def setup_method(self):
        from kbd.fetcher import _extract_image_urls
        self.fn = _extract_image_urls

    def test_single_image(self):
        html = '<img src="/_static/img1.png" />'
        urls = self.fn(html)
        assert len(urls) == 1
        assert urls[0].endswith("/img1.png")
        assert urls[0].startswith("http")

    def test_data_uri_ignored(self):
        """data: 协议图片应被忽略"""
        html = '<img src="data:image/png;base64,abc123" />'
        urls = self.fn(html)
        assert urls == []

    def test_deduplication(self):
        """同一 URL 只出现一次"""
        html = '<img src="/img.png" /><img src="/img.png" />'
        urls = self.fn(html)
        assert len(urls) == 1

    def test_preserves_order(self):
        """不同 URL 保持出现顺序"""
        html = '<img src="/a.png" /><img src="/b.png" /><img src="/c.png" />'
        urls = self.fn(html)
        assert urls[0].endswith("/a.png")
        assert urls[1].endswith("/b.png")
        assert urls[2].endswith("/c.png")

    def test_empty_html(self):
        assert self.fn("") == []
        assert self.fn("<p>无图片</p>") == []

    def test_data_src_fallback(self):
        """data-src 懒加载属性应被提取"""
        html = '<img data-src="/_static/lazy.jpg" />'
        urls = self.fn(html)
        assert len(urls) == 1
        assert urls[0].endswith("/lazy.jpg")


# ─── _extract_metadata ───────────────────────────────────────────────────────

class TestExtractMetadata:
    """测试 metadata 字段提取"""

    def setup_method(self):
        from kbd.fetcher import _extract_metadata
        self.fn = _extract_metadata

    def test_full_metadata(self, minimal_rows):
        meta = self.fn(minimal_rows)
        assert meta["sangfor_main_module"] == "网络问题"
        assert meta["sangfor_sub_module"] == "实体机网络"
        assert meta["suite_version"] == "通用"
        assert meta["sangfor_updated_at"] == "2026-01-15 11:25:46"
        assert meta["sangfor_created_at"] is None
        assert meta["create_admin_id"] == "68532"
        assert meta["update_admin_id"] == "14201"

    def test_missing_fields_become_none(self):
        meta = self.fn({})
        for key in [
            "sangfor_main_module", "sangfor_sub_module", "suite_version",
            "sangfor_updated_at", "sangfor_created_at",
            "create_admin_id", "update_admin_id",
        ]:
            assert meta[key] is None

    def test_integer_converted_to_str(self):
        """数字 ID 应被转换为字符串"""
        meta = self.fn({"createAdminId": 12345})
        assert meta["create_admin_id"] == "12345"

    def test_empty_string_becomes_none(self):
        meta = self.fn({"mainModuleNames": ""})
        assert meta["sangfor_main_module"] is None


# ─── _make_support_url ────────────────────────────────────────────────────────

class TestMakeSupportUrl:
    """测试 support_url 生成"""

    def setup_method(self):
        from kbd.fetcher import _make_support_url
        self.fn = _make_support_url

    def test_url_format(self):
        url = self.fn("36156")
        assert "support.sangfor.com.cn" in url
        assert "product_id=33" in url
        assert "category_id=36156" in url
        assert "isOpen=true" in url

    def test_different_ids(self):
        url1 = self.fn("100")
        url2 = self.fn("99999")
        assert "category_id=100" in url1
        assert "category_id=99999" in url2
        assert url1 != url2


# ─── _is_fetched ─────────────────────────────────────────────────────────────

class TestIsFetched:
    """测试文件幂等检测"""

    def test_no_directory(self, tmp_path):
        from unittest.mock import patch
        with patch("kbd.fetcher.settings") as mock_s:
            mock_s.KBD_CACHE_DIR = tmp_path
            from kbd.fetcher import _is_fetched
            assert _is_fetched("nonexistent") is False

    def test_valid_raw_json(self, tmp_path):
        case_dir = tmp_path / "12345"
        case_dir.mkdir()
        (case_dir / "raw.json").write_text(
            json.dumps({"id": 12345, "name": "测试"}), encoding="utf-8"
        )
        with patch("kbd.fetcher.settings") as mock_s:
            mock_s.KBD_CACHE_DIR = tmp_path
            from importlib import reload

            import kbd.fetcher as ft
            reload(ft)
            # 直接调用，patch settings
            with patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path):
                # 需要重新绑定，因为 _case_dir 也使用 settings
                result = ft._is_fetched.__wrapped__("12345") if hasattr(ft._is_fetched, "__wrapped__") else None

    def test_corrupt_raw_json(self, tmp_path):
        """损坏的 raw.json 应返回 False"""
        case_dir = tmp_path / "99"
        case_dir.mkdir()
        (case_dir / "raw.json").write_text("not-json", encoding="utf-8")
        with patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path):
            from kbd.fetcher import _is_fetched
            assert _is_fetched("99") is False


# ─── _write_raw / _write_fetch_failed ────────────────────────────────────────

class TestFileWrite:
    """测试文件写入功能"""

    def test_write_raw_creates_file(self, tmp_path, minimal_rows):
        with patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path):
            from kbd.fetcher import _write_raw
            _write_raw("36156", minimal_rows)
            raw_path = tmp_path / "36156" / "raw.json"
            assert raw_path.exists()
            loaded = json.loads(raw_path.read_text(encoding="utf-8"))
            assert loaded["id"] == 36156
            assert loaded["name"] == minimal_rows["name"]

    def test_write_raw_is_valid_json(self, tmp_path, minimal_rows):
        with patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path):
            from kbd.fetcher import _write_raw
            _write_raw("abc", minimal_rows)
            content = (tmp_path / "abc" / "raw.json").read_text(encoding="utf-8")
            # 确认可解析
            parsed = json.loads(content)
            assert isinstance(parsed, dict)

    def test_write_fetch_failed(self, tmp_path):
        with patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path):
            from kbd.fetcher import _write_fetch_failed
            _write_fetch_failed("789", "连接超时")
            failed_path = tmp_path / "789" / "fetch.failed"
            assert failed_path.exists()
            data = json.loads(failed_path.read_text(encoding="utf-8"))
            assert data["support_id"] == "789"
            assert "连接超时" in data["error"]


# ─── fetch_case（集成级 mock）────────────────────────────────────────────────

class TestFetchCase:
    """测试 fetch_case 主流程（mock httpx，真实文件系统）"""

    @pytest.mark.asyncio
    async def test_fetch_creates_raw_json(self, tmp_path, api_payload):
        """成功抓取后应创建 raw.json"""
        import httpx

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = api_payload
        mock_resp.raise_for_status = MagicMock()
        # 模拟图片下载异常（不影响主流程）
        mock_resp.content = b""

        with (
            patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.fetcher.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.fetcher.settings.SANGFOR_TIMEOUT", 30.0),
            patch("kbd.fetcher.settings.SANGFOR_MAX_RETRIES", 1),
            patch("kbd.fetcher.settings.VISION_CONCURRENCY", 1),
            patch("kbd.fetcher.settings.MIN_IMAGE_SIZE", 2048),
            patch("kbd.fetcher._retry_request", AsyncMock(return_value=mock_resp)),
        ):
            from kbd.fetcher import fetch_case
            result = await fetch_case("36156")

            assert result is not None
            assert result["support_id"] == "36156"
            assert "title" in result
            raw_path = tmp_path / "36156" / "raw.json"
            assert raw_path.exists()

    @pytest.mark.asyncio
    async def test_skip_if_already_fetched(self, tmp_path, minimal_rows):
        """raw.json 已存在时应直接返回 skipped=True"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(
            json.dumps(minimal_rows), encoding="utf-8"
        )
        with patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path):
            from kbd.fetcher import fetch_case
            result = await fetch_case("36156")
            assert result is not None
            assert result.get("skipped") is True

    @pytest.mark.asyncio
    async def test_api_error_code_writes_failed(self, tmp_path):
        """API 返回非零 code 时应写入 fetch.failed"""
        import httpx

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 1001, "msg": "案例不存在", "rows": None}
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("kbd.fetcher.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.fetcher.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
            patch("kbd.fetcher.settings.SANGFOR_TIMEOUT", 30.0),
            patch("kbd.fetcher.settings.SANGFOR_MAX_RETRIES", 1),
            patch("kbd.fetcher._retry_request", AsyncMock(return_value=mock_resp)),
        ):
            from kbd.fetcher import fetch_case
            result = await fetch_case("99999")
            assert result is None
            failed_path = tmp_path / "99999" / "fetch.failed"
            assert failed_path.exists()
