#!/usr/bin/env python3
"""
【降级保留工具】将本地 cache 目录的图片导入到 kbd_image 表。

⚠️ 自 P0-1 起，主流程已不再依赖此脚本：
  - IMPORT 阶段通过扩展的 ingest API（KbdIngestRequest.images 字段）原子写入图片，
    流水线自身完成 kbd_image 入库，无需单独的同步步骤。
  - 本脚本仅保留两个用途：

用途 1：补录历史缺图数据
  当历史案例已通过旧版 ingest 流程入库（kbd_image 表为空）时，可使用本脚本
  回填图片数据，以便 admin-ui 重新识图按钮可用。
  用法：
    python -m kbd.upload_images_to_db --ids 26890,26891
    python -m kbd.upload_images_to_db --all

用途 2：测试 / 调试
  单独验证图片入库逻辑。

主流程请勿使用本脚本——直接走 pipeline 的 IMPORT 阶段即可。
"""

import argparse
import asyncio
import logging
from typing import Any

import asyncpg
from PIL import Image

from .config import settings

logger = logging.getLogger("kbd.upload_images_to_db")

# 图片扩展名
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

# MIME 类型映射
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


async def upload_images_for_kbd(
    support_id: str,
    pool: asyncpg.Pool,
) -> dict[str, Any]:
    """
    将单个案例的图片上传到数据库。

    Args:
        support_id: 案例 ID
        pool: asyncpg 连接池

    Returns:
        {"success": bool, "kbd_entry_id": int, "uploaded": int, "skipped": int, "error": str}
    """
    kbd_dir = settings.KBD_CACHE_DIR / support_id
    if not kbd_dir.exists():
        return {"success": False, "error": f"cache 目录不存在: {kbd_dir}"}

    # 查询 kbd_entry.id
    kbd_entry_id = await pool.fetchval(
        "SELECT id FROM kbd_entry WHERE support_id = $1", support_id
    )
    if kbd_entry_id is None:
        return {"success": False, "error": f"kbd_entry 表中不存在 support_id={support_id}"}

    # 检查是否已上传
    existing_count = await pool.fetchval(
        "SELECT COUNT(*) FROM kbd_image WHERE kbd_entry_id = $1", kbd_entry_id
    )
    if existing_count > 0:
        return {
            "success": True,
            "kbd_entry_id": kbd_entry_id,
            "uploaded": 0,
            "skipped": existing_count,
            "message": "图片已存在，跳过",
        }

    # 查找所有图片文件
    img_files = sorted(
        [f for f in kbd_dir.iterdir() if f.suffix.lower() in IMAGE_EXTENSIONS and f.name.startswith("img_")]
    )

    if not img_files:
        return {"success": True, "kbd_entry_id": kbd_entry_id, "uploaded": 0, "skipped": 0, "message": "无图片文件"}

    uploaded = 0
    errors = []

    async with pool.acquire() as conn, conn.transaction():
            for img_file in img_files:
                try:
                    # 提取 seq (img_N.ext -> N)
                    seq = int(img_file.stem.split("_")[1])

                    # 读取图片数据
                    with img_file.open("rb") as f:
                        image_data = f.read()

                    # 获取 MIME 类型
                    mime_type = MIME_MAP.get(img_file.suffix.lower(), "image/png")

                    # 获取图片尺寸（使用 PIL）
                    try:
                        with Image.open(img_file) as img:
                            width, height = img.size
                    except Exception:
                        width, height = None, None

                    # 插入数据库
                    await conn.execute(
                        """
                        INSERT INTO kbd_image (kbd_entry_id, seq, image_data, mime_type, width, height)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (kbd_entry_id, seq) DO NOTHING
                        """,
                        kbd_entry_id,
                        seq,
                        image_data,
                        mime_type,
                        width,
                        height,
                    )
                    uploaded += 1

                except Exception as exc:
                    errors.append(f"img_{seq}: {exc}")

    return {
        "success": True,
        "kbd_entry_id": kbd_entry_id,
        "uploaded": uploaded,
        "skipped": 0,
        "errors": errors,
    }


async def upload_images_batch(kbd_ids: list[str]) -> dict[str, int]:
    """
    批量上传图片到数据库。

    Args:
        kbd_ids: support_id 列表

    Returns:
        {"done": N, "failed": N, "skipped": N}
    """
    pool = await asyncpg.create_pool(
        dsn=settings.asyncpg_database_url
    )

    try:
        stats = {"done": 0, "failed": 0, "skipped": 0}

        for idx, support_id in enumerate(kbd_ids, 1):
            logger.info("[%d/%d] 上传图片 support_id=%s", idx, len(kbd_ids), support_id)

            result = await upload_images_for_kbd(support_id, pool)

            if result.get("success"):
                uploaded = result.get("uploaded", 0)
                skipped = result.get("skipped", 0)
                if uploaded > 0:
                    stats["done"] += uploaded
                    logger.info(
                        "上传完成 support_id=%s uploaded=%d",
                        support_id,
                        uploaded,
                    )
                elif skipped > 0:
                    stats["skipped"] += skipped
                    logger.info("已存在 support_id=%s skipped=%d", support_id, skipped)
                else:
                    logger.info("无图片 support_id=%s", support_id)
            else:
                stats["failed"] += 1
                logger.error("上传失败 support_id=%s error=%s", support_id, result.get("error"))

        logger.info(
            "批量上传完成 done=%d failed=%d skipped=%d",
            stats["done"],
            stats["failed"],
            stats["skipped"],
        )
        return stats

    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="上传本地 cache 图片到数据库")
    parser.add_argument(
        "--ids",
        help="指定 support_id 列表（逗号分隔），例如 --ids 26890,26891",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="上传所有 cache 目录中的图片",
    )
    args = parser.parse_args()

    if not args.ids and not args.all:
        parser.error("必须指定 --ids 或 --all")

    # 确定 kbd_ids
    if args.ids:
        kbd_ids = [x.strip() for x in args.ids.split(",")]
    else:
        # 扫描 cache 目录
        cache_dir = settings.KBD_CACHE_DIR
        kbd_ids = [
            d.name
            for d in cache_dir.iterdir()
            if d.is_dir() and not d.name.endswith(".lock")
        ]

    # 运行上传
    stats = asyncio.run(upload_images_batch(kbd_ids))
    print(f"上传完成: done={stats['done']} failed={stats['failed']} skipped={stats['skipped']}")


if __name__ == "__main__":
    main()
