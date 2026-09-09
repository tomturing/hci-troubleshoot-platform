"""Bundle 工厂版本自动迁移机制

设计思路：
1. 记录每个 Bundle 使用的工厂版本
2. 当工厂版本升级时，自动检测过期的 Bundle
3. 提供批量重新编译的 API
4. 支持渐进式迁移（按需或批量）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger("bundle-migration")


# 当前 Bundle 工厂版本
CURRENT_BUNDLE_FACTORY_VERSION = "v4-fixture-assets"


@dataclass
class BundleMigrationStatus:
    """Bundle 迁移状态"""

    kbd_id: int
    support_id: str
    current_factory_version: str
    expected_factory_version: str
    needs_migration: bool
    last_compiled_at: datetime | None


def get_outdated_bundles(
    current_version: str = CURRENT_BUNDLE_FACTORY_VERSION,
) -> list[BundleMigrationStatus]:
    """获取需要迁移的 Bundle 列表

    Args:
        current_version: 当前工厂版本

    Returns:
        需要迁移的 Bundle 列表
    """
    # TODO: 实现数据库查询逻辑
    # SELECT kbd_id, support_id, factory_version, compiled_at
    # FROM bundle_metadata
    # WHERE factory_version != current_version
    pass


async def migrate_bundle(
    kbd_id: int,
    force: bool = False,
) -> dict[str, Any]:
    """迁移单个 Bundle

    Args:
        kbd_id: KBD ID
        force: 是否强制迁移（即使版本相同）

    Returns:
        迁移结果
    """
    from .offline_acquisition_compiler import compile_signal_acquisition

    # TODO: 实现迁移逻辑
    # 1. 读取 KBD 信号配置
    # 2. 使用新工厂版本重新编译
    # 3. 更新 Bundle 元数据
    # 4. 返回结果

    logger.info(
        "bundle_migration_started",
        kbd_id=kbd_id,
        target_version=CURRENT_BUNDLE_FACTORY_VERSION,
    )

    return {
        "kbd_id": kbd_id,
        "status": "success",
        "old_version": "unknown",
        "new_version": CURRENT_BUNDLE_FACTORY_VERSION,
    }


async def batch_migrate_bundles(
    kbd_ids: list[int] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """批量迁移 Bundle

    Args:
        kbd_ids: 要迁移的 KBD ID 列表，None 表示全部过期 Bundle
        dry_run: 是否只检查不执行

    Returns:
        批量迁移结果
    """
    if dry_run:
        # 只检查，不执行迁移
        outdated = get_outdated_bundles()
        return {
            "dry_run": True,
            "total_outdated": len(outdated),
            "bundles": [
                {
                    "kbd_id": item.kbd_id,
                    "support_id": item.support_id,
                    "current_version": item.current_factory_version,
                    "expected_version": item.expected_factory_version,
                }
                for item in outdated
            ],
        }

    # 执行迁移
    results = []
    targets = kbd_ids or [item.kbd_id for item in get_outdated_bundles()]

    for kbd_id in targets:
        try:
            result = await migrate_bundle(kbd_id)
            results.append(result)
        except Exception as e:
            logger.error(
                "bundle_migration_failed",
                kbd_id=kbd_id,
                error=str(e),
            )
            results.append({
                "kbd_id": kbd_id,
                "status": "failed",
                "error": str(e),
            })

    return {
        "dry_run": False,
        "total": len(targets),
        "success": sum(1 for r in results if r.get("status") == "success"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "details": results,
    }


def check_factory_version_health() -> dict[str, Any]:
    """检查工厂版本健康状态

    Returns:
        健康状态报告
    """
    outdated = get_outdated_bundles()

    return {
        "current_factory_version": CURRENT_BUNDLE_FACTORY_VERSION,
        "total_bundles": 0,  # TODO: 查询总数
        "outdated_bundles": len(outdated),
        "health_status": "healthy" if len(outdated) == 0 else "needs_migration",
        "recommendation": "所有 Bundle 都是最新版本"
        if len(outdated) == 0
        else f"有 {len(outdated)} 个 Bundle 需要迁移到版本 {CURRENT_BUNDLE_FACTORY_VERSION}",
    }
