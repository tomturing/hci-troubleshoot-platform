"""
fetcher.py — 产品案例页面抓取器

功能：
  - 抓取 support.sangfor.com.cn 产品案例列表页（翻页）
  - 下载每篇案例正文 HTML，保存到 raw/<id>.html
  - 指数退避重试，幂等（存在则跳过）
  - 输出抓取进度到 stdout

使用：
  uv run data-pipeline/fetcher.py --help
  uv run data-pipeline/fetcher.py --limit 50    # 只抓前 50 篇
  uv run data-pipeline/fetcher.py --page-size 20 --max-pages 100
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import time
from pathlib import Path

import httpx
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────
BASE_URL = "https://support.sangfor.com.cn"
LIST_API = f"{BASE_URL}/api/cases"           # 实际路径按站点调整
DETAIL_URL_TPL = f"{BASE_URL}/cases/{{id}}"  # 详情页 URL 模板

RAW_DIR = Path(__file__).parent / "raw"
METADATA_FILE = Path(__file__).parent / "raw" / "_index.jsonl"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": BASE_URL,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────
def _retry_get(
    client: httpx.Client,
    url: str,
    max_retries: int = 4,
    base_delay: float = 1.0,
    **kwargs,
) -> httpx.Response:
    """带指数退避重试的 GET 请求"""
    for attempt in range(max_retries):
        try:
            resp = client.get(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            if attempt == max_retries - 1:
                raise
            wait = base_delay * (2**attempt)
            logger.warning("请求失败 url=%s 原因=%s 等待 %.1fs 后重试", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _load_fetched_ids() -> set[str]:
    """从 _index.jsonl 读取已抓取的 ID 集合（断点续传）"""
    if not METADATA_FILE.exists():
        return set()
    ids: set[str] = set()
    with METADATA_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError, KeyError):
                    ids.add(json.loads(line)["id"])
    return ids


def _save_metadata(meta: dict) -> None:
    """追加元数据记录到 _index.jsonl"""
    with METADATA_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# 列表页抓取
# ─────────────────────────────────────────────────────────────
def fetch_case_list(
    client: httpx.Client,
    page: int = 1,
    page_size: int = 20,
) -> list[dict]:
    """
    抓取案例列表页，返回案例元数据列表。

    实际 API 参数需根据目标网站调整，此处为示意占位。
    返回格式示例：
      [{"id": "12345", "title": "...", "tags": [...], "created_at": "..."}]
    """
    params = {
        "page": page,
        "pageSize": page_size,
        "type": "product",  # 筛选产品案例
    }
    try:
        resp = _retry_get(client, LIST_API, params=params, timeout=20.0)
        data = resp.json()
        # 不同站点结构不同，按实际情况适配 data["list"] / data["items"] 等
        return data.get("list", data.get("items", []))
    except Exception as exc:
        logger.error("列表页抓取失败 page=%d error=%s", page, exc)
        return []


# ─────────────────────────────────────────────────────────────
# 详情页抓取
# ─────────────────────────────────────────────────────────────
def fetch_case_detail(client: httpx.Client, case_id: str) -> str | None:
    """
    抓取单篇案例详情页 HTML。
    返回 HTML 字符串，失败返回 None。
    """
    url = DETAIL_URL_TPL.format(id=case_id)
    try:
        resp = _retry_get(client, url, timeout=30.0)
        return resp.text
    except Exception as exc:
        logger.warning("详情页抓取失败 id=%s error=%s", case_id, exc)
        return None


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def run(
    limit: int | None = None,
    page_size: int = 20,
    max_pages: int = 500,
) -> int:
    """
    执行抓取流程。

    Args:
        limit: 最多抓取 N 篇（None=不限制）
        page_size: 列表页每页条目数
        max_pages: 最大翻页数（防止死循环）

    Returns:
        本次新抓取的文章数量
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    fetched_ids = _load_fetched_ids()
    logger.info("已有 %d 篇案例（断点续传）", len(fetched_ids))

    new_count = 0
    pbar = tqdm(desc="抓取案例", unit="篇", dynamic_ncols=True)

    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            cases = fetch_case_list(client, page=page, page_size=page_size)
            if not cases:
                logger.info("列表页为空，抓取完成（共 %d 页）", page - 1)
                break

            for case in cases:
                case_id = str(case.get("id", ""))
                if not case_id:
                    continue

                # 断点续传：已存在则跳过
                if case_id in fetched_ids:
                    pbar.update(1)
                    continue

                # 检查 limit
                if limit is not None and new_count >= limit:
                    pbar.close()
                    logger.info("已达 limit=%d，停止抓取", limit)
                    return new_count

                # 下载详情页
                html = fetch_case_detail(client, case_id)
                if not html:
                    continue

                # 保存 HTML
                out_path = RAW_DIR / f"{case_id}.html"
                out_path.write_text(html, encoding="utf-8")

                # 记录元数据
                meta = {
                    "id": case_id,
                    "title": case.get("title", ""),
                    "tags": case.get("tags", []),
                    "source_url": DETAIL_URL_TPL.format(id=case_id),
                    "created_at": case.get("created_at", ""),
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                _save_metadata(meta)
                fetched_ids.add(case_id)
                new_count += 1
                pbar.update(1)

                # 礼貌延迟，避免给服务器造成压力
                time.sleep(0.3)

    pbar.close()
    logger.info("本次新抓取 %d 篇，累计 %d 篇", new_count, len(fetched_ids))
    return new_count


# ─────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="产品案例 HTML 抓取器")
    parser.add_argument("--limit", type=int, default=None, help="最多抓取 N 篇")
    parser.add_argument("--page-size", type=int, default=20, help="列表页每页条目数（默认 20）")
    parser.add_argument("--max-pages", type=int, default=500, help="最大翻页数（默认 500）")
    args = parser.parse_args()

    count = run(limit=args.limit, page_size=args.page_size, max_pages=args.max_pages)
    print(f"抓取完成，新增 {count} 篇")


if __name__ == "__main__":
    main()


# =============================================================================
# HCI 历史工单获取器（T16：内部支持系统工单数据管道，仅限授权内网访问）
# =============================================================================

import os  # noqa: E402
import re as _re  # noqa: E402

import httpx as _httpx  # noqa: E402


class HCICaseFetcher:
    """
    HCI 历史工单获取器。

    数据来源：公司内部支持系统 API（需提前申请访问权限）。
    接口地址从环境变量 DATA_SOURCE_URL 读取，不在代码中硬编码。
    认证 Token 从环境变量 DATA_SOURCE_TOKEN 读取。

    只处理已解决（resolved）状态的工单，过滤其他状态。
    """

    # HCI 产品在内部系统中的产品 ID
    HCI_PRODUCT_ID: str = "33"
    # 默认每页获取条目数
    DEFAULT_PAGE_SIZE: int = 50
    # HTTP 超时（秒）
    TIMEOUT: float = 30.0

    def __init__(self, base_url: str, auth_token: str) -> None:
        # 不硬编码任何 URL 或认证信息，必须从环境变量传入
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {auth_token}",
            "Accept": "application/json",
            "User-Agent": (
                "HCI-DataPipeline/1.0 "
                "(Internal; authorized data collection)"
            ),
        }

    @classmethod
    def from_env(cls) -> HCICaseFetcher:
        """从环境变量创建实例（DATA_SOURCE_URL + DATA_SOURCE_TOKEN）"""
        url = os.environ.get("DATA_SOURCE_URL", "")
        token = os.environ.get("DATA_SOURCE_TOKEN", "")
        if not url or not token:
            raise ValueError(
                "必须设置 DATA_SOURCE_URL 和 DATA_SOURCE_TOKEN 环境变量"
            )
        return cls(base_url=url, auth_token=token)

    async def fetch_batch(
        self,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> list[dict]:
        """
        获取一批工单（分页）。

        API 响应格式（内部系统标准）：
          {"code": 0, "data": {"list": [...], "total": N}}
        """
        params = {
            "productId": self.HCI_PRODUCT_ID,
            "status": "resolved",        # 只取已解决工单
            "page": page,
            "pageSize": page_size,
            "orderBy": "updatedAt",
            "orderDir": "desc",
        }
        async with _httpx.AsyncClient(
            headers=self._headers,
            timeout=self.TIMEOUT,
            follow_redirects=False,       # 不跟随重定向（防止 SSRF 风险）
        ) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/api/v1/cases",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", {}).get("list", [])
            except _httpx.HTTPStatusError as exc:
                logger.warning(
                    "工单列表请求失败 page=%d status=%d",
                    page,
                    exc.response.status_code,
                )
                return []
            except Exception as exc:
                logger.error("工单列表请求异常 page=%d error=%s", page, exc)
                return []

    async def fetch_500_cases(self) -> list[dict]:
        """
        试运行：获取最近 500 个 HCI 工单。

        分 10 页 × 50 条分批拉取，遇到数量不足时提前结束。
        """
        results: list[dict] = []
        for page in range(1, 11):           # 10 页 × 50 = 500
            batch = await self.fetch_batch(page=page, page_size=self.DEFAULT_PAGE_SIZE)
            results.extend(batch)
            logger.info("已获取 %d 条工单（第 %d 页）", len(results), page)
            if len(batch) < self.DEFAULT_PAGE_SIZE:
                break                        # 数据不足说明已到末页
        return results[:500]


