#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量抓取深信服支持网站 HCI 案例库数据。

用法:
    python scripts/fetch_cases.py [--start 0] [--count 1898] [--delay 0.8]

说明:
    - 从 Excel 文件读取案例 ID 列表（以 "3" 开头的 ID）
    - 逐条调用 getDetailById API 获取案例详情
    - 解析 HTML content 提取结构化字段
    - 输出为 JSONL 格式到 data-pipeline/cases_raw/cases.jsonl
    - 支持断点续传（已抓取的 ID 自动跳过）
"""

import argparse
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib import request, error
from urllib.request import Request

# ─── 配置 ────────────────────────────────────────────────────────────────────

BASE_URL = "https://support.sangfor.com.cn/spt/openapi/case/es/getDetailById/{id}"
EXCEL_PATH = Path(__file__).parent.parent / "案例生产详细数据24-26.xlsx"
OUTPUT_DIR = Path(__file__).parent.parent / "data-pipeline" / "cases_raw"
OUTPUT_FILE = OUTPUT_DIR / "cases.jsonl"
PROGRESS_FILE = OUTPUT_DIR / ".fetch_progress.json"

# 请求头（来自浏览器抓包）
HEADERS = {
    "Accept": "application/vnd.edusoho.v2+json",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
    ),
    "X-Requested-With": "xmlhttprequest",
    "Http_x_requested_with": "xmlhttprequest",
    "Referer": "https://support.sangfor.com.cn/cases/list?product_id=33&type=1",
    # Cookie 在运行时通过参数传入，不硬编码
}

# ─── HTML 解析 ─────────────────────────────────────────────────────────────────

class SectionExtractor(HTMLParser):
    """
    解析案例 HTML content，提取各章节文本。
    
    章节标识：<input ... value="章节名" />
    章节内容：紧随其后的 <div class="..."> 标签内容
    """

    def __init__(self):
        super().__init__()
        self.sections: dict[str, str] = {}
        self._current_section: str | None = None
        self._capture_next_div = False
        self._div_depth = 0
        self._capturing = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list):
        attr_dict = dict(attrs)
        # 检测章节标题 input
        if tag == "input" and attr_dict.get("value"):
            val = attr_dict["value"].strip().lstrip("*")
            if val:
                self._current_section = val
                self._capture_next_div = True
                return
        # 开始捕获章节内容 div
        if tag == "div" and self._capture_next_div and self._current_section:
            self._capture_next_div = False
            self._capturing = True
            self._div_depth = 1
            self._buffer = []
            return
        # 追踪嵌套 div 深度
        if self._capturing and tag == "div":
            self._div_depth += 1

    def handle_endtag(self, tag: str):
        if self._capturing and tag == "div":
            self._div_depth -= 1
            if self._div_depth == 0:
                # 章节结束，保存内容
                text = " ".join(self._buffer).strip()
                # 清理多余空白
                text = re.sub(r"\s+", " ", text).strip()
                self.sections[self._current_section] = text
                self._capturing = False
                self._current_section = None
                self._buffer = []

    def handle_data(self, data: str):
        if self._capturing:
            cleaned = data.strip()
            if cleaned:
                self._buffer.append(cleaned)


def parse_html_content(html: str) -> dict[str, str]:
    """从 HTML content 中提取结构化字段。"""
    parser = SectionExtractor()
    parser.feed(html)

    # 字段映射（HTML 中的章节名 → 结构化字段名）
    field_map = {
        "问题描述": "problem_desc",
        "告警信息": "alarm_info",
        "有效排查步骤": "troubleshoot_steps",
        "根因": "root_cause",
        "解决方案": "solution",
        "操作影响范围": "impact_scope",
        "是否是临时解决方案": "is_temporary",
        "建议与总结": "suggestions",
        "排查内容": "troubleshoot_content",
        # 兼容旧格式
        "排查步骤": "troubleshoot_steps",
        "处理步骤": "troubleshoot_steps",
    }

    result = {}
    for html_key, field_name in field_map.items():
        val = parser.sections.get(html_key, "").strip()
        if val and val not in ("无", "&nbsp;", ""):
            result[field_name] = val

    # 保留未映射的章节（用于发现新字段）
    unknown = {k: v for k, v in parser.sections.items()
               if k not in field_map and v.strip() and v.strip() not in ("无", "&nbsp;")}
    if unknown:
        result["_extra_sections"] = unknown

    return result


# ─── Excel 解析 ────────────────────────────────────────────────────────────────

def load_ids_from_excel(excel_path: Path, prefix: str = "3") -> list[int]:
    """从 Excel 文件解析案例 ID（不依赖 openpyxl，直接解析 ZIP 格式）。"""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

    # 读取共享字符串
    with zipfile.ZipFile(excel_path) as z:
        # 共享字符串表
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            with z.open("xl/sharedStrings.xml") as f:
                tree = ET.parse(f)
                for si in tree.findall(f"{ns}si"):
                    t = si.find(f"{ns}t")
                    if t is not None and t.text:
                        shared_strings.append(t.text)
                    else:
                        # 富文本格式
                        text_parts = [r.text for r in si.findall(f".//{ns}t") if r.text]
                        shared_strings.append("".join(text_parts))

        # 解析 sheet1
        with z.open("xl/worksheets/sheet1.xml") as f:
            tree = ET.parse(f)

    ids = []
    rows = tree.findall(f".//{ns}row")
    for row in rows[1:]:  # 跳过表头
        cells = row.findall(f"{ns}c")
        if not cells:
            continue
        first_cell = cells[0]
        cell_type = first_cell.get("t", "")
        v = first_cell.find(f"{ns}v")
        if v is None or not v.text:
            continue

        if cell_type == "s":
            # 共享字符串
            idx = int(v.text)
            val = shared_strings[idx] if idx < len(shared_strings) else ""
        else:
            val = v.text

        val = str(val).strip()
        if val.startswith(prefix) and val.isdigit():
            ids.append(int(val))

    return ids


# ─── 进度管理 ──────────────────────────────────────────────────────────────────

def load_progress(progress_file: Path) -> set[int]:
    """加载已抓取的 ID 集合（断点续传）。"""
    if not progress_file.exists():
        return set()
    try:
        data = json.loads(progress_file.read_text())
        return set(data.get("done_ids", []))
    except Exception:
        return set()


def save_progress(progress_file: Path, done_ids: set[int]):
    """保存进度。"""
    progress_file.write_text(json.dumps({"done_ids": sorted(done_ids), "updated": datetime.now().isoformat()}))


# ─── API 调用 ──────────────────────────────────────────────────────────────────

def fetch_case(case_id: int, cookie: str, retry: int = 3) -> dict | None:
    """调用 getDetailById API 获取单条案例详情。"""
    url = BASE_URL.format(id=case_id)
    headers = {
        **HEADERS,
        "Cookie": cookie,
        "Referer": f"https://support.sangfor.com.cn/cases/detail?id={case_id}",
    }

    for attempt in range(retry):
        try:
            req = Request(url, headers=headers)
            with request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)

                if data.get("code") != 0:
                    print(f"  [WARN] ID={case_id} 返回 code={data.get('code')}: {data.get('msg', '')}")
                    return None

                return data.get("rows")

        except error.HTTPError as e:
            print(f"  [ERROR] ID={case_id} HTTP {e.code}，尝试 {attempt + 1}/{retry}")
            if e.code in (401, 403):
                print("  [FATAL] 认证失败，Cookie 可能已过期，终止抓取")
                sys.exit(1)
            time.sleep(2 ** attempt)

        except error.URLError as e:
            print(f"  [ERROR] ID={case_id} 网络错误：{e.reason}，尝试 {attempt + 1}/{retry}")
            time.sleep(2 ** attempt)

        except json.JSONDecodeError as e:
            print(f"  [ERROR] ID={case_id} JSON 解析失败：{e}")
            return None

    print(f"  [SKIP] ID={case_id} 重试次数耗尽，跳过")
    return None


# ─── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="批量抓取深信服 HCI 案例库")
    parser.add_argument("--cookie", required=True, help="完整 Cookie 字符串")
    parser.add_argument("--start", type=int, default=0, help="从第 N 个 ID 开始（用于分段）")
    parser.add_argument("--count", type=int, default=0, help="抓取条数，0 表示全部")
    parser.add_argument("--delay", type=float, default=0.8, help="请求间隔秒数（默认 0.8s）")
    parser.add_argument("--prefix", default="3", help="只抓取指定前缀的 ID（默认 '3'）")
    parser.add_argument("--no-resume", action="store_true", help="不使用断点续传，重新抓取")
    args = parser.parse_args()

    # 初始化输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 加载 ID 列表
    print(f"📂 加载 Excel: {EXCEL_PATH}")
    all_ids = load_ids_from_excel(EXCEL_PATH, prefix=args.prefix)
    print(f"✅ 共找到 {len(all_ids)} 个以 '{args.prefix}' 开头的案例 ID")

    # 分段处理
    target_ids = all_ids[args.start:]
    if args.count > 0:
        target_ids = target_ids[:args.count]
    print(f"📋 本次目标: {len(target_ids)} 个 ID（从索引 {args.start} 开始）")

    # 断点续传
    done_ids: set[int] = set()
    if not args.no_resume:
        done_ids = load_progress(PROGRESS_FILE)
        skip_count = sum(1 for i in target_ids if i in done_ids)
        if skip_count:
            print(f"⏩ 断点续传：跳过已完成的 {skip_count} 个 ID")

    remaining = [i for i in target_ids if i not in done_ids]
    print(f"🚀 实际需抓取: {len(remaining)} 条\n")

    if not remaining:
        print("✅ 全部已完成，无需抓取")
        return

    # 打开输出文件（追加模式）
    success_count = 0
    fail_count = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as out_f:
        for i, case_id in enumerate(remaining):
            print(f"[{i + 1}/{len(remaining)}] 抓取 ID={case_id} ...", end=" ", flush=True)

            rows = fetch_case(case_id, args.cookie)

            if rows is None:
                fail_count += 1
                print("FAIL")
                continue

            # 解析 HTML content
            html_content = rows.get("content", "")
            structured = parse_html_content(html_content) if html_content else {}

            # 构建最终记录
            record = {
                "id": case_id,
                "title": rows.get("name", ""),
                "product": rows.get("productName", ""),
                "main_module": rows.get("mainModuleNames", ""),
                "sub_module": rows.get("childModuleNames", ""),
                "suite_version": rows.get("suiteVersion", ""),
                "read_access": rows.get("readaccess", 0),
                "update_time": rows.get("updateTime", ""),
                "fetched_at": datetime.now().isoformat(),
                **structured,
            }

            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            out_f.flush()  # 实时写入，防止中断丢失

            done_ids.add(case_id)
            success_count += 1
            print(f"OK  [{rows.get('mainModuleNames', '')} / {rows.get('childModuleNames', '')}]")

            # 每 50 条保存一次进度
            if success_count % 50 == 0:
                save_progress(PROGRESS_FILE, done_ids)
                print(f"\n  💾 进度已保存 ({success_count} 成功，{fail_count} 失败)\n")

            # 限速
            if i < len(remaining) - 1:
                time.sleep(args.delay)

    # 最终保存进度
    save_progress(PROGRESS_FILE, done_ids)

    print(f"\n{'=' * 60}")
    print(f"✅ 抓取完成！")
    print(f"   成功: {success_count} 条")
    print(f"   失败: {fail_count} 条")
    print(f"   输出: {OUTPUT_FILE}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
