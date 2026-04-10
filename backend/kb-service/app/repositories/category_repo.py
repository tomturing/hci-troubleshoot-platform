"""
KB Service — 分类数据访问层

职责：
- CRUD 操作（基于 code 业务键）
- YAML 导入（两阶段：dry_run 验证 + 实际写入）
  - 支持 category_baseline.yaml 标准格式（id/label/domain/path）
  - 自动推断 level（= path 长度），同时生成 L1 域节点和中间层节点
- hit_count 增量更新

注意：
- 所有操作使用 async session
- YAML 格式不要求显式 level 字段，从 path 长度推断（DRY 原则）
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

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

    @staticmethod
    def _parse_baseline_yaml(categories_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 category_baseline.yaml 的叶节点列表展开为完整节点列表。

        基准 YAML 只包含叶节点（格式：id/label/domain/path），level 隐含于 path 长度中。
        本方法实现四阶段解析，与 scripts/kbd/seed_categories.py 逻辑等价：
          Phase 1: 从 domain 字段推断并生成 L1 域节点（code 格式：<domain>-L1）
          Phase 2: 从 L3/L4 叶节点 path 提取中间层节点（L2+，code 格式：<domain>-L<n>-<hash8>）
          Phase 3: 解析叶节点（id→code, label→name, len(path)→level, path→path_labels）
          Phase 4: 按 level 排序，确保父节点先于子节点写入

        中间层节点 code 使用 path 的 md5 前 8 位保证幂等性：
          相同 path → 相同 code，重复导入安全。

        Args:
            categories_data: YAML 中的 categories 列表（原始格式）

        Returns:
            按 level 升序排列的记录列表，每条包含：
            code, name, domain, path_labels(list), level, source
        """
        # ── Phase 1: 推断 L1 域节点 ──────────────────────────────────────────
        domains: dict[str, dict[str, Any]] = {}
        for cat in categories_data:
            domain = cat.get("domain", "")
            if domain and domain not in domains:
                domains[domain] = {
                    "code": f"{domain}-L1",
                    "name": domain,
                    "domain": domain,
                    "path_labels": [domain],
                    "level": 1,
                    "source": "baseline_yaml",
                }

        # ── Phase 2: 从 path 提取中间层节点（L3+ 叶节点才有中间层）──────────
        # 例如 path=['虚拟机','FC','FC存储添加失败'] → 中间层 ['虚拟机','FC']
        intermediate: dict[str, dict[str, Any]] = {}
        for cat in categories_data:
            path: list[str] = cat.get("path", [])
            domain: str = cat.get("domain", "")
            level: int = len(path)
            if level >= 3:
                # 提取所有中间路径（从 path[0:2] 到 path[0:level-1]）
                for depth in range(2, level):
                    sub_path = path[:depth]
                    path_key = json.dumps(sub_path, ensure_ascii=False)
                    if path_key not in intermediate:
                        # 使用路径的 md5 hash 前 8 位作为 code 后缀，保证幂等
                        hash_suffix = hashlib.md5(path_key.encode()).hexdigest()[:8]
                        intermediate[path_key] = {
                            "code": f"{domain}-L{depth}-{hash_suffix}",
                            "name": sub_path[-1],
                            "domain": domain,
                            "path_labels": sub_path,
                            "level": depth,
                            "source": "baseline_yaml",
                        }

        # ── Phase 3: 解析叶节点（YAML 原始 198 条）──────────────────────────
        # 字段映射：id→code, label→name（兼容旧格式 name），path→path_labels, len(path)→level
        leaf_records: list[dict[str, Any]] = []
        for cat in categories_data:
            path = cat.get("path", [])
            code = cat.get("id", "")
            name = cat.get("label") or cat.get("name", "")  # 兼容旧格式
            domain = cat.get("domain", "")
            level = len(path)
            if not code or not name:
                continue
            leaf_records.append({
                "code": code,
                "name": name,
                "domain": domain,
                "path_labels": path,
                "level": level,
                "source": "baseline_yaml",
            })

        # ── Phase 4: 合并并按 level 排序（父节点先于子节点写入）──────────────
        all_records = list(domains.values()) + list(intermediate.values()) + leaf_records
        all_records.sort(key=lambda r: r["level"])
        return all_records

    async def import_from_yaml(
        self,
        content: bytes,
        dry_run: bool = False,
    ) -> dict:
        """从 category_baseline.yaml 导入分类数据（两阶段 upsert）。

        支持基准 YAML 标准格式（字段：id/label/domain/path），
        level 从 path 长度自动推断，无需在 YAML 中显式声明。

        导入结果包含：
          - L1 域节点：<domain>-L1（5 条）
          - L2+ 中间层节点：从叶节点 path 提取（约 32 条）
          - 叶节点：YAML 原始数据（198 条）

        两阶段策略：
          Phase 1：全量 upsert（parent_id 暂为 NULL）
          Phase 2：根据 path_labels 批量 UPDATE parent_id（建立树形关系）

        Args:
            content: YAML 文件内容（bytes）
            dry_run: True=仅验证不写入，False=实际写入

        Returns:
            {
                "success": bool,
                "dry_run": bool,
                "yaml_categories": int,    # YAML 原始叶节点数
                "total": int,              # 实际处理节点数（含 L1 + 中间层 + 叶节点）
                "created": int,
                "updated": int,
                "skipped": int,
                "errors": list[str],
                "details": list[dict],
            }
        """
        trace_id = get_current_trace_id()
        errors: list[str] = []
        details: list[dict] = []

        # ── 解析 YAML ─────────────────────────────────────────────────────────
        try:
            data = yaml.safe_load(content)
            if not data or "categories" not in data:
                errors.append("YAML 格式错误：缺少 categories 字段")
                return {
                    "success": False,
                    "dry_run": dry_run,
                    "yaml_categories": 0,
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
                "yaml_categories": 0,
                "total": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "errors": errors,
                "details": details,
            }

        raw_categories = data["categories"]
        yaml_count = len(raw_categories)

        # ── 四阶段解析：生成完整节点列表（L1 + 中间层 + 叶节点）──────────────
        all_records = self._parse_baseline_yaml(raw_categories)
        total = len(all_records)

        logger.info(
            event="repo_import_start",
            dry_run=dry_run,
            yaml_categories=yaml_count,
            total_records=total,
            trace_id=trace_id,
        )

        created = 0
        updated = 0
        skipped = 0

        async with self._db.async_session_factory() as session:
            # 查询已存在的 code → id 映射
            existing_result = await session.execute(select(KbCategory.code, KbCategory.id))
            code_to_id: dict[str, int] = {
                row[0]: row[1] for row in existing_result.fetchall() if row[0]
            }

            # ── Phase 1: 全量 upsert（parent_id=NULL，先建立所有节点）────────
            for idx, rec in enumerate(all_records):
                code = rec["code"]
                name = rec["name"]
                level = rec["level"]
                path_labels = rec["path_labels"]
                domain = rec["domain"]
                source = rec["source"]

                # 验证 level 合法性（防御性校验）
                if level not in KbCategory.VALID_LEVELS:
                    errors.append(f"节点 {code} level={level} 非法（应为 1-4）")
                    details.append({
                        "index": idx + 1,
                        "code": code,
                        "status": "error",
                        "reason": f"level={level} 超出合法范围 1-4",
                    })
                    continue

                if dry_run:
                    # 仅验证，不写入
                    if code in code_to_id:
                        details.append({
                            "index": idx + 1,
                            "code": code,
                            "status": "would_update",
                            "name": name,
                            "level": level,
                        })
                        updated += 1
                    else:
                        details.append({
                            "index": idx + 1,
                            "code": code,
                            "status": "would_create",
                            "name": name,
                            "level": level,
                        })
                        created += 1
                else:
                    try:
                        stmt = insert(KbCategory).values(
                            code=code,
                            name=name,
                            level=level,
                            domain=domain,
                            parent_id=None,  # Phase 1 统一置 NULL，Phase 2 再更新
                            path_labels=path_labels,
                            keywords=[],
                            source=source,
                            version="1.0",
                            is_active=True,
                            hit_count=0,
                        ).on_conflict_do_update(
                            index_elements=["code"],
                            set_={
                                "name": name,
                                "level": level,
                                "domain": domain,
                                "path_labels": path_labels,
                                "source": source,
                                "version": "1.0",
                            },
                        ).returning(KbCategory.id, KbCategory.code)

                        result = await session.execute(stmt)
                        row = result.fetchone()
                        if row:
                            new_id = row[0]
                            # 判断是新建（ON CONFLICT 前）
                            if code not in code_to_id:
                                created += 1
                                details.append({
                                    "index": idx + 1,
                                    "code": code,
                                    "status": "created",
                                    "name": name,
                                    "level": level,
                                })
                                code_to_id[code] = new_id
                            else:
                                updated += 1
                                details.append({
                                    "index": idx + 1,
                                    "code": code,
                                    "status": "updated",
                                    "name": name,
                                    "level": level,
                                })
                    except Exception as e:
                        errors.append(f"节点 {code} 写入失败：{str(e)}")
                        details.append({
                            "index": idx + 1,
                            "code": code,
                            "status": "error",
                            "reason": str(e),
                        })

            # ── Phase 2: 批量 UPDATE parent_id（建立树形关系）────────────────
            # 策略：子节点的 parent path = path_labels[:-1]，从 code_to_id 通过 path 反查
            if not dry_run and not errors:
                # 构建 path_labels(JSON) → id 的映射
                path_result = await session.execute(
                    select(KbCategory.id, KbCategory.path_labels)
                    .where(KbCategory.path_labels.is_not(None))
                )
                path_to_id: dict[str, int] = {}
                for id_val, path_val in path_result.fetchall():
                    if path_val is not None:
                        key = json.dumps(path_val, ensure_ascii=False)
                        path_to_id[key] = id_val

                # 对所有非 L1 节点，计算其父节点 path，更新 parent_id
                for rec in all_records:
                    if rec["level"] <= 1:
                        continue
                    path_labels = rec["path_labels"]
                    parent_path = path_labels[:-1]
                    parent_path_key = json.dumps(parent_path, ensure_ascii=False)
                    parent_id = path_to_id.get(parent_path_key)

                    if parent_id is None:
                        # 父节点路径未找到（正常情况：L2 节点父节点是 L1，path=[domain]）
                        # L1 path_labels = [domain]，key 应该能找到
                        logger.warning(
                            event="repo_import_parent_not_found",
                            code=rec["code"],
                            parent_path=parent_path,
                            trace_id=trace_id,
                        )
                        continue

                    node_id = code_to_id.get(rec["code"])
                    if node_id:
                        await session.execute(
                            update(KbCategory)
                            .where(KbCategory.id == node_id)
                            .values(parent_id=parent_id)
                        )

                logger.info(
                    event="repo_import_parent_update_done",
                    trace_id=trace_id,
                )

            if not dry_run:
                await session.commit()

        success = len(errors) == 0
        logger.info(
            event="repo_import_done",
            dry_run=dry_run,
            yaml_categories=yaml_count,
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
            "yaml_categories": yaml_count,
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