# ─── 工单数据脱敏处理 ──────────────────────────────────────────────────────────

class DataAnonymizer:
    """工单数据脱敏：移除或替换可能包含客户隐私的信息"""

    # IPv4 地址替换（保留前两段网段信息，用于故障定位分析）
    _IP_RE = _re.compile(r"\b(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}\b")
    # UUID 替换
    _UUID_RE = _re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        _re.IGNORECASE,
    )
    # 密码上下文（简单规则）
    _PASSWD_RE = _re.compile(
        r"(password|passwd|密码)\s*[:=]\s*\S+",
        _re.IGNORECASE,
    )
    # 常见内网主机名格式（如 DESKTOP-XXXXX、SERVER01）
    _HOSTNAME_RE = _re.compile(
        r"\b(DESKTOP|LAPTOP|SERVER|NODE|HOST)-[A-Z0-9]{4,12}\b",
        _re.IGNORECASE,
    )

    def anonymize(self, text: str) -> str:
        """对文本进行脱敏处理，返回脱敏后的文本"""
        text = self._IP_RE.sub(r"\1.\2.x.x", text)
        text = self._UUID_RE.sub("[UUID]", text)
        text = self._PASSWD_RE.sub(r"\1: [REDACTED]", text)
        text = self._HOSTNAME_RE.sub(r"\1-[HOSTNAME]", text)
        return text


