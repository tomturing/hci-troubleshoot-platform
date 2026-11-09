"""日志收集「自定义场景」的读写服务。

第一性原理：acli 的 ``-c`` 只接受一个**节点上的 JSON 文件**，而平台唯一能写节点文件的
通道是 bridge。因此这里保存的是"结构化路径清单"，下发序列化只发生在 ``serialize_config``
一处——多写一处就必然漂移。

版本化：场景被任务引用后，编辑不得原地覆盖。所有更新都实现为"复制最新 revision + 1"，
任务侧通过 ``(scenario_id, revision)`` 永远能查回当时那份配置。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..schemas.log_collect_custom_scenario import (
    MAX_ITEMS,
    CustomScenarioItem,
    LogCollectCustomScenarioCreate,
    LogCollectCustomScenarioResponse,
    LogCollectCustomScenarioUpdate,
)

logger = logging.getLogger(__name__)

_COLUMNS = (
    "scenario_id, revision, code, label, summary, applies_to, items, item_count, "
    "config_sha256, config_bytes, source, enabled, sort_order, review_state, "
    "created_by, updated_by, created_at, updated_at"
)


class CustomScenarioNotFoundError(Exception):
    """场景（或指定 revision）不存在。"""


class CustomScenarioInUseError(Exception):
    """场景已被历史任务引用：只能停用，不能物理删除。"""


def serialize_config(items: list[dict[str, Any]]) -> tuple[str, str, int]:
    """把平台模型序列化成 acli 认的 config 文本。

    契约（已由 acli 源码确证）：
    - 只写 ``paths`` 一个字段：``modules`` 等字段因 ``-m`` 的 default 永不生效；
    - ``paths`` 是**逗号分隔字符串**，不是数组；
    - ``ensure_ascii=True``：acli 侧是 Python2 ``json.load``，转义成纯 ASCII
      可以绕开 BOM/编码校验缺失导致的历史坑。
    """
    paths = [str(item.get("path", "")).strip() for item in items]
    paths = [path for path in paths if path]
    body = json.dumps({"paths": ",".join(paths)}, ensure_ascii=True, separators=(",", ":"))
    encoded = body.encode("utf-8")
    return body, hashlib.sha256(encoded).hexdigest(), len(encoded)


def _row_to_response(row: Any) -> LogCollectCustomScenarioResponse:
    data = dict(row)
    raw_items = data.get("items") or []
    data["items"] = [CustomScenarioItem(**item) for item in raw_items]
    return LogCollectCustomScenarioResponse(**data)


class LogCollectCustomScenarioService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_latest(self, *, include_disabled: bool) -> list[LogCollectCustomScenarioResponse]:
        """每个 code 只返回最新 revision（历史版本按需单独查）。"""
        where = "" if include_disabled else "WHERE enabled"
        result = await self.session.execute(
            text(
                f"""
                SELECT DISTINCT ON (code) {_COLUMNS}
                FROM log_collect_custom_scenario
                {where}
                ORDER BY code, revision DESC
                """
            )
        )
        rows = [_row_to_response(row) for row in result.mappings()]
        # DISTINCT ON 按 code 排序，展示顺序需要按 sort_order 再排一次：
        # 否则运营调整的排序在列表里完全不生效。
        rows.sort(key=lambda item: (item.sort_order, item.code))
        return rows

    async def get_latest(self, code: str) -> LogCollectCustomScenarioResponse:
        result = await self.session.execute(
            text(
                f"""
                SELECT {_COLUMNS} FROM log_collect_custom_scenario
                WHERE code = :code ORDER BY revision DESC LIMIT 1
                """
            ),
            {"code": code},
        )
        row = result.mappings().first()
        if row is None:
            raise CustomScenarioNotFoundError(code)
        return _row_to_response(row)

    async def get_revision(self, scenario_id: str, revision: int) -> LogCollectCustomScenarioResponse:
        """按 (id, revision) 精确取：创建任务时用它保证"用的就是当时那一版"。"""
        result = await self.session.execute(
            text(
                f"""
                SELECT {_COLUMNS} FROM log_collect_custom_scenario
                WHERE scenario_id = :scenario_id AND revision = :revision
                """
            ),
            {"scenario_id": scenario_id, "revision": revision},
        )
        row = result.mappings().first()
        if row is None:
            raise CustomScenarioNotFoundError(f"{scenario_id}@{revision}")
        return _row_to_response(row)

    async def create(
        self, payload: LogCollectCustomScenarioCreate, *, actor: str | None
    ) -> LogCollectCustomScenarioResponse:
        items = [item.model_dump() for item in payload.items]
        config_text, sha256, size = serialize_config(items)
        result = await self.session.execute(
            text(
                f"""
                INSERT INTO log_collect_custom_scenario
                    (code, revision, label, summary, applies_to, items, item_count,
                     config_sha256, config_bytes, source, enabled, sort_order,
                     review_state, created_by, updated_by)
                VALUES
                    (:code, 1, :label, :summary, :applies_to, CAST(:items AS jsonb), :item_count,
                     :sha256, :size, :source, :enabled, :sort_order,
                     :review_state, :actor, :actor)
                RETURNING {_COLUMNS}
                """
            ),
            {
                "code": payload.code,
                "label": payload.label,
                "summary": payload.summary,
                "applies_to": payload.applies_to,
                "items": json.dumps(items, ensure_ascii=False),
                "item_count": len(items),
                "sha256": sha256,
                "size": size,
                "source": payload.source,
                "enabled": payload.enabled,
                "sort_order": payload.sort_order,
                "review_state": payload.review_state,
                "actor": actor,
            },
        )
        logger.info(
            "custom_scenario_created code=%s items=%d bytes=%d sha256=%s",
            payload.code,
            len(items),
            size,
            sha256,
        )
        return _row_to_response(result.mappings().one())

    async def update(
        self,
        code: str,
        payload: LogCollectCustomScenarioUpdate,
        *,
        actor: str | None,
    ) -> LogCollectCustomScenarioResponse:
        """生成新 revision。

        为什么连"只改文案"也要新版本：任务侧记录的是 revision + sha256，
        如果文案变更原地覆盖，历史任务详情页展示的说明就会与当时实际下发的不一致。
        """
        current = await self.get_latest(code)
        changes = payload.model_dump(exclude_unset=True)

        items = current.items
        if "items" in changes and changes["items"] is not None:
            items = payload.items or []
        item_dicts = [item.model_dump() for item in items]
        config_text, sha256, size = serialize_config(item_dicts)

        next_revision = current.revision + 1
        result = await self.session.execute(
            text(
                f"""
                INSERT INTO log_collect_custom_scenario
                    (scenario_id, revision, code, label, summary, applies_to, items,
                     item_count, config_sha256, config_bytes, source, enabled,
                     sort_order, review_state, created_by, updated_by)
                VALUES
                    (:scenario_id, :revision, :code, :label, :summary, :applies_to,
                     CAST(:items AS jsonb), :item_count, :sha256, :size, :source,
                     :enabled, :sort_order, :review_state, :created_by, :actor)
                RETURNING {_COLUMNS}
                """
            ),
            {
                "scenario_id": current.scenario_id,
                "revision": next_revision,
                "code": current.code,
                "label": changes.get("label", current.label),
                "summary": changes.get("summary", current.summary),
                "applies_to": changes.get("applies_to", current.applies_to),
                "items": json.dumps(item_dicts, ensure_ascii=False),
                "item_count": len(item_dicts),
                "sha256": sha256,
                "size": size,
                "source": current.source,
                "enabled": changes.get("enabled", current.enabled),
                "sort_order": changes.get("sort_order", current.sort_order),
                "review_state": changes.get("review_state", current.review_state),
                "created_by": current.created_by,
                "actor": actor,
            },
        )
        logger.info(
            "custom_scenario_revised code=%s revision=%d items=%d",
            code,
            next_revision,
            len(item_dicts),
        )
        return _row_to_response(result.mappings().one())

    async def delete(self, code: str) -> None:
        """物理删除只在"从未被任何任务引用"时允许。

        已被引用的场景删掉，历史任务详情会变成一个无法解释的空壳；
        停用（enabled=false）足以让它从客户面板消失。
        """
        usage = await self.session.execute(
            text(
                """
                SELECT count(*) FROM log_collect_task t
                JOIN log_collect_custom_scenario s
                  ON s.scenario_id = t.custom_scenario_id
                 AND s.revision = t.custom_scenario_revision
                WHERE s.code = :code
                """
            ),
            {"code": code},
        )
        referenced = int(usage.scalar_one() or 0)
        if referenced:
            raise CustomScenarioInUseError(code)
        await self.session.execute(
            text("DELETE FROM log_collect_custom_scenario WHERE code = :code"),
            {"code": code},
        )

    async def validate(self, payload: LogCollectCustomScenarioCreate) -> tuple[int, int]:
        """不落库的校验：返回 (item_count, config_bytes)。

        pydantic 已经挡住了非法路径；这里额外回答前端"会生成多大一份配置"，
        避免用户保存后才发现超过节点写入上限。
        """
        items = [item.model_dump() for item in payload.items]
        if len(items) > MAX_ITEMS:
            raise ValueError(f"路径条数不能超过 {MAX_ITEMS}")
        _, _, size = serialize_config(items)
        return len(items), size
