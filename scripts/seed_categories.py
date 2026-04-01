"""
scripts/seed_categories.py — 将 category_baseline.yaml 导入 kb_category 表

功能：
  1. 解析 backend/kb-service/config/category_baseline.yaml
  2. 提取每个叶节点的 id、label、domain、path
  3. 调用 OpenAI-compatible embedding API 生成语义向量
  4. 批量插入到 kb_category 表（幂等执行）

幂等规则：
  - code（YAML id）作为唯一键
  - 已存在 → 更新 name/domain/path_labels/embedding
  - 不存在 → 插入新记录

CLI 用法：
  uv run python -m scripts.seed_categories
  uv run python -m scripts.seed_categories --dry-run  # 仅打印，不执行
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import asyncpg
import yaml
from openai import AsyncOpenAI

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent

# 默认配置路径
DEFAULT_YAML_PATH = _PROJECT_ROOT / "backend" / "kb-service" / "config" / "category_baseline.yaml"

# Embedding 模型配置
EMBEDDING_MODEL = "text-embedding-3-small"  # 1536 维向量
EMBEDDING_DIMENSION = 1536
EMBEDDING_BATCH_SIZE = 100  # OpenAI embedding API 单次最多处理的文本数
EMBEDDING_TIMEOUT = 30.0

logger = logging.getLogger("seed_categories")


# ─── 配置读取 ────────────────────────────────────────────────────────────────


def get_database_url() -> str:
    """从环境变量读取 DATABASE_URL"""
    import os
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://hci_user:hci_pass@localhost:5432/hci_db",
    )


def get_api_key() -> str:
    """从环境变量读取 ZAI_API_KEY"""
    import os
    api_key = os.environ.get("ZAI_API_KEY", "")
    if not api_key:
        logger.warning("ZAI_API_KEY 未设置，将无法生成 embedding")
    return api_key


def get_base_url() -> str:
    """从环境变量读取 ZAI_BASE_URL"""
    import os
    return os.environ.get("ZAI_BASE_URL", "https://api.z.ai/v1")


# ─── YAML 解析 ───────────────────────────────────────────────────────────────


def load_categories(yaml_path: Path) -> list[dict[str, Any]]:
    """加载 category_baseline.yaml，返回分类列表"""
    if not yaml_path.exists():
        raise FileNotFoundError(f"YAML 文件不存在: {yaml_path}")

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    categories = data.get("categories", [])
    if not categories:
        raise ValueError(f"YAML 文件无 categories 字段或为空: {yaml_path}")

    logger.info("加载 %d 个分类节点", len(categories))
    return categories


def build_embedding_text(cat: dict[str, Any]) -> str:
    """
    构建 embedding 输入文本。

    格式：label + " > " + " > ".join(path)
    例如："虚拟机创建失败 > 虚拟机 > 虚拟机创建 > 虚拟机创建失败"
    """
    label = cat.get("label", "")
    path = cat.get("path", [])

    if path:
        path_str = " > ".join(str(p) for p in path)
        return f"{label} > {path_str}"
    else:
        return label


# ─── Embedding 生成 ───────────────────────────────────────────────────────────


async def generate_embeddings(
    texts: list[str],
    client: AsyncOpenAI,
) -> list[list[float]]:
    """
    批量生成 embedding 向量。

    Args:
        texts: 文本列表（最多 EMBEDDING_BATCH_SIZE 条）
        client: OpenAI Async 客户端

    Returns:
        embedding 向量列表（每个向量长度为 EMBEDDING_DIMENSION）
    """
    if not texts:
        return []

    try:
        response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=texts,
            dimensions=EMBEDDING_DIMENSION,
            timeout=EMBEDDING_TIMEOUT,
        )

        embeddings = [item.embedding for item in response.data]
        logger.debug("生成 %d 个 embedding，model=%s", len(embeddings), EMBEDDING_MODEL)
        return embeddings

    except Exception as exc:
        logger.error("Embedding API 调用失败: %s", exc)
        raise


async def generate_all_embeddings(
    categories: list[dict[str, Any]],
    client: AsyncOpenAI,
) -> dict[str, list[float]]:
    """
    为所有分类生成 embedding，返回 {code: embedding} 映射。
    """
    code_to_embedding: dict[str, list[float]] = {}

    # 分批处理（避免 API 限流）
    total = len(categories)
    batch_size = EMBEDDING_BATCH_SIZE

    for start in range(0, total, batch_size):
        end = min(start + batch_size, total)
        batch = categories[start:end]

        texts = [build_embedding_text(cat) for cat in batch]
        codes = [cat.get("id", "") for cat in batch]

        logger.info("生成 embedding [%d-%d/%d]", start + 1, end, total)

        embeddings = await generate_embeddings(texts, client)

        for code, embedding in zip(codes, embeddings):
            if code:
                code_to_embedding[code] = embedding

        # 避免 API 限流，每批次间隔 0.5 秒
        if end < total:
            await asyncio.sleep(0.5)

    logger.info("共生成 %d 个 embedding", len(code_to_embedding))
    return code_to_embedding


# ─── 数据库操作 ───────────────────────────────────────────────────────────────


async def insert_category(
    pool: asyncpg.Pool,
    cat: dict[str, Any],
    embedding: list[float] | None,
) -> str:
    """
    插入或更新单个分类记录。

    Returns:
        "inserted" | "updated" | "skipped"
    """
    code = cat.get("id", "")
    if not code:
        logger.warning("分类缺少 id 字段，跳过: %s", cat)
        return "skipped"

    name = cat.get("label", "")
    domain = cat.get("domain", "")
    path = cat.get("path", [])
    level = len(path)

    # path_labels 转为 JSONB
    path_labels_json = json.dumps(path, ensure_ascii=False)

    # embedding 转为 PostgreSQL vector 格式
    embedding_str = None
    if embedding:
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

    # 幂等执行：检查是否已存在
    existing = await pool.fetchrow(
        "SELECT id FROM kb_category WHERE code = $1",
        code,
    )

    try:
        if existing:
            # 更新已有记录
            await pool.execute(
                """UPDATE kb_category
                   SET name = $1,
                       domain = $2,
                       path_labels = $3::jsonb,
                       level = $4,
                       embedding = $5::vector,
                       updated_at = NOW()
                   WHERE code = $6""",
                name, domain, path_labels_json, level, embedding_str, code,
            )
            logger.debug("更新分类 %s: %s", code, name)
            return "updated"
        else:
            # 插入新记录
            await pool.execute(
                """INSERT INTO kb_category
                       (code, name, domain, path_labels, level, embedding, source)
                   VALUES ($1, $2, $3, $4::jsonb, $5, $6::vector, 'baseline')""",
                code, name, domain, path_labels_json, level, embedding_str,
            )
            logger.debug("插入分类 %s: %s", code, name)
            return "inserted"

    except Exception as exc:
        logger.error("数据库操作失败 code=%s: %s", code, exc)
        return "error"


async def seed_all_categories(
    categories: list[dict[str, Any]],
    pool: asyncpg.Pool,
    embeddings: dict[str, list[float]],
) -> dict[str, int]:
    """
    批量导入分类到 kb_category 表。

    Returns:
        {"inserted": N, "updated": N, "skipped": N, "error": N}
    """
    stats: dict[str, int] = {"inserted": 0, "updated": 0, "skipped": 0, "error": 0}
    total = len(categories)

    for idx, cat in enumerate(categories, 1):
        code = cat.get("id", "")
        embedding = embeddings.get(code) if code else None

        logger.info("[%d/%d] 处理分类 %s", idx, total, code)

        result = await insert_category(pool, cat, embedding)
        stats[result] = stats.get(result, 0) + 1

    logger.info(
        "导入完成 inserted=%d updated=%d skipped=%d error=%d",
        stats["inserted"], stats["updated"], stats["skipped"], stats["error"],
    )
    return stats


# ─── Dry-run 模式 ─────────────────────────────────────────────────────────────


def print_categories(categories: list[dict[str, Any]]) -> None:
    """Dry-run 模式：打印分类信息，不执行数据库操作"""
    print(f"\n共 {len(categories)} 个分类节点:\n")
    print("code | domain | level | name | embedding_text")
    print("-" * 80)

    for cat in categories:
        code = cat.get("id", "")
        domain = cat.get("domain", "")
        name = cat.get("label", "")
        path = cat.get("path", [])
        level = len(path)
        embedding_text = build_embedding_text(cat)

        print(f"{code} | {domain} | {level} | {name} | {embedding_text[:50]}...")

    print("\n提示：使用 --execute 参数执行实际导入")


# ─── 主函数 ───────────────────────────────────────────────────────────────────


async def async_main(
    yaml_path: Path,
    dry_run: bool = True,
    skip_embedding: bool = False,
) -> None:
    """异步主函数"""
    # 加载 YAML
    categories = load_categories(yaml_path)

    if dry_run:
        print_categories(categories)
        return

    # 读取配置
    database_url = get_database_url()
    api_key = get_api_key()

    if not api_key and not skip_embedding:
        logger.error("ZAI_API_KEY 未设置，无法生成 embedding。使用 --skip-embedding 跳过")
        sys.exit(1)

    # 创建数据库连接池
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    logger.info("数据库连接池已创建")

    try:
        # 生成 embedding
        embeddings: dict[str, list[float]] = {}
        if not skip_embedding and api_key:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=get_base_url(),
            )
            embeddings = await generate_all_embeddings(categories, client)

        # 导入数据库
        stats = await seed_all_categories(categories, pool, embeddings)

        # 验证结果
        count = await pool.fetchval(
            "SELECT count(*) FROM kb_category WHERE code IS NOT NULL",
        )
        logger.info("验证：kb_category 表有 %d 条记录（code IS NOT NULL）", count)

        if count != len(categories):
            logger.warning(
                "预期 %d 条记录，实际 %d 条，可能存在数据丢失",
                len(categories), count,
            )

    finally:
        await pool.close()
        logger.info("数据库连接池已关闭")


def main() -> None:
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="将 category_baseline.yaml 导入 kb_category 表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yaml-path",
        type=Path,
        default=DEFAULT_YAML_PATH,
        help="YAML 文件路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="仅打印分类信息，不执行数据库操作（默认）",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="执行实际导入（覆盖 --dry-run）",
    )
    parser.add_argument(
        "--skip-embedding",
        action="store_true",
        help="跳过 embedding 生成（仅导入分类数据）",
    )

    args = parser.parse_args()

    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 执行
    dry_run = not args.execute
    asyncio.run(async_main(
        yaml_path=args.yaml_path,
        dry_run=dry_run,
        skip_embedding=args.skip_embedding,
    ))


if __name__ == "__main__":
    main()