# ─── 工单质量评分器 ────────────────────────────────────────────────────────────

class CaseQualityScorer:
    """
    工单质量评分（0-100），评估工单是否值得入库作为诊断参考。

    高质量工单特征：有错误码、有根因分析、有可执行解决方案、有命令输出证据。
    """

    WEIGHTS: dict[str, int] = {
        "has_error_code": 15,        # 有明确错误码（如 0x001、E1001）
        "has_root_cause": 25,        # 有根因分析（"原因"等关键词）
        "has_resolution": 20,        # 有解决方案（"解决"、"修复"等）
        "has_command_output": 15,    # 有命令输出证据（代码块或 acli 命令）
        "category_identified": 10,   # 有明确故障分类
        "sufficient_detail": 10,     # 正文长度超过 500 字（内容充足）
        "resolved_status": 5,        # 工单状态为已解决
    }

    # 错误码识别模式
    _ERROR_CODE_RE = _re.compile(
        r"(0x[0-9a-f]{2,8}|E\d{3,6}|错误码\s*[:：]\s*[\w\-]+)",
        _re.IGNORECASE,
    )

    def score(self, case: dict) -> int:
        """计算工单质量分，返回 0-100 整数"""
        content = str(case.get("content_text", "") or case.get("content", ""))
        total = 0

        if self._ERROR_CODE_RE.search(content):
            total += self.WEIGHTS["has_error_code"]
        if len(content) > 200 and ("原因" in content or "根因" in content or "cause" in content.lower()):
            total += self.WEIGHTS["has_root_cause"]
        if "解决" in content or "修复" in content or "resolved" in content.lower():
            total += self.WEIGHTS["has_resolution"]
        if "acli" in content or "```" in content or "命令" in content:
            total += self.WEIGHTS["has_command_output"]
        if case.get("category") or case.get("fault_category"):
            total += self.WEIGHTS["category_identified"]
        if len(content) > 500:
            total += self.WEIGHTS["sufficient_detail"]
        if str(case.get("status", "")).lower() in ("resolved", "closed", "已解决"):
            total += self.WEIGHTS["resolved_status"]

        return min(100, total)

