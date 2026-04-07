"""
scripts/kbd/seed_categories.py — 从 YAML 导入分类数据到 kb_category 表

功能：
  读取 backend/kb-service/config/category_baseline.yaml，解析分类树结构，
  将分类数据导入 PostgreSQL kb_category 表。

  YAML 特点：
    - 只包含叶节点（198 条 L2-L4 分类）
    - L1 域节点（虚拟机/网络/存储/硬件/平台）需从 domain 字段推断
    - L2 中间层分组节点（如"虚拟机创建"/"FC"/"分布式防火墙"）需从 path 中提取

  导入策略：
    1. 推断并创建 L1 域节点（code 格式：<domain>-L1）
    2. 从 L3/L4 叶节点 path 中提取中间层节点（code 格式：<domain>-L<level>-<name>）
    3. 导入 YAML 叶节点（parent_id 暂为 NULL）
    4. 根据 path_labels 查找并更新所有节点的 parent_id

  导入统计（预期）：
    - L1 域节点：5 条（虚拟机/网络/存储/硬件/平台）
    - L2 中间节点：约 32 条（分组节点）
    - L2-L4 叶节点：198 条（YAML 原始数据）
    - 总计：约 235 条

幂等规则：
  - 根据 code（UNIQUE）判断是否已存在
  - 已存在：跳过（默认）
  - --force：删除已有数据重新导入

参数：
  --dry-run  仅打印 SQL，不执行写入
  --force    删除已有数据重新导入（危险操作）

使用方式：
  uv run python scripts/kbd/seed_categories.py --dry-run
  uv run python scripts/kbd/seed_categories.py
  uv run python scripts/kbd/seed_categories.py --force

输出：
  导入统计：新增/更新/跳过/错误
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

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent.parent

# 默认 YAML 文件路径
DEFAULT_YAML_PATH = _PROJECT_ROOT / "backend" / "kb-service" / "config" / "category_baseline.yaml"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("seed_categories")


def load_yaml_data(yaml_path: Path) -> list[dict[str, Any]]:
    """
    读取并解析 category_baseline.yaml 文件。

    Args:
        yaml_path: YAML 文件路径

    Returns:
        分类数据列表，每条包含 id/domain/label/path 字段
    """
    if not yaml_path.exists():
        logger.error("YAML 文件不存在: %s", yaml_path)
        sys.exit(1)

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    categories = data.get("categories", [])
    logger.info("读取 YAML 文件: %s，共 %d 条分类", yaml_path, len(categories))
    return categories


def parse_category_data(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    解析 YAML 分类数据，转换为数据库记录格式。

    字段映射：
      YAML id      → code
      YAML label   → name
      YAML domain  → domain
      YAML path    → path_labels (JSONB)
      path 长度    → level

    注意：
      - YAML 只包含叶节点（198 条 L2-L4 分类）
      - L1 域节点（虚拟机/网络/存储/硬件/平台）需从 domain 字段推断
      - 中间层节点（L2 分组节点）需从叶节点 path 中提取并生成

    Args:
        categories: YAML 原始分类列表

    Returns:
        解析后的记录列表，按层级排序（确保父节点先插入）
    """
    # ─── 第一阶段：推断并创建 L1 域节点 ─────────────────────────────────────
    domains: set[str] = set()
    for cat in categories:
        domains.add(cat.get("domain", ""))

    l1_records: list[dict[str, Any]] = []
    for domain in sorted(domains):
        if not domain:
            continue
        l1_records.append({
            "code": f"{domain}-L1",
            "name": domain,
            "domain": domain,
            "path_labels": json.dumps([domain], ensure_ascii=False),
            "level": 1,
            "parent_code": None,
            "source": "baseline_yaml",
            "version": "1.0",
        })

    # ─── 第二阶段：提取并生成中间层节点（L2 分组节点）──────────────────────
    # 从 L3/L4 叶节点的 path 中提取中间路径（path[:-1]）
    # 例如：['虚拟机', '虚拟机创建', '虚拟机创建失败'] → ['虚拟机', '虚拟机创建']
    intermediate_paths: dict[str, dict[str, Any]] = {}  # path_str → record

    for cat in categories:
        path = cat.get("path", [])
        domain = cat.get("domain", "")
        level = len(path)

        # L3 及以上节点需要中间层父节点（path[:-1]）
        if level >= 3:
            for i in range(2, level):  # 提取所有中间路径
                intermediate_path = path[:i]
                path_str = json.dumps(intermediate_path, ensure_ascii=False)

                if path_str not in intermediate_paths:
                    # 生成中间节点记录
                    # code 格式：<domain>-L2-<hash> 或使用路径最后元素
                    intermediate_name = intermediate_path[-1]
                    intermediate_code = f"{domain}-L{i}-{intermediate_path[-1]}"
                    intermediate_paths[path_str] = {
                        "code": intermediate_code,
                        "name": intermediate_name,
                        "domain": domain,
                        "path_labels": path_str,
                        "level": i,
                        "parent_code": None,
                        "source": "baseline_yaml_intermediate",
                        "version": "1.0",
                    }

    intermediate_records = list(intermediate_paths.values())
    logger.info("提取中间层节点: %d 条", len(intermediate_records))

    # ─── 第三阶段：解析叶节点（L2-L4）──────────────────────────────────────
    leaf_records: list[dict[str, Any]] = []

    for cat in categories:
        code = cat.get("id", "")
        name = cat.get("label", "")
        domain = cat.get("domain", "")
        path = cat.get("path", [])
        level = len(path)

        leaf_records.append({
            "code": code,
            "name": name,
            "domain": domain,
            "path_labels": json.dumps(path, ensure_ascii=False),
            "level": level,
            "parent_code": None,
            "source": "baseline_yaml",
            "version": "1.0",
        })

    # ─── 第四阶段：合并并按层级排序 ───────────────────────────────────────
    all_records = l1_records + intermediate_records + leaf_records
    all_records.sort(key=lambda r: r["level"])

    return all_records


