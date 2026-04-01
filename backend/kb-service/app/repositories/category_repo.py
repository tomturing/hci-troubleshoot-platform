"""
KB Service — 分类数据访问层

职责：
- CRUD 操作（基于 code 业务键）
- YAML 导入（两阶段：dry_run 验证 + 实际写入）
- hit_count 增量更新

注意：
- 所有操作使用 async session
- 导入操作需处理 parent_id 关系（code → id 映射）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.kb_category import KbCategory

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-category-repo")


class CategoryRepository:
    """分类数据访问层"""

    def __init__(self, db_manager: DatabaseManager):
        self._db = db_manager

    async def get_all(self) -> list[KbCategory]:
        """获取所有分类（按 level + domain 排序）"""
        trace_id = get_current_trace_id()
        async with self._db.async_session_factory() as session:
            result = await session.execute(
                select(KbCategory)
                .order_by(KbCategory.level, KbCategory.domain, KbCategory.code)
            )
            categories = result.scalars().all()
            logger.info(
                event="repo_get_all",
                count=len(categories),
                trace_id=trace_id,
            )
            return list(categories)

    async def get_all_active(self) -> list[KbCategory]:
        """获取所有活跃分类（is_active=True）"""
        trace_id = get_current_trace_id()
        async with self._db.async_session_factory() as session:
            result = await session.execute(
                select(KbCategory)
                .where(KbCategory.is_active.is_(True))
                .order_by(KbCategory.level, KbCategory.domain, KbCategory.code)
            )
            categories = result.scalars().all()
            logger.info(
                event="repo_get_all_active",
                count=len(categories),
                trace_id=trace_id,
            )
            return list(categories)

    async def get_by_code(self, code: str) -> KbCategory | None:
        """根据业务键 code 获取分类"""
        trace_id = get_current_trace_id()
        async with self._db.async_session_factory() as session:
            result = await session.execute(
                select(KbCategory).where(KbCategory.code == code)
            )
            category = result.scalar_one_or_none()
            logger.info(
                event="repo_get_by_code",
                code=code,
                found=category is not None,
                trace_id=trace_id,
            )
            return category

    async def get_by_id(self, category_id: int) -> KbCategory | None:
        """根据主键 id 获取分类"""
        async with self._db.async_session_factory() as session:
            result = await session.execute(
                select(KbCategory).where(KbCategory.id == category_id)
            )
            return result.scalar_one_or_none()

    async def update(
        self,
        code: str,
        name: str | None = None,
        is_active: bool | None = None,
        keywords: list[str] | None = None,
    ) -> KbCategory | None:
        """更新分类属性"""
        trace_id = get_current_trace_id()
        async with self._db.async_session_factory() as session:
            # 先查询是否存在
            result = await session.execute(
                select(KbCategory).where(KbCategory.code == code)
            )
            category = result.scalar_one_or_none()
            if not category:
                logger.warning(
                    event="repo_update_not_found",
                    code=code,
                    trace_id=trace_id,
                )
                return None

            # 更新字段
            if name is not None:
                category.name = name
            if is_active is not None:
                category.is_active = is_active
            if keywords is not None:
                category.keywords = keywords

            await session.commit()
            await session.refresh(category)

            logger.info(
                event="repo_update_success",
                code=code,
                name=name,
                is_active=is_active,
                trace_id=trace_id,
            )
            return category

    async def increment_hit_count(self, code: str) -> KbCategory | None:
        """增加命中计数（原子操作）"""
        trace_id = get_current_trace_id()
        async with self._db.async_session_factory() as session:
            # 使用 UPDATE 语句原子更新
            result = await session.execute(
                update(KbCategory)
                .where(KbCategory.code == code)
                .values(hit_count=KbCategory.hit_count + 1)
                .returning(KbCategory)
            )
            category = result.scalar_one_or_none()
            if not category:
                logger.warning(
                    event="repo_hit_increment_not_found",
                    code=code,
                    trace_id=trace_id,
                )
                return None

            await session.commit()
            logger.info(
                event="repo_hit_increment_success",
                code=code,
                new_hit_count=category.hit_count,
                trace_id=trace_id,
            )
            return category

    async def import_from_yaml(
        self,
        content: bytes,
        dry_run: bool = False,
    ) -> dict:
        """从 YAML 导入分类数据（两阶段）

        Args:
            content: YAML 文件内容（bytes）
            dry_run: True=仅验证不写入，False=实际写入

        Returns:
            {
                "success": bool,
                "dry_run": bool,
                "total": int,          # YAML 中的分类总数
                "created": int,        # 新创建的数量
                "updated": int,        # 更新的数量
                "skipped": int,        # 跳过的数量（无变化）
                "errors": list[str],   # 错误信息
                "details": list[dict], # 每条记录的处理结果
            }
        """
        trace_id = get_current_trace_id()
        errors: list[str] = []
        details: list[dict] = []

        # 解析 YAML
        try:
            data = yaml.safe_load(content)
            if not data or "categories" not in data:
                errors.append("YAML 格式错误：缺少 categories 字段")
                return {
                    "success": False,
                    "dry_run": dry_run,
                    "total": 0,
                    "created": 0,
                    "updated": 0,
                    "skipped": 0,
                    "errors": errors,
                    "details": details,
                }
        except yaml.YAMLError as e:
            errors.append(f"YAML 解析失败：{str(e)}")
            return {
                "success": False,
                "dry_run": dry_run,
                "total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": errors,
                "details": details,
            }

        categories_data = data["categories"]
        total = len(categories_data)
        created = 0
        updated = 0
        skipped = 0

        logger.info(
            event="repo_import_start",
            dry_run=dry_run,
            total=total,
            trace_id=trace_id,
        )

        # 构建现有分类的 code → id 映射（用于处理 parent_code）
        async with self._db.async_session_factory() as session:
            # 第一阶段：构建映射表
            existing_result = await session.execute(select(KbCategory.code, KbCategory.id))
            code_to_id = {row[0]: row[1] for row in existing_result.fetchall() if row[0]}

            # 处理每条记录
            for idx, cat_data in enumerate(categories_data):
                cat_code = cat_data.get("id")  # YAML 中 id 字段对应 code
                if not cat_code:
                    errors.append(f"第 {idx + 1} 条记录缺少 id 字段")
                    details.append({"index": idx + 1, "status": "error", "reason": "缺少 id"})
                    continue

                # 解析 parent_code
                parent_code = cat_data.get("parent_id")  # YAML 中 parent_id 是 parent_code
                parent_id = None
                if parent_code:
                    if parent_code in code_to_id:
                        parent_id = code_to_id[parent_code]
                    else:
                        # 父节点尚未处理（可能在本批次中），暂时跳过
                        # 实际导入时需要按层级顺序处理
                        errors.append(f"第 {idx + 1} 条记录 parent_id={parent_code} 未找到")
                        details.append({
                            "index": idx + 1,
                            "code": cat_code,
                            "status": "error",
                            "reason": f"parent_id {parent_code} 未找到",
                        })
                        continue

                # 验证字段
                level = cat_data.get("level")
                if level not in KbCategory.VALID_LEVELS:
                    errors.append(f"第 {idx + 1} 条记录 level={level} 非法")
                    details.append({
                        "index": idx + 1,
                        "code": cat_code,
                        "status": "error",
                        "reason": f"level {level} 非法",
                    })
                    continue

                if dry_run:
                    # 仅验证，不写入
                    if cat_code in code_to_id:
                        details.append({
                            "index": idx + 1,
                            "code": cat_code,
                            "status": "would_update",
                            "name": cat_data.get("name"),
                        })
                        updated += 1
                    else:
                        details.append({
                            "index": idx + 1,
                            "code": cat_code,
                            "status": "would_create",
                            "name": cat_data.get("name"),
                        })
                        created += 1
                else:
                    # 实际写入：使用 upsert（ON CONFLICT）
                    try:
                        stmt = insert(KbCategory).values(
                            code=cat_code,
                            name=cat_data.get("name", ""),
                            level=level,
                            domain=cat_data.get("domain"),
                            parent_id=parent_id,
                            path_labels=cat_data.get("path_labels", []),
                            keywords=cat_data.get("keywords", []),
                            source=cat_data.get("source", "manual"),
                            version=cat_data.get("version", "1.0"),
                            is_active=True,
                            hit_count=0,
                        ).on_conflict_do_update(
                            index_elements=["code"],
                            set_={
                                "name": cat_data.get("name", ""),
                                "level": level,
                                "domain": cat_data.get("domain"),
                                "parent_id": parent_id,
                                "path_labels": cat_data.get("path_labels", []),
                                "keywords": cat_data.get("keywords", []),
                                "source": cat_data.get("source", "manual"),
                                "version": cat_data.get("version", "1.0"),
                            },
                        )
                        result = await session.execute(stmt)

                        # 判断是 insert 还是 update
                        if result.inserted_primary_key:
                            # 新创建的记录
                            created += 1
                            details.append({
                                "index": idx + 1,
                                "code": cat_code,
                                "status": "created",
                                "name": cat_data.get("name"),
                            })
                            # 更新映射表（后续记录可能引用）
                            code_to_id[cat_code] = result.inserted_primary_key[0]
                        else:
                            # 更新已有记录
                            updated += 1
                            details.append({
                                "index": idx + 1,
                                "code": cat_code,
                                "status": "updated",
                                "name": cat_data.get("name"),
                            })
                    except Exception as e:
                        errors.append(f"第 {idx + 1} 条记录写入失败：{str(e)}")
                        details.append({
                            "index": idx + 1,
                            "code": cat_code,
                            "status": "error",
                            "reason": str(e),
                        })

            if not dry_run:
                await session.commit()

        success = len(errors) == 0
        logger.info(
            event="repo_import_done",
            dry_run=dry_run,
            total=total,
            created=created,
            updated=updated,
            skipped=skipped,
            errors=len(errors),
            success=success,
            trace_id=trace_id,
        )

        return {
            "success": success,
            "dry_run": dry_run,
            "total": total,
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
            "details": details,
        }

    async def get_stats(self) -> dict:
        """获取分类统计信息"""
        trace_id = get_current_trace_id()
        async with self._db.async_session_factory() as session:
            # 总数
            total_result = await session.execute(
                select(KbCategory.id)
            )
            total = len(total_result.fetchall())

            # 活跃数
            active_result = await session.execute(
                select(KbCategory.id).where(KbCategory.is_active.is_(True))
            )
            active = len(active_result.fetchall())

            # 按域分组统计
            domain_result = await session.execute(
                select(KbCategory.domain, KbCategory.hit_count)
                .where(KbCategory.is_active.is_(True))
            )
            domain_stats: dict[str, dict] = {}
            total_hits = 0
            for row in domain_result.fetchall():
                domain = row[0] or "未分类"
                hit_count = row[1] or 0
                total_hits += hit_count
                if domain not in domain_stats:
                    domain_stats[domain] = {"count": 0, "total_hits": 0}
                domain_stats[domain]["count"] += 1
                domain_stats[domain]["total_hits"] += hit_count

            logger.info(
                event="repo_get_stats",
                total=total,
                active=active,
                total_hits=total_hits,
                domains=len(domain_stats),
                trace_id=trace_id,
            )

            return {
                "total": total,
                "active": active,
                "inactive": total - active,
                "total_hits": total_hits,
                "domains": domain_stats,
            }
