"""
enricher.py — 元数据补全（category / keywords 打标）

功能：
  - 读取 converted/<id>.json
  - 根据 config/keywords_map.json 做规则匹配，判断 fault_category
  - 可选：调用 ZAI LLM 对 content 做分类打标（需 ZAI_API_KEY 环境变量）
  - 输出 enriched/<id>.json，新增字段：
      "fault_category": "vm_power_failure",   # 故障类别（规则匹配 + LLM）
      "keywords": ["虚拟机", "断电"],          # 关键词列表
      "summary": "..."                         # 可选 LLM 摘要（100字以内）
  - 幂等：已存在 enriched/<id>.json 则跳过

使用：
  uv run data-pipeline/enricher.py               # 规则匹配（无 LLM，快速）
  uv run data-pipeline/enricher.py --llm         # 启用 LLM 打标
  uv run data-pipeline/enricher.py --llm --limit 100
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import httpx
from tqdm import tqdm

CONVERTED_DIR = Path(__file__).parent / "converted"
ENRICHED_DIR = Path(__file__).parent / "enriched"
KEYWORDS_MAP_PATH = Path(__file__).parent / "config" / "keywords_map.json"

ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.getenv("ENRICH_LLM_MODEL", "glm-4-flash")  # 低成本打标模型

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 规则匹配
# ─────────────────────────────────────────────────────────────
def _load_keywords_map() -> dict[str, list[str]]:
    """加载 config/keywords_map.json，结构 {category: [keyword, ...]}"""
    if not KEYWORDS_MAP_PATH.exists():
        logger.warning("keywords_map.json 不存在，使用空字典")
        return {}
    with KEYWORDS_MAP_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _rule_classify(text: str, keywords_map: dict[str, list[str]]) -> tuple[str, list[str]]:
    """
    基于关键词规则匹配 fault_category。

    Returns:
        (category, matched_keywords)  category="unknown" 表示未命中
    """
    text_lower = text.lower()
    best_category = "unknown"
    best_score = 0
    best_keywords: list[str] = []

    for category, keywords in keywords_map.items():
        matched = [kw for kw in keywords if kw.lower() in text_lower]
        if len(matched) > best_score:
            best_score = len(matched)
            best_category = category
            best_keywords = matched

    return best_category, best_keywords


# ─────────────────────────────────────────────────────────────
# LLM 打标（可选）
# ─────────────────────────────────────────────────────────────
CLASSIFY_PROMPT = """\
你是 HCI 超融合基础设施故障分类专家。根据以下案例内容，完成两件事：

1. 从候选类别中选出最匹配的 fault_category（只选一个，若都不匹配则填 "other"）：
   - vm_power_failure（虚拟机电源/断电故障）
   - vm_boot_failure（虚拟机无法启动/引导）
   - network_failure（网络故障/连接中断）
   - node_failure（节点宕机/硬件故障）
   - storage_failure（存储/磁盘故障）
   - other（其他）

2. 用 100 字以内概括案例要点（summary）

输出严格 JSON：{"fault_category": "...", "summary": "..."}
不要输出其他任何内容。

案例标题：{title}
案例内容（前 800 字）：{content}"""


def _llm_classify(title: str, content: str) -> dict:
    """
    调用 ZAI LLM 进行分类打标。
    返回 {"fault_category": ..., "summary": ...} 或空字典（失败时）。
    """
    if not ZAI_API_KEY:
        logger.debug("ZAI_API_KEY 未设置，跳过 LLM 打标")
        return {}

    prompt = CLASSIFY_PROMPT.format(title=title, content=content[:800])
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 200,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{ZAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {ZAI_API_KEY}"},
                json=payload,
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            # 去除可能的 markdown 包裹
            if raw.startswith("```"):
                raw = "\n".join(raw.splitlines()[1:]).rstrip("`").strip()
            return json.loads(raw)
    except Exception as exc:
        logger.warning("LLM 打标失败 error=%s", exc)
        return {}


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def run(limit: int | None = None, use_llm: bool = False) -> int:
    """
    执行补全流程。

    Returns:
        本次新处理的文件数量
    """
    ENRICHED_DIR.mkdir(parents=True, exist_ok=True)
    keywords_map = _load_keywords_map()
    logger.info("规则词典加载完成，共 %d 个类别", len(keywords_map))

    json_files = sorted(CONVERTED_DIR.glob("*.json"))
    if not json_files:
        logger.warning("converted/ 目录为空，请先运行 converter.py")
        return 0

    new_count = 0
    for json_path in tqdm(json_files, desc="元数据补全", unit="篇"):
        case_id = json_path.stem

        out_path = ENRICHED_DIR / f"{case_id}.json"
        if out_path.exists():
            continue

        if limit is not None and new_count >= limit:
            break

        doc = json.loads(json_path.read_text(encoding="utf-8"))
        title = doc.get("title", "")
        content = doc.get("content_md", "")

        # 规则匹配
        category, matched_kws = _rule_classify(f"{title}\n{content}", keywords_map)

        # LLM 补全（可选）
        llm_result: dict = {}
        if use_llm:
            llm_result = _llm_classify(title, content)

        enriched = {
            **doc,
            "fault_category": llm_result.get("fault_category") or category,
            "keywords": matched_kws,
            "summary": llm_result.get("summary", ""),
        }

        out_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
        new_count += 1

    logger.info("本次新补全 %d 篇", new_count)
    return new_count


def main() -> None:
    parser = argparse.ArgumentParser(description="案例元数据补全（分类 + 摘要）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 篇")
    parser.add_argument("--llm", action="store_true", help="启用 ZAI LLM 打标（需要 ZAI_API_KEY）")
    args = parser.parse_args()

    count = run(limit=args.limit, use_llm=args.llm)
    print(f"补全完成，新增 {count} 篇")


if __name__ == "__main__":
    main()
