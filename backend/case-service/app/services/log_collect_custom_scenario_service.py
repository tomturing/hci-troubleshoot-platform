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
from datetime import date, timedelta
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


def serialize_spc(items: list[dict[str, Any]]) -> tuple[str, str, int]:
    """把平台模型序列化成 spc/<slot>.json 的内容。

    与 config 的差别：保留 action/filters/date_placeholders/docker/interpreter，
    不做任何简化；filters 里的 START/END 原样保留，由插件按 -d 注入时间范围。
    """
    payload: list[dict[str, Any]] = []
    current_note: str | None = None
    for item in items:
        note = str(item.get("note") or "").strip()
        if note and note != current_note:
            payload.append({"note": note})
            current_note = note
        entry: dict[str, Any] = {"action": item.get("action") or "copy", "path": item["path"]}
        for key in ("filters", "date_placeholders", "docker", "interpreter"):
            value = item.get(key)
            if value:
                entry[key] = value
        payload.append(entry)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = body.encode("utf-8")
    return body, hashlib.sha256(encoded).hexdigest(), len(encoded)


def _validate_items_for_source(items: list[dict[str, Any]], source: str) -> None:
    # -c 通道只做逐条 cp -r：非 copy、通配符、filters 都会被静默跳过，必须在这里拒绝。
    if source == "spc":
        return
    for index, item in enumerate(items):
        if item.get("action", "copy") != "copy":
            raise ValueError(f"第 {index + 1} 条：-c 通道只支持 copy 条目")
        if "*" in str(item.get("path", "")):
            raise ValueError(f"第 {index + 1} 条：-c 通道不支持通配符")
        if item.get("filters"):
            raise ValueError(f"第 {index + 1} 条：-c 通道不支持 filters")


def _serialize(items: list[dict[str, Any]], source: str) -> tuple[str, str, int]:
    """按通道选择序列化器：两条通道的产物完全不同，绝不能混用。"""
    _validate_items_for_source(items, source)
    if source == "spc":
        return serialize_spc(items)
    return serialize_config(items)


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

    async def get_latest_by_id(self, scenario_id: str) -> LogCollectCustomScenarioResponse:
        """按逻辑 ID 取最新版：客户前端只知道 scenario_id，不知道 code。

        为什么不以 code 为入口：code 是管理端稳定标识，让客户传它等于把
        平台内部命名暴露给客户端，且客户改不了的历史任务要按 revision 回溯。
        """
        result = await self.session.execute(
            text(
                f"""
                SELECT {_COLUMNS} FROM log_collect_custom_scenario
                WHERE scenario_id = :scenario_id ORDER BY revision DESC LIMIT 1
                """
            ),
            {"scenario_id": scenario_id},
        )
        row = result.mappings().first()
        if row is None:
            raise CustomScenarioNotFoundError(scenario_id)
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
        config_text, sha256, size = _serialize(items, payload.source)
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
        # 通道可以切换（spc ⇄ -c），序列化器必须跟着**新**通道走：
        # 沿用 current.source 会把 spc 条目按 paths 拼成 -c 文本，静默丢掉 filters。
        source = changes.get("source") or current.source
        config_text, sha256, size = _serialize(item_dicts, source)

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
                "source": source,
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

    async def validate(self, payload: LogCollectCustomScenarioCreate) -> tuple[int, int, str, str]:
        """不落库的校验：返回 (item_count, config_bytes, preview_text, preview_kind)。

        pydantic 已经挡住了非法路径；这里额外回答前端两件事：
        1. "会生成多大一份配置"——避免用户保存后才发现超过节点写入上限；
        2. "最终下发长什么样"——直接返回 ``_serialize`` 的真实产物，
           前端不再自行拼装协议，预览与实际下发不可能不一致。
        """
        items = [item.model_dump() for item in payload.items]
        if len(items) > MAX_ITEMS:
            raise ValueError(f"路径条数不能超过 {MAX_ITEMS}")
        text, _, size = _serialize(items, payload.source)
        kind = "spc" if payload.source == "spc" else "config"
        return len(items), size, text, kind


def expand_scenario_paths(
    items: list[dict[str, Any]],
    date_from: date,
    date_to: date,
    *,
    extra_paths: list[str] | None = None,
    max_count: int = 200,
) -> list[str]:
    """把「平台模型」展开成 acli 认的字面路径清单。

    三件事一次做完，顺序不可换：

    1. ``{date}`` 展开：spc 清单里大量 ``/sf/log/{date}.tar.gz`` 这种按天归档文件，
       而 config 的 paths 完全不做占位符替换（未展开的路径会被 ``os.path.exists``
       判假后**静默跳过**）。这里按用户选的时间范围逐天展开；
    2. 合并 ``extra_paths``（客户临时填的 ``-p``）：同时传 ``-c`` 和 ``-p`` 时
       CLI 会覆盖 config.paths，因此必须在平台侧合并，且只下发 ``-c``；
    3. 去重保序：重复项不会报错，但会让体积预估翻倍。

    ``{date}`` 的展开格式按 ``YYYY-MM-DD``（与 ``-d`` 参数同形）。
    该格式需以插件 spc 的 ``date_placeholders`` 为准；若现场实际是别的格式，
    预检会把它们列为缺失路径并由用户二次确认，不会静默收错。
    """
    days = (date_to - date_from).days + 1
    result: list[str] = []
    for item in items:
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        if "{date}" in path:
            for offset in range(days):
                day = date_from + timedelta(days=offset)
                result.append(path.replace("{date}", day.isoformat()))
        else:
            result.append(path)
    for raw in extra_paths or []:
        path = str(raw).strip()
        if path:
            result.append(path)

    # 去重保序：dict 保持插入顺序，比 set 更适合"下发顺序即用户所见顺序"的契约
    unique = list(dict.fromkeys(result))
    if len(unique) > max_count:
        raise ValueError(f"展开后的路径条数（{len(unique)}）超过上限 {max_count}")
    return unique
