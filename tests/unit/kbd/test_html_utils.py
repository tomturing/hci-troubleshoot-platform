"""
tests/unit/kbd/test_html_utils.py — 测试 scripts/kbd/html_utils.py

测试公共图片 URL 提取函数，确保：
  - 基本提取功能正常
  - data-src 懒加载属性正确处理
  - 去重保序
  - 相对路径正确解析
  - 位置计算准确
"""
import sys
from pathlib import Path

# 将 scripts 目录加入 sys.path
_scripts_root = Path(__file__).parent.parent.parent.parent / "scripts"
if _scripts_root not in sys.path:
    sys.path.insert(0, str(_scripts_root))


# ─── extract_image_urls_with_positions ────────────────────────────────────────

class TestExtractImageUrlsWithPositions:
    """测试图片 URL + 位置提取"""

    def setup_method(self):
        from kbd.html_utils import extract_image_urls_with_positions
        self.fn = extract_image_urls_with_positions
        self.base_url = "https://support.sangfor.com.cn"

    def test_single_image_position(self):
        """单张图片位置计算"""
        html = '<p>前置文字</p><img src="/img1.png" />'
        result = self.fn(html, self.base_url)
        assert len(result) == 1
        url, pos = result[0]
        assert "/img1.png" in url
        # src="/img1.png" 在 HTML 中的位置
        assert pos > 0
        assert html[pos:].startswith("/img1.png")

    def test_relative_path_resolution(self):
        """相对路径正确解析为绝对 URL"""
        html = '<img src="/_static/img/test.png" />'
        result = self.fn(html, self.base_url)
        assert len(result) == 1
        url, _pos = result[0]
        assert url == "https://support.sangfor.com.cn/_static/img/test.png"

    def test_query_string_preserved(self):
        """带 query 的 src 正确处理"""
        html = '<img src="/img.png?v=123&w=456" />'
        result = self.fn(html, self.base_url)
        assert len(result) == 1
        url, pos = result[0]
        assert "?v=123" in url
        # 使用原始 src（带 query）定位
        assert pos > 0

    def test_data_src_attribute(self):
        """data-src 懒加载属性提取"""
        html = '<img data-src="/lazy.jpg" />'
        result = self.fn(html, self.base_url)
        assert len(result) == 1
        url, pos = result[0]
        assert "/lazy.jpg" in url
        assert pos > 0

    def test_deduplication_with_positions(self):
        """去重保序，位置为首次出现"""
        html = '<img src="/a.png" /><img src="/b.png" /><img src="/a.png" />'
        result = self.fn(html, self.base_url)
        assert len(result) == 2  # a, b (去重)
        url_a, pos_a = result[0]
        url_b, pos_b = result[1]
        assert "/a.png" in url_a
        assert "/b.png" in url_b
        # a 的位置应为首次出现位置（小于 b）
        assert pos_a < pos_b

    def test_preserves_order(self):
        """URL 按出现顺序排列"""
        html = '<img src="/1.png" /><img src="/2.png" /><img src="/3.png" />'
        result = self.fn(html, self.base_url)
        urls = [url for url, _ in result]
        assert urls[0].endswith("/1.png")
        assert urls[1].endswith("/2.png")
        assert urls[2].endswith("/3.png")

    def test_data_uri_ignored(self):
        """data: 协议图片跳过"""
        html = '<img src="data:image/png;base64,abc" />'
        result = self.fn(html, self.base_url)
        assert result == []

    def test_empty_html(self):
        """空 HTML 返回空列表"""
        assert self.fn("", self.base_url) == []
        assert self.fn("<p>无图片</p>", self.base_url) == []

    def test_filename_collision_position(self):
        """文件名重复时位置仍为首次出现（基于原始 src 定位）"""
        # 两个不同路径但文件名相同的图片
        html = '<img src="/dir1/img.png" /><img src="/dir2/img.png" />'
        result = self.fn(html, self.base_url)
        assert len(result) == 2
        url1, pos1 = result[0]
        url2, pos2 = result[1]
        assert "/dir1/img.png" in url1
        assert "/dir2/img.png" in url2
        # 位置分别是各自 src 的位置
        assert pos1 < pos2


# ─── extract_image_urls ────────────────────────────────────────────────────────

class TestExtractImageUrls:
    """测试纯 URL 提取（薄封装）"""

    def setup_method(self):
        from kbd.html_utils import extract_image_urls
        self.fn = extract_image_urls
        self.base_url = "https://support.sangfor.com.cn"

    def test_returns_urls_only(self):
        """仅返回 URL 列表"""
        html = '<img src="/a.png" /><img src="/b.png" />'
        urls = self.fn(html, self.base_url)
        assert isinstance(urls, list)
        assert all(isinstance(u, str) for u in urls)
        assert len(urls) == 2

    def test_consistent_with_positions_version(self):
        """与 with_positions 版本序号一致"""
        from kbd.html_utils import extract_image_urls_with_positions
        html = '<img src="/x.png" /><img src="/y.png" /><img src="/x.png" />'
        urls = self.fn(html, self.base_url)
        with_pos = extract_image_urls_with_positions(html, self.base_url)
        urls_from_pos = [url for url, _ in with_pos]
        assert urls == urls_from_pos