async def get_db_pool(database_url: str) -> asyncpg.Pool:
    """
    创建数据库连接池。

    Args:
        database_url: PostgreSQL 连接字符串

    Returns:
        asyncpg 连接池
    """
    pool = await asyncpg.create_pool(
        database_url,
        min_size=2,
        max_size=10,
    )
    return pool


async def check_existing_categories(pool: asyncpg.Pool) -> dict[str, int]:
    """
    查询已存在的分类记录。

    Returns:
        {code: id} 映射表
    """
    rows = await pool.fetch(
        "SELECT id, code FROM kb_category WHERE code IS NOT NULL"
    )
    return {row["code"]: row["id"] for row in rows}


async def build_parent_mapping(pool: asyncpg.Pool) -> dict[str, int]:
    """
    构建 path → id 的映射表，用于查找父节点。

    策略：根据 path_labels JSONB 查找父节点。
      父节点的 path_labels 应等于子节点 path_labels[:-1]

    注意：asyncpg 读取 JSONB 时返回字符串，需用 json.loads() 解析。

    Returns:
        {path_str: id} 映射表
    """
    rows = await pool.fetch(
        "SELECT id, path_labels FROM kb_category WHERE path_labels IS NOT NULL"
    )
    mapping = {}
    for row in rows:
        path_labels_raw = row["path_labels"]
        if path_labels_raw:
            # asyncpg 返回 JSONB 为字符串，需解析为 Python list
            if isinstance(path_labels_raw, str):
                path_labels = json.loads(path_labels_raw)
            else:
                path_labels = path_labels_raw
            # 序列化为 JSON 字符串作为 key
            path_str = json.dumps(path_labels, ensure_ascii=False)
            mapping[path_str] = row["id"]
    return mapping


