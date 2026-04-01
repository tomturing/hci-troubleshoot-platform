"""
KB Service — 分类业务服务层

职责：
- 内存缓存管理（198 条，TTL=5 分钟）
- 按域分组查询
- hit_count 业务逻辑
- 统计信息聚合

缓存设计：
- 数据量：约 198 条分类，内存占用可控
- TTL：5 分钟（平衡实时性与性能）
- 刷新触发：导入、更新、过期
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

from app.models.kb_category import KbCategory
from app.repositories.category_repo import CategoryRepository

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-category-service")

# 缓存 TTL（秒）
CACHE_TTL_SECONDS = 300  # 5 分钟


class CategoryService:
    """分类业务服务层"""

    def __init__(self, db_manager: DatabaseManager):
        self._repo = CategoryRepository(db_manager)
        # 内存缓存
        self._cache: list[KbCategory] | None = None
        self._cache_timestamp: float = 0
        self._cache_by_code: dict[str, KbCategory] = {}
        self._cache_by_domain: dict[str, list[KbCategory]] = defaultdict(list)

    def _is_cache_valid(self) -> bool:
        """检查缓存是否有效（未过期）"""
        if self._cache is None:
            return False
        return (time.monotonic() - self._cache_timestamp) < CACHE_TTL_SECONDS

    async def _refresh_cache(self) -> None:
        """刷新内存缓存"""
        trace_id = get_current_trace_id()
        t_start = time.monotonic()

        # 从数据库加载活跃分类
        categories = await self._repo.get_all_active()
        self._cache = categories
        self._cache_timestamp = time.monotonic()

        # 重建索引
        self._cache_by_code = {}
        self._cache_by_domain = defaultdict(list)
        for cat in categories:
            if cat.code:
                self._cache_by_code[cat.code] = cat
            domain = cat.domain or "未分类"
            self._cache_by_domain[domain].append(cat)

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.info(
            event="cache_refreshed",
            count=len(categories),
            domains=len(self._cache_by_domain),
            elapsed_ms=elapsed_ms,
            trace_id=trace_id,
        )

    async def get_all_active(self, force_refresh: bool = False) -> list[KbCategory]:
        """获取所有活跃分类

        Args:
            force_refresh: 强制刷新缓存（用于导入后立即生效）

        Returns:
            活跃分类列表（按 level + domain 排序）
        """
        trace_id = get_current_trace_id()

        if force_refresh or not self._is_cache_valid():
            await self._refresh_cache()

        logger.info(
            event="service_get_all_active",
            count=len(self._cache or []),
            cache_valid=self._is_cache_valid(),
            trace_id=trace_id,
        )
        return self._cache or []

    async def get_grouped_by_domain(self, force_refresh: bool = False) -> dict[str, list[KbCategory]]:
        """按一级技术域分组获取分类

        Args:
            force_refresh: 强制刷新缓存

        Returns:
            { domain: [KbCategory, ...] } 字典
        """
        trace_id = get_current_trace_id()

        if force_refresh or not self._is_cache_valid():
            await self._refresh_cache()

        logger.info(
            event="service_get_grouped_by_domain",
            domains=len(self._cache_by_domain),
            trace_id=trace_id,
        )
        return dict(self._cache_by_domain)

    async def get_by_code(self, code: str) -> KbCategory | None:
        """根据业务键获取分类（优先使用缓存）"""
        trace_id = get_current_trace_id()

        # 先查缓存
        if self._is_cache_valid() and code in self._cache_by_code:
            logger.info(
                event="service_get_by_code_cache_hit",
                code=code,
                trace_id=trace_id,
            )
            return self._cache_by_code[code]

        # 缓存未命中，直接查数据库
        category = await self._repo.get_by_code(code)
        logger.info(
            event="service_get_by_code_db_query",
            code=code,
            found=category is not None,
            trace_id=trace_id,
        )
        return category

    async def increment_hit_count(self, code: str) -> int:
        """增加命中计数

        Args:
            code: 分类业务键

        Returns:
            更新后的 hit_count，若分类不存在返回 -1
        """
        trace_id = get_current_trace_id()

        category = await self._repo.increment_hit_count(code)
        if not category:
            logger.warning(
                event="service_hit_increment_failed",
                code=code,
                reason="分类不存在",
                trace_id=trace_id,
            )
            return -1

        # 同步更新缓存中的 hit_count
        if self._is_cache_valid() and code in self._cache_by_code:
            self._cache_by_code[code].hit_count = category.hit_count

        logger.info(
            event="service_hit_increment_success",
            code=code,
            hit_count=category.hit_count,
            trace_id=trace_id,
        )
        return category.hit_count

    async def update(
        self,
        code: str,
        name: str | None = None,
        is_active: bool | None = None,
        keywords: list[str] | None = None,
    ) -> KbCategory | None:
        """更新分类属性

        注意：更新后会强制刷新缓存
        """
        trace_id = get_current_trace_id()

        category = await self._repo.update(
            code=code,
            name=name,
            is_active=is_active,
            keywords=keywords,
        )
        if not category:
            logger.warning(
                event="service_update_failed",
                code=code,
                reason="分类不存在",
                trace_id=trace_id,
            )
            return None

        # 强制刷新缓存
        await self._refresh_cache()

        logger.info(
            event="service_update_success",
            code=code,
            name=name,
            is_active=is_active,
            trace_id=trace_id,
        )
        return category

    async def import_from_yaml(
        self,
        content: bytes,
        dry_run: bool = False,
    ) -> dict:
        """导入 YAML 分类数据

        Args:
            content: YAML 文件内容
            dry_run: True=仅验证不写入

        Returns:
            导入结果详情

        注意：实际导入后会强制刷新缓存
        """
        trace_id = get_current_trace_id()

        result = await self._repo.import_from_yaml(content, dry_run)

        # 实际导入成功后刷新缓存
        if not dry_run and result.get("success"):
            await self._refresh_cache()
            logger.info(
                event="service_import_cache_refreshed",
                created=result.get("created", 0),
                updated=result.get("updated", 0),
                trace_id=trace_id,
            )

        logger.info(
            event="service_import_done",
            dry_run=dry_run,
            success=result.get("success"),
            created=result.get("created", 0),
            updated=result.get("updated", 0),
            errors=len(result.get("errors", [])),
            trace_id=trace_id,
        )
        return result

    async def get_stats(self) -> dict:
        """获取分类统计信息

        Returns:
            {
                "total": int,
                "active": int,
                "inactive": int,
                "total_hits": int,
                "domains": { domain: { count, total_hits } },
                "cache_status": { valid, age_seconds, count },
            }
        """
        trace_id = get_current_trace_id()

        # 获取数据库统计
        db_stats = await self._repo.get_stats()

        # 添加缓存状态
        cache_valid = self._is_cache_valid()
        cache_age = 0
        if self._cache_timestamp > 0:
            cache_age = int(time.monotonic() - self._cache_timestamp)

        stats = {
            **db_stats,
            "cache_status": {
                "valid": cache_valid,
                "age_seconds": cache_age,
                "count": len(self._cache or []),
            },
        }

        logger.info(
            event="service_get_stats",
            total=stats["total"],
            active=stats["active"],
            total_hits=stats["total_hits"],
            cache_valid=cache_valid,
            trace_id=trace_id,
        )
        return stats

    def invalidate_cache(self) -> None:
        """手动失效缓存（用于测试或特殊场景）"""
        trace_id = get_current_trace_id()
        self._cache = None
        self._cache_timestamp = 0
        self._cache_by_code = {}
        self._cache_by_domain = defaultdict(list)
        logger.info(
            event="cache_invalidated",
            trace_id=trace_id,
        )
