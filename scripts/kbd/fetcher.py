"""
scripts/kbd/fetcher.py — 源数据抓取器（文件存储版）

功能：
  1. 从 support.sangfor.com.cn API（Cookie 认证）抓取 KBD 案例详情
  2. 将完整 API 响应写入 cache/{support_id}/raw.json（存在则跳过）
  3. 从 content HTML 提取所有图片 URL，下载到 cache/{support_id}/img_N.{ext}
     - 已下载的图片（img_N.* 存在）跳过
     - 下载失败写 img_N.failed 标记
  4. 无 asyncpg 依赖——所有持久化均为文件系统操作

并发策略：
  - 案例详情：串行抓取 + 请求间隔（避免限流）
  - 图片下载：每个案例内最多 VISION_CONCURRENCY 并发

幂等键：
  - raw.json 存在且有效（JSON 可解析）= 抓取完成，整案例跳过
  - img_N.failed 存在 = 该图片上次失败，可重跑（用 --retry-images 标志）
  - {support_id}.lock（flock）防并发重复写入
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .config import settings

logger = logging.getLogger("kbd.fetcher")


# ─── 内部工具 ────────────────────────────────────────────────────────────────

async def _retry_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    max_retries: int = 4,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """带指数退避重试的 HTTP 请求（不重试 4xx 客户端错误）"""
    for attempt in range(max_retries):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 401:
                raise RuntimeError(
                    "Cookie 已过期（401），请重新从浏览器复制 Cookie 到 .env 的 SANGFOR_COOKIE"
                )
            if resp.status_code == 429:
                wait = base_delay * (2 ** attempt) * 2
                logger.warning("触发限流（429），等待 %.1fs", wait)
                await asyncio.sleep(wait)
                continue
            if 400 <= resp.status_code < 500:
                raise httpx.HTTPStatusError(
                    f"客户端错误 {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            if attempt == max_retries - 1:
                raise
            wait = base_delay * (2 ** attempt)
            logger.warning("请求失败 url=%s 原因=%s 等待 %.1fs", url, exc, wait)
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


from .html_utils import extract_image_urls


def _extract_metadata(rows: dict[str, Any]) -> dict[str, Any]:
    """从 API rows 提取 metadata 字段"""
    def _str_or_none(v: Any) -> str | None:
        return str(v) if v is not None and v != "" else None

    return {
        "sangfor_main_module": _str_or_none(rows.get("mainModuleNames")),
        "sangfor_sub_module":  _str_or_none(rows.get("childModuleNames")),
        "suite_version":       _str_or_none(rows.get("suiteVersion")),
        "sangfor_updated_at":  _str_or_none(rows.get("updateTime")),
        "sangfor_created_at":  _str_or_none(rows.get("createTime")),
        "create_admin_id":     _str_or_none(rows.get("createAdminId")),
        "update_admin_id":     _str_or_none(rows.get("updateAdminId")),
    }


def _make_support_url(support_id: str) -> str:
    """生成深信服原始案例页面 URL"""
    return (
        f"{settings.SANGFOR_API_BASE}/cases/list"
        f"?product_id=33&type=1&category_id={support_id}&isOpen=true"
    )


# ─── 文件存储操作 ────────────────────────────────────────────────────────────

def _case_dir(support_id: str) -> Path:
    return settings.KBD_CACHE_DIR / support_id


def _is_fetched(support_id: str) -> bool:
    """raw.json 存在且可解析 = 已抓取完成"""
    raw_path = _case_dir(support_id) / "raw.json"
    if not raw_path.exists():
        return False
    try:
        with raw_path.open(encoding="utf-8") as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, OSError):
        return False


def _write_raw(support_id: str, rows: dict[str, Any]) -> None:
    """写入 raw.json（使用文件锁防并发冲突）"""
    case_dir = _case_dir(support_id)
    case_dir.mkdir(parents=True, exist_ok=True)
    lock_path = settings.KBD_CACHE_DIR / f"{support_id}.lock"
    raw_path = case_dir / "raw.json"

    with lock_path.open("w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            raw_path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _write_fetch_failed(support_id: str, error: str) -> None:
    """写入 fetch.failed 标记"""
    case_dir = _case_dir(support_id)
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "fetch.failed").write_text(
        json.dumps({"support_id": support_id, "error": error, "time": time.time()},
                   ensure_ascii=False),
        encoding="utf-8",
    )


async def _download_image(
    client: httpx.AsyncClient,
    url: str,
    case_dir: Path,
    seq: int,
    retry_failed: bool = False,
) -> tuple[str | None, str | None]:
    """
    下载单张图片到 cache/{support_id}/img_{seq}.{ext}。

    幂等：
      - img_{seq}.* 存在 → 跳过
      - img_{seq}.failed 存在且 retry_failed=False → 跳过

    Returns:
        (local_relative_path, mime_type) 或 (None, None) 失败时
    """
    # 检查是否已下载
    existing = list(case_dir.glob(f"img_{seq}.*"))
    done = [p for p in existing if p.suffix != ".failed"]
    failed = [p for p in existing if p.suffix == ".failed"]

    if done:
        logger.debug("图片 img_%d 已存在，跳过", seq)
        return str(done[0]), None

    if failed and not retry_failed:
        logger.debug("图片 img_%d 上次下载失败，跳过（--retry-images 可重跑）", seq)
        return None, None

    # 下载
    try:
        resp = await _retry_request(
            client, "GET", url,
            timeout=settings.SANGFOR_TIMEOUT,
            follow_redirects=True,
        )
    except Exception as exc:
        logger.warning("图片下载失败 url=%s 原因=%s", url, exc)
        (case_dir / f"img_{seq}.failed").write_text(
            json.dumps({"url": url, "error": str(exc), "time": time.time()},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        return None, None

    content = resp.content
    if len(content) < settings.MIN_IMAGE_SIZE:
        logger.debug("图片过小跳过 url=%s size=%d", url, len(content))
        return None, None

    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    ext = mimetypes.guess_extension(mime) or ".jpg"
    # .jpe → .jpg 兼容处理
    if ext == ".jpe":
        ext = ".jpg"
    save_path = case_dir / f"img_{seq}{ext}"
    save_path.write_bytes(content)
    logger.debug("图片已保存 path=%s size=%d", save_path, len(content))
    return str(save_path), mime


# ─── 主抓取函数 ──────────────────────────────────────────────────────────────

async def fetch_case(
    support_id: str,
    *,
    force: bool = False,
    retry_images: bool = False,
) -> dict[str, Any] | None:
    """
    抓取单个案例并写入文件存储。

    Args:
        support_id:    深信服案例 ID（数字字符串，如 "36156"）
        force:         True 时即使 raw.json 已存在也重新抓取
        retry_images:  True 时对 img_N.failed 标记的图片重新下载

    Returns:
        {"support_id", "title", "image_count", "metadata"} 或 None（失败时）
    """
    if not force and _is_fetched(support_id):
        logger.debug("案例 %s 已抓取（raw.json 存在），跳过", support_id)
        return {"support_id": support_id, "skipped": True}

    detail_url = f"{settings.sangfor_detail_url}/{support_id}"

    async with httpx.AsyncClient(
        headers=settings.sangfor_headers,
        timeout=settings.SANGFOR_TIMEOUT,
    ) as client:
        # ── Step 1: 抓取案例详情 API ─────────────────────────────────────────
        logger.info("正在抓取案例 %s", support_id)
        try:
            resp = await _retry_request(
                client, "GET", detail_url,
                max_retries=settings.SANGFOR_MAX_RETRIES,
            )
            payload: dict[str, Any] = resp.json()
        except RuntimeError:
            # Cookie 过期等致命错误——向上抛出终止整个批次
            raise
        except Exception as exc:
            err_msg = str(exc)
            logger.error("抓取失败 support_id=%s 原因=%s", support_id, err_msg)
            _write_fetch_failed(support_id, err_msg)
            return None

        # API 正常返回 code=0
        if payload.get("code") != 0:
            err_msg = f"API 返回非零 code={payload.get('code')} msg={payload.get('msg')}"
            logger.error("抓取失败 support_id=%s 原因=%s", support_id, err_msg)
            _write_fetch_failed(support_id, err_msg)
            return None

        rows: dict[str, Any] = payload.get("rows") or {}

        # ── Step 2: 写入 raw.json ────────────────────────────────────────────
        _write_raw(support_id, rows)
        logger.info("案例 %s raw.json 已写入", support_id)

        # ── Step 3: 提取图片 URL 并下载 ──────────────────────────────────────
        content_html: str = rows.get("content") or ""
        image_urls = extract_image_urls(content_html, settings.SANGFOR_API_BASE)

        if image_urls:
            case_dir = _case_dir(support_id)
            sem = asyncio.Semaphore(settings.VISION_CONCURRENCY)

            async def _fetch_one(seq: int, url: str) -> None:
                async with sem:
                    await _download_image(client, url, case_dir, seq,
                                          retry_failed=retry_images)

            await asyncio.gather(*[_fetch_one(i, u) for i, u in enumerate(image_urls)])

        title = rows.get("name") or rows.get("title") or f"案例 {support_id}"
        metadata = _extract_metadata(rows)
        logger.info("案例 %s 抓取完成，共 %d 张图片", support_id, len(image_urls))
        return {
            "support_id": support_id,
            "title": title,
            "image_count": len(image_urls),
            "metadata": metadata,
        }


async def fetch_batch(
    support_ids: list[str],
    *,
    force: bool = False,
    retry_images: bool = False,
) -> dict[str, int]:
    """
    批量抓取案例列表。

    Args:
        support_ids:   要抓取的案例 ID 列表
        force:         True 时重新抓取已完成的案例
        retry_images:  True 时重试失败的图片下载

    Returns:
        统计字典 {"done": N, "skipped": N, "failed": N}
    """
    stats: dict[str, int] = {"done": 0, "skipped": 0, "failed": 0}
    total = len(support_ids)

    for idx, support_id in enumerate(support_ids, 1):
        logger.info("[%d/%d] 处理案例 %s", idx, total, support_id)
        try:
            result = await fetch_case(support_id, force=force, retry_images=retry_images)
            if result is None:
                stats["failed"] += 1
            elif result.get("skipped"):
                stats["skipped"] += 1
            else:
                stats["done"] += 1
        except RuntimeError as exc:
            # Cookie 过期等致命错误——停止整个批次
            logger.critical("致命错误，终止批次：%s", exc)
            raise

        # 请求间隔（避免限流）
        if idx < total:
            await asyncio.sleep(settings.SANGFOR_REQUEST_DELAY)

    logger.info("批量抓取完成 done=%d skipped=%d failed=%d",
                stats["done"], stats["skipped"], stats["failed"])
    return stats


def read_ids_from_excel(excel_path: Path | None = None) -> list[str]:
    """从 Excel 文件读取第一列的所有案例 ID（跳过标题行）"""
    import zipfile
    from xml.etree import ElementTree as ET

    fpath = excel_path or settings.EXCEL_FILE
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

    with zipfile.ZipFile(fpath) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{{{ns}}}si"):
                t_list = si.findall(f".//{{{ns}}}t")
                shared.append("".join(t.text or "" for t in t_list))

        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(f"{{{ns}}}sheetData/{{{ns}}}row")

        def _cell_val(cell: ET.Element) -> str | None:
            t = cell.get("t", "")
            v_el = cell.find(f"{{{ns}}}v")
            if v_el is None:
                return None
            if t == "s":
                return shared[int(v_el.text)]
            return v_el.text

        ids: list[str] = []
        for row in rows[1:]:  # 跳过标题行
            cells = row.findall(f"{{{ns}}}c")
            if cells:
                val = _cell_val(cells[0])
                if val and str(val).strip().isdigit():
                    ids.append(str(val).strip())
        return ids