async def seed_categories(
    pool: asyncpg.Pool,
    records: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, int]:
    """
    导入分类数据到 kb_category 表。

    Args:
        pool: 数据库连接池
        records: 解析后的分类记录列表
        dry_run: 仅打印 SQL，不执行
        force: 删除已有数据重新导入

    Returns:
        {"created": N, "updated": N, "skipped": N, "error": N}
    """
    stats = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    # 查询已存在的记录
    existing = await check_existing_categories(pool)

    if force and existing and not dry_run:
        # 删除已有数据
        logger.warning("强制模式：删除 %d 条已有分类记录", len(existing))
        await pool.execute(
            "DELETE FROM kb_category WHERE code IS NOT NULL"
        )
        existing = {}

    # 第一阶段：导入所有节点（parent_id 暂为 NULL）
    id_mapping: dict[str, int] = {}  # code → id（新插入的）

    for rec in records:
        code = rec["code"]

        if dry_run:
            # 打印 SQL
            sql = _generate_insert_sql(rec)
            logger.info("[DRY-RUN] %s", sql)
            stats["created"] += 1
            continue

        if code in existing:
            # 已存在，跳过（幂等）
            id_mapping[code] = existing[code]
            stats["skipped"] += 1
            logger.debug("跳过已存在分类: %s", code)
            continue

        try:
            # 插入新记录
            row = await pool.fetchrow(
                """INSERT INTO kb_category
                    (code, name, domain, path_labels, level, parent_id,
                     source, version, hit_count, is_active)
                   VALUES ($1, $2, $3, $4::jsonb, $5, NULL, $6, $7, 0, TRUE)
                   RETURNING id""",
                code,
                rec["name"],
                rec["domain"],
                rec["path_labels"],
                rec["level"],
                rec["source"],
                rec["version"],
            )
            id_mapping[code] = row["id"]
            stats["created"] += 1
            logger.info("创建分类: %s (%s)", code, rec["name"])

        except Exception as e:
            stats["error"] += 1
            logger.error("创建分类失败: %s，错误: %s", code, e)

    if dry_run:
        return stats

    # 第二阶段：更新 parent_id（根据 path_labels 查找）
    parent_mapping = await build_parent_mapping(pool)

    for rec in records:
        if rec["level"] <= 1:
            # L1 节点无父节点
            continue

        code = rec["code"]
        cat_id = id_mapping.get(code)
        if not cat_id:
            continue

        # 计算父节点的 path_labels
        path_labels = json.loads(rec["path_labels"])
        parent_path = path_labels[:-1]
        parent_path_str = json.dumps(parent_path, ensure_ascii=False)

        parent_id = parent_mapping.get(parent_path_str)
        if parent_id:
            try:
                await pool.execute(
                    "UPDATE kb_category SET parent_id = $1 WHERE id = $2",
                    parent_id,
                    cat_id,
                )
                logger.debug("更新父节点: %s → parent_id=%d", code, parent_id)
            except Exception as e:
                logger.error("更新父节点失败: %s，错误: %s", code, e)

    logger.info(
        "导入完成: created=%d, updated=%d, skipped=%d, error=%d",
        stats["created"], stats["updated"], stats["skipped"], stats["error"],
    )
    return stats


def _generate_insert_sql(rec: dict[str, Any]) -> str:
    """
    生成 INSERT SQL 语句（用于 dry-run）。
    """
    return (
        f"INSERT INTO kb_category "
        f"(code, name, domain, path_labels, level, parent_id, source, version, hit_count, is_active) "
        f"VALUES ('{rec['code']}', '{rec['name']}', '{rec['domain']}', "
        f"'{rec['path_labels']}'::jsonb, {rec['level']}, NULL, "
        f"'{rec['source']}', '{rec['version']}', 0, TRUE);"
    )


async def main() -> None:
    """
    主函数：解析参数 → 读取 YAML → 导入数据库。
    """
    parser = argparse.ArgumentParser(
        description="从 YAML 导入分类数据到 kb_category 表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  uv run python scripts/kbd/seed_categories.py --dry-run
  uv run python scripts/kbd/seed_categories.py
  uv run python scripts/kbd/seed_categories.py --force
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印 SQL，不执行写入",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="删除已有数据重新导入（危险操作）",
    )
    parser.add_argument(
        "--yaml-path",
        type=Path,
        default=DEFAULT_YAML_PATH,
        help=f"YAML 文件路径（默认: {DEFAULT_YAML_PATH}）",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="数据库连接字符串（默认: 从 .env 读取 DATABASE_URL）",
    )

    args = parser.parse_args()

    # 读取数据库连接配置
    database_url = args.database_url
    if not database_url:
        # 从环境变量或 .env 文件读取
        import os
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            # 尝试从 .env 文件读取
            env_file = _PROJECT_ROOT / "scripts" / "kbd" / ".env"
            if env_file.exists():
                with open(env_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("DATABASE_URL="):
                            database_url = line.split("=", 1)[1]
                            break
            if not database_url:
                # 使用默认值
                database_url = "postgresql://hci_user:hci_pass@localhost:5432/hci_db"
                logger.warning("未配置 DATABASE_URL，使用默认值: %s", database_url)

    # 读取 YAML 数据
    categories = load_yaml_data(args.yaml_path)

    # 解析分类数据
    records = parse_category_data(categories)
    logger.info("解析完成，共 %d 条记录，按层级排序", len(records))

    # 统计各层级数量
    level_counts: dict[int, int] = {}
    for rec in records:
        level_counts[rec["level"]] = level_counts.get(rec["level"], 0) + 1
    logger.info("层级分布: %s", level_counts)

    if args.dry_run:
        # 仅打印 SQL，不连接数据库
        logger.info("=== DRY-RUN 模式 ===")
        for rec in records[:5]:  # 打印前 5 条作为示例
            sql = _generate_insert_sql(rec)
            logger.info("[DRY-RUN] %s", sql)
        logger.info("... 共 %d 条 SQL（已省略其余）", len(records))
        return

    # 连接数据库并导入
    pool = await get_db_pool(database_url)
    try:
        stats = await seed_categories(pool, records, dry_run=args.dry_run, force=args.force)
        logger.info("导入统计: %s", stats)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())