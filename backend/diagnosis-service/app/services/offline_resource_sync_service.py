"""从已发布 KBD 增量生成 Collector 和 Collection Profile 候选版本。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import defaultdict
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from jsonschema import Draft202012Validator
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.observability.langfuse import observe_workflow, update_observation
from shared.observability.logger import get_logger
from shared.observability.metrics import (
    OFFLINE_RESOURCE_SYNC_CHANGES_TOTAL,
    OFFLINE_RESOURCE_SYNC_DURATION_SECONDS,
    OFFLINE_RESOURCE_SYNC_TOTAL,
)
from shared.observability.otel import get_current_trace_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.domain.collector_security import (
    render_collector_command,
    validate_collector_contract,
    validate_hci_api_contract,
    validate_manual_guide,
)
from app.errors import DiagnosisError
from app.schemas.collection_profile import CollectionProfileDefinition
from app.schemas.collector_definition import CollectorDefinitionWrite
from app.services.offline_acquisition_compiler import compile_signal_acquisition

logger = get_logger("diagnosis-service-offline-resource-sync")

_WRITE_OPERATION_WORDS = frozenset(
    {
        "add",
        "create",
        "delete",
        "destroy",
        "disable",
        "down",
        "enable",
        "format",
        "kill",
        "migrate",
        "modify",
        "reboot",
        "remove",
        "reset",
        "restart",
        "rm",
        "set",
        "shutdown",
        "start",
        "stop",
        "update",
        "up",
        "wipe",
    }
)

# 同步预检只需要验证模板中已经冻结的 KBD/Tool 参数；这四项由采集计划和制品生成
# 阶段按真实会话注入。使用无副作用占位值完成 argv 安全校验，绝不把测试值持久化到资源。
_PREVIEW_RUNTIME_PARAMETERS: dict[str, str] = {
    "target_id": "preview-target",
    "target_type": "node",
    "window_start": "1970-01-01T00:00:00Z",
    "window_end": "1970-01-01T00:00:00Z",
}


def normalize_acquirer(value: str) -> str:
    """兼容历史点号命名并统一到当前 snake_case。"""

    return value.strip().lower().replace(".", "_")


def resolve_scenario(kbd: dict[str, Any]) -> str | None:
    """使用 KBD 最终分类作为在线、离线诊断共用的问题场景标识。"""

    category_id = str(kbd.get("category_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-\u3400-\u9fff]{1,100}", category_id):
        return None
    return category_id


def resolve_target_scope(requirements: list[dict[str, Any]], tool: str, command_template: str) -> str:
    """由显式离线声明或最终命令真实依赖确定画像目标范围。"""

    declared = {item.get("target_scope") for item in requirements if item.get("target_scope")}
    if len(declared) == 1:
        return str(next(iter(declared)))
    if "{target_id}" in command_template:
        return "affected_object"
    return "source_node" if tool.startswith("qfk_") else "once"


def scenario_metadata(kbds: list[dict[str, Any]], scenario: str) -> dict[str, Any]:
    """从共享分类和 KBD 元数据合并画像展示名、产品版本范围。"""

    metadata_rows = [item.get("metadata") or {} for item in kbds]
    display_name = next(
        (str(item.get("category_name") or "").strip() for item in kbds if str(item.get("category_name") or "").strip()),
        scenario,
    )
    versions = sorted(
        {
            str(version).strip()
            for metadata in metadata_rows
            for version in (metadata.get("offline_supported_product_versions") or [])
            if str(version).strip()
        }
    )
    return {"display_name": display_name, "supported_product_versions": versions or ["6.*", "7.*", "8.*"]}


def extract_requirements(kbd: dict[str, Any]) -> list[dict[str, Any]]:
    """从 KBD 结构化信号提取采集需求，禁止解析自然语言步骤生成命令。"""

    document = kbd.get("signals_json") or []
    signals = document.get("signals", []) if isinstance(document, dict) else document
    requirements: list[dict[str, Any]] = []
    for signal in signals if isinstance(signals, list) else []:
        if not isinstance(signal, dict):
            continue
        acquire = signal.get("acquire") or {}
        tool = normalize_acquirer(str(acquire.get("tool") or signal.get("acquirer") or ""))
        if not tool:
            continue
        args = acquire.get("args") or signal.get("acquirer_args") or {}
        matcher = signal.get("match") or signal.get("matcher") or {}
        command = str(
            args.get("command")
            or args.get("sub_command")
            or signal.get("sub_command")
            or args.get("resource_keyword")
            or args.get("keyword")
            or "*"
        ).strip()
        signal_id = (
            str(signal.get("id") or "").strip()
            or hashlib.sha256(
                json.dumps(signal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:16]
        )
        base_requirement = {
            "signal_id": signal_id,
            "kbd_revision": int(kbd["resource_revision"]),
            "kbd_checksum": str(kbd["resource_checksum"]),
            "tool": tool,
            "command": command[:128],
            "args": args if isinstance(args, dict) else {},
            "matcher": matcher if isinstance(matcher, dict) else {},
            "produces": list((signal.get("orchestrate") or {}).get("produces") or []),
            # v2 信号复核标记在 provenance.needs_review / review.require_human_confirm；
            # 顶层字段仅为 v1 遗留兜底
            "needs_review": bool(
                (signal.get("provenance") or {}).get("needs_review")
                or (signal.get("review") or {}).get("require_human_confirm")
                or signal.get("needs_review")
                or signal.get("require_human_confirm")
            ),
            "kbd_id": int(kbd["id"]),
            "required_level": str(
                (acquire.get("offline") or {}).get("required_level")
                or signal.get("role")
                or ("mandatory" if signal.get("required_for_confirmation", True) else "recommended")
            ),
            "time_window": dict((acquire.get("offline") or {}).get("time_window") or {}),
            "target_scope": str((acquire.get("offline") or {}).get("target_scope") or ""),
            "required_permissions": list((acquire.get("offline") or {}).get("required_permissions") or []),
            "sensitive_data_types": list((acquire.get("offline") or {}).get("sensitive_data_types") or []),
            "support_id": str(kbd.get("support_id") or ""),
            "category_id": str(kbd.get("category_id") or "*"),
        }
        if tool == "qkv_dialog":
            paths = args.get("paths") if isinstance(args, dict) else None
            for path in paths or ["/sf/log/today", "/sf/log/today/vt"]:
                dialog_requirement = deepcopy(base_requirement)
                dialog_requirement["dialog_path"] = str(path)
                dialog_requirement["command"] = f"{command}@{path}"[:128]
                requirements.append(dialog_requirement)
        else:
            requirements.append(base_requirement)
    return requirements


def _schema_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    """读取 Tool 参数模式中的顶层默认值，供模板可选段判定。"""

    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return {}
    return {
        str(name): definition["default"]
        for name, definition in properties.items()
        if isinstance(definition, dict) and "default" in definition
    }


def _assert_read_only_requirement(requirement: dict[str, Any], tool: dict[str, Any]) -> None:
    """Tool 和 KBD 两端都必须满足只读边界。"""

    if int(tool.get("risk_level") or 1) != 1:
        raise ValueError(f"工具 {requirement['tool']} 不是只读工具，不能生成离线 Collector")
    args = requirement.get("args") or {}
    operation = str(args.get("command") or args.get("sub_command") or "").lower()
    operation_words = set(re.findall(r"[a-z]+", operation))
    blocked = sorted(operation_words & _WRITE_OPERATION_WORDS)
    if blocked:
        raise ValueError(f"KBD 信号包含写操作 {', '.join(blocked)}，不能生成只读 Collector")


def _tool_parameter_definition(tool_schema: dict[str, Any], field: str) -> dict[str, Any] | None:
    """按 Collector 扁平字段找到 Tool 模式中的原始约束。"""

    aliases = {
        "file": ("target", "resource"),
        "path": ("target", "path"),
        "time_window": ("target", "time_window"),
    }
    path = aliases.get(field, (field,))
    current: dict[str, Any] = tool_schema
    for segment in path:
        properties = current.get("properties") if isinstance(current, dict) else None
        if not isinstance(properties, dict) or not isinstance(properties.get(segment), dict):
            return None
        current = properties[segment]
    return deepcopy(current)


def _parameter_schema(parameters: dict[str, Any], tool_schema: dict[str, Any]) -> dict[str, Any]:
    """优先继承 Tool 参数约束，并生成默认拒绝的 Collector 模式。"""

    properties: dict[str, dict[str, Any]] = {}
    for field, value in parameters.items():
        inherited = _tool_parameter_definition(tool_schema, field)
        if inherited is not None:
            inherited.pop("default", None)
            inherited.pop("description", None)
            properties[field] = inherited
        elif isinstance(value, bool):
            properties[field] = {"type": "boolean"}
        elif isinstance(value, int):
            properties[field] = {"type": "integer"}
        elif isinstance(value, float):
            properties[field] = {"type": "number"}
        else:
            properties[field] = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "properties": properties,
        "required": list(parameters),
        "additionalProperties": False,
    }


def build_tool_collector_candidate(
    requirement: dict[str, Any], tool: dict[str, Any], *, version: str
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """由 KBD 信号和已发布 Tool 修订唯一确定 Collector 候选。"""

    _assert_read_only_requirement(requirement, tool)
    _validate_tool_bindings(
        dict(requirement.get("args") or {}),
        dict(tool.get("parameters_schema") or {}),
    )
    bindings = _runtime_bindings(requirement, tool)
    if requirement.get("dialog_path"):
        bindings["path"] = requirement["dialog_path"]
    compiled = compile_signal_acquisition(
        tool=requirement["tool"],
        args=bindings,
        matcher=dict(requirement.get("matcher") or {}),
        produces=list(requirement.get("produces") or []),
    )
    command_template = compiled.command_template
    parameters = dict(compiled.parameters)
    collector_identity_payload = {
        "tool": requirement["tool"],
        "command_template": command_template,
        "parameters": parameters,
    }
    execution_contract_payload = {
        "tool": requirement["tool"],
        "tool_revision": tool["revision"],
        "tool_checksum": tool["checksum"],
        "command_template": command_template,
        "parameters": parameters,
        "query_type": compiled.query_type,
        "timeout_seconds": compiled.timeout_seconds,
        "supported_product_versions": compiled.supported_product_versions,
        "resolution_catalog_version": compiled.catalog_version,
        "resolution_snapshot": compiled.resolution_snapshot,
    }
    execution_contract_checksum = hashlib.sha256(
        json.dumps(execution_contract_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(collector_identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    collector_id = f"kbd_{requirement['tool']}_{fingerprint}"
    is_json = compiled.query_type == "json"
    is_log = compiled.query_type == "log"
    extension = "json" if is_json else "log" if is_log else "txt"
    output_directory = "logs" if is_log else "commands"
    query_type = compiled.query_type
    parameter_schema = _parameter_schema(parameters, dict(tool.get("parameters_schema") or {}))
    # 同步预检就验证 KBD 冻结值，禁止把 enum/pattern 不匹配问题延迟到客户制品阶段。
    render_collector_command(
        command_template,
        parameter_schema,
        {**_PREVIEW_RUNTIME_PARAMETERS, **parameters},
    )
    candidate = {
        "collector_id": collector_id,
        "display_name": f"{tool['display_name']}（KBD 同步）",
        "description": f"由已发布 Tool {requirement['tool']} 修订 {tool['revision']} 与 KBD 结构化信号生成",
        "platform": "linux",
        "executor": "shell",
        "command_template": command_template,
        "parameter_schema": parameter_schema,
        "risk_level": "read_only",
        "timeout_seconds": compiled.timeout_seconds,
        "max_output_mb": 4,
        "supported_product_versions": compiled.supported_product_versions,
        "output_contract": {
            "schema_id": f"hci.offline.{collector_id}.v1",
            "media_type": "application/json" if is_json else "text/plain",
            "output_path": f"{output_directory}/{collector_id}.{extension}",
        },
        "version": version,
        "managed_by": "kbd_sync",
        "generation_metadata": {
            "tool_name": requirement["tool"],
            "tool_revision": tool["revision"],
            "tool_version": tool["version"],
            "tool_checksum": tool["checksum"],
            "resolution_catalog_version": compiled.catalog_version,
            "resolution_status": compiled.resolution_status.value,
            "resolution_snapshot": compiled.resolution_snapshot,
            "supported_product_versions": compiled.supported_product_versions,
            "execution_contract_checksum": execution_contract_checksum,
        },
    }
    return candidate, parameters, query_type


def _runtime_bindings(requirement: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    """合并 Tool 默认值和 KBD 参数；执行语义由 Shared Resolver 决定。"""

    args = dict(requirement.get("args") or {})
    bindings = {**_schema_defaults(tool.get("parameters_schema") or {}), **args}
    target = args.get("target") if isinstance(args.get("target"), dict) else {}
    for source, destination in (
        ("resource", "file"),
        ("path", "path"),
        ("time_window", "time_window"),
    ):
        if bindings.get(destination) in (None, "") and target.get(source) not in (None, ""):
            bindings[destination] = target[source]
    return bindings


def _validate_tool_bindings(bindings: dict[str, Any], schema: dict[str, Any]) -> None:
    """在同步预览阶段执行 Tool 参数契约，禁止把坏参数推迟到客户现场。"""

    if not schema:
        return
    errors = sorted(Draft202012Validator(schema).iter_errors(bindings), key=lambda error: list(error.path))
    if errors:
        raise DiagnosisError(
            code="COLLECTOR_PARAMETER_VALIDATION_FAILED",
            message="KBD 采集参数不符合已发布 Tool 契约",
            http_status=422,
            details={"errors": [error.message for error in errors]},
        )


class OfflineResourceSyncService:
    """维护增量同步候选、原子发布、批次回滚和追加式审计。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def preview(self, *, actor: ActorContext, mode: str) -> dict[str, Any]:
        """扫描 KBD 不可变修订并保存候选差异，不修改当前生效资源。"""

        self._require_admin(actor)
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        await self._ensure_state(trace_id)
        state = (
            (
                await self._session.execute(
                    text("SELECT * FROM offline_resource_sync_state WHERE state_key = 'kbd' FOR UPDATE")
                )
            )
            .mappings()
            .one()
        )
        await self._supersede_candidates(actor.user_id, trace_id)
        base_cursor = 0 if mode == "full" else int(state["last_kbd_revision_id"])
        base_tool_cursor = 0 if mode == "full" else int(state["last_tool_revision_id"])
        scan_started_at = time.monotonic()
        with observe_workflow(
            name="offline_resource_sync.scan_revisions",
            input={"mode": mode, "base_cursor": base_cursor, "base_tool_cursor": base_tool_cursor},
            metadata={"mode": mode, "trace_id": trace_id},
            session_id=trace_id,
            user_id=actor.user_id,
            trace_id=trace_id,
        ) as scan_observation:
            revision_rows = await self._load_changed_kbd_revisions(base_cursor=base_cursor, full=mode == "full")
            tool_revision_rows = await self._load_changed_tool_revisions(
                base_cursor=base_tool_cursor,
                full=mode == "full",
            )
            update_observation(
                scan_observation,
                output={"kbd_revision_count": len(revision_rows), "tool_revision_count": len(tool_revision_rows)},
                metadata={"mode": mode},
            )
        OFFLINE_RESOURCE_SYNC_DURATION_SECONDS.labels(mode=mode, phase="scan").observe(
            time.monotonic() - scan_started_at
        )
        target_cursor = max([base_cursor, *[int(row["id"]) for row in revision_rows]])
        target_tool_cursor = max([base_tool_cursor, *[int(row["id"]) for row in tool_revision_rows]])
        batch_id = uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO offline_resource_sync_batch (
                    batch_id, base_cursor, target_cursor, base_tool_cursor, target_tool_cursor,
                    sync_mode, status,
                    requested_by, trace_id
                ) VALUES (
                    :batch_id, :base_cursor, :target_cursor, :base_tool_cursor, :target_tool_cursor,
                    :sync_mode, 'candidate',
                    :requested_by, :trace_id
                )
                """
            ),
            {
                "batch_id": batch_id,
                "base_cursor": int(state["last_kbd_revision_id"]),
                "target_cursor": target_cursor,
                "base_tool_cursor": int(state["last_tool_revision_id"]),
                "target_tool_cursor": target_tool_cursor,
                "sync_mode": mode,
                "requested_by": actor.user_id,
                "trace_id": trace_id,
            },
        )
        await self._event(batch_id, "preview", "started", actor.user_id, {"mode": mode}, trace_id)
        preview_started_at = time.monotonic()
        try:
            with observe_workflow(
                name="offline_resource_sync.preview",
                input={
                    "mode": mode,
                    "kbd_revision_count": len(revision_rows),
                    "tool_revision_count": len(tool_revision_rows),
                },
                metadata={"batch_id": str(batch_id), "mode": mode, "trace_id": trace_id},
                session_id=str(batch_id),
                user_id=actor.user_id,
                trace_id=trace_id,
            ) as preview_observation:
                async with self._session.begin_nested():
                    summary, validations, changes = await self._build_changes(
                        revision_rows=revision_rows,
                        tool_revision_rows=tool_revision_rows,
                        target_cursor=target_cursor,
                        target_tool_cursor=target_tool_cursor,
                        full=mode == "full",
                    )
                    for change in changes:
                        await self._insert_change(batch_id=batch_id, trace_id=trace_id, **change)
                    counts = {
                        "collector": sum(item["resource_type"] == "collector" for item in changes),
                        "collection_profile": sum(item["resource_type"] == "collection_profile" for item in changes),
                        "signal_mapping": sum(item["resource_type"] == "signal_mapping" for item in changes),
                    }
                    await self._session.execute(
                        text(
                            """
                            UPDATE offline_resource_sync_batch SET
                                kbd_change_count = :kbd_count,
                                tool_change_count = :tool_count,
                                collector_change_count = :collector_count,
                                profile_change_count = :profile_count,
                                mapping_change_count = :mapping_count,
                                summary_json = CAST(:summary AS jsonb),
                                validation_json = CAST(:validations AS jsonb),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE batch_id = :batch_id
                            """
                        ),
                        {
                            "batch_id": batch_id,
                            "kbd_count": len(revision_rows),
                            "tool_count": len(tool_revision_rows),
                            "collector_count": counts["collector"],
                            "profile_count": counts["collection_profile"],
                            "mapping_count": counts["signal_mapping"],
                            "summary": self._json(summary),
                            "validations": self._json(validations),
                        },
                    )
                change_types: dict[tuple[str, str], int] = defaultdict(int)
                for change in changes:
                    change_types[(str(change["resource_type"]), str(change["change_type"]))] += 1
                for (resource_type, change_type), count in change_types.items():
                    OFFLINE_RESOURCE_SYNC_CHANGES_TOTAL.labels(
                        mode=mode, resource_type=resource_type, change_type=change_type
                    ).inc(count)
                update_observation(
                    preview_observation,
                    output={
                        "change_count": len(changes),
                        "validation_count": len(validations),
                        "resource_counts": counts,
                        # Langfuse 用于复盘“为什么产生这项同步差异”；统一观测层会先
                        # 对 Token、密钥等字段脱敏，再根据内容采集开关决定是否上传正文。
                        "changes": changes,
                        "validations": validations,
                        "summary": summary,
                    },
                    metadata={"batch_id": str(batch_id), "mode": mode, "status": "succeeded"},
                )
            await self._event(
                batch_id,
                "preview",
                "succeeded",
                actor.user_id,
                {"change_count": len(changes), "validation_count": len(validations)},
                trace_id,
            )
            OFFLINE_RESOURCE_SYNC_TOTAL.labels(mode=mode, status="succeeded").inc()
        except Exception as exc:
            OFFLINE_RESOURCE_SYNC_TOTAL.labels(mode=mode, status="failed").inc()
            logger.exception(
                event="offline_resource_sync_preview_failed",
                batch_id=str(batch_id),
                mode=mode,
                trace_id=trace_id,
                error=str(exc),
            )
            await self._mark_failed(batch_id, "preview", actor.user_id, trace_id, exc)
        finally:
            OFFLINE_RESOURCE_SYNC_DURATION_SECONDS.labels(mode=mode, phase="preview").observe(
                time.monotonic() - preview_started_at
            )
        return await self.get(actor=actor, batch_id=str(batch_id))

    async def publish(self, *, actor: ActorContext, batch_id: str, reason: str) -> dict[str, Any]:
        """校验候选后原子发布 Collector、Mapping 和 Profile，并推进 KBD 游标。"""

        self._require_admin(actor)
        batch = await self._get_batch_locked(batch_id)
        if batch["status"] != "candidate":
            raise DiagnosisError(code="SYNC_BATCH_NOT_CANDIDATE", message="只有候选批次可以发布", http_status=409)
        if (batch.get("summary_json") or {}).get("scenario_source") != "kbd.category_id":
            raise DiagnosisError(
                code="SYNC_BATCH_POLICY_OUTDATED",
                message="该候选批次使用旧离线准入规则，请重新执行全量检测",
                http_status=409,
            )
        blocking = [item for item in batch["validation_json"] if item.get("severity") == "error"]
        if blocking:
            raise DiagnosisError(
                code="SYNC_BATCH_VALIDATION_FAILED",
                message="同步候选存在阻断项，不能发布",
                http_status=422,
                details={"validations": blocking},
            )
        trace_id = get_current_trace_id() or batch["trace_id"]
        await self._event(UUID(batch_id), "publish", "started", actor.user_id, {"reason": reason}, trace_id)
        try:
            async with self._session.begin_nested():
                state = (
                    (
                        await self._session.execute(
                            text("SELECT * FROM offline_resource_sync_state WHERE state_key = 'kbd' FOR UPDATE")
                        )
                    )
                    .mappings()
                    .one()
                )
                if int(state["last_kbd_revision_id"]) != int(batch["base_cursor"]):
                    raise DiagnosisError(
                        code="SYNC_CURSOR_CONFLICT",
                        message="已有其他同步批次发布，请重新生成预览",
                        http_status=409,
                    )
                if int(state["last_tool_revision_id"]) != int(batch["base_tool_cursor"]):
                    raise DiagnosisError(
                        code="SYNC_TOOL_CURSOR_CONFLICT",
                        message="Tool Registry 已产生新修订，请重新生成同步预览",
                        http_status=409,
                    )
                changes = await self._load_changes(batch_id)
                order = {"collector": 0, "signal_mapping": 1, "collection_profile": 2}
                for change in sorted(changes, key=lambda item: order[item["resource_type"]]):
                    await self._publish_change(change, actor.user_id, trace_id)
                await self._session.execute(
                    text(
                        """
                        UPDATE offline_resource_sync_state SET
                            last_kbd_revision_id = :cursor,
                            last_tool_revision_id = :tool_cursor,
                            last_batch_id = :batch_id,
                            lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP,
                            trace_id = :trace_id
                        WHERE state_key = 'kbd'
                        """
                    ),
                    {
                        "cursor": batch["target_cursor"],
                        "tool_cursor": batch["target_tool_cursor"],
                        "batch_id": batch_id,
                        "trace_id": trace_id,
                    },
                )
                await self._session.execute(
                    text(
                        """
                        UPDATE offline_resource_sync_batch SET
                            status = 'published', approved_by = :actor_id, approval_reason = :reason,
                            published_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                        WHERE batch_id = :batch_id
                        """
                    ),
                    {"batch_id": batch_id, "actor_id": actor.user_id, "reason": reason},
                )
            await self._event(UUID(batch_id), "publish", "succeeded", actor.user_id, {}, trace_id)
        except Exception as exc:
            await self._mark_failed(UUID(batch_id), "publish", actor.user_id, trace_id, exc)
        return await self.get(actor=actor, batch_id=batch_id)

    async def reject(self, *, actor: ActorContext, batch_id: str, reason: str) -> dict[str, Any]:
        """拒绝候选批次，保留全部差异和操作历史。"""

        self._require_admin(actor)
        batch = await self._get_batch_locked(batch_id)
        if batch["status"] != "candidate":
            raise DiagnosisError(code="SYNC_BATCH_NOT_CANDIDATE", message="只有候选批次可以拒绝", http_status=409)
        trace_id = get_current_trace_id() or batch["trace_id"]
        await self._event(UUID(batch_id), "reject", "started", actor.user_id, {"reason": reason}, trace_id)
        await self._session.execute(
            text(
                """
                UPDATE offline_resource_sync_batch SET status = 'rejected', approval_reason = :reason,
                    approved_by = :actor_id, updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = :batch_id
                """
            ),
            {"batch_id": batch_id, "reason": reason, "actor_id": actor.user_id},
        )
        await self._event(UUID(batch_id), "reject", "succeeded", actor.user_id, {}, trace_id)
        return await self.get(actor=actor, batch_id=batch_id)

    async def rollback(self, *, actor: ActorContext, batch_id: str, reason: str) -> dict[str, Any]:
        """仅允许回滚最新发布批次，原子恢复全部 active 指针和事实源。"""

        self._require_admin(actor)
        batch = await self._get_batch_locked(batch_id)
        if batch["status"] not in {"published", "rollback_failed"}:
            raise DiagnosisError(code="SYNC_BATCH_NOT_PUBLISHED", message="只有已发布批次可以回滚", http_status=409)
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        await self._event(UUID(batch_id), "rollback", "started", actor.user_id, {"reason": reason}, trace_id)
        try:
            async with self._session.begin_nested():
                state = (
                    (
                        await self._session.execute(
                            text("SELECT * FROM offline_resource_sync_state WHERE state_key = 'kbd' FOR UPDATE")
                        )
                    )
                    .mappings()
                    .one()
                )
                if str(state["last_batch_id"] or "") != batch_id:
                    raise DiagnosisError(
                        code="SYNC_ROLLBACK_ORDER_CONFLICT",
                        message="只能回滚最后一次已发布同步，避免资源依赖错位",
                        http_status=409,
                    )
                changes = await self._load_changes(batch_id)
                order = {"collection_profile": 0, "signal_mapping": 1, "collector": 2}
                for change in sorted(changes, key=lambda item: order[item["resource_type"]]):
                    await self._rollback_change(change, trace_id)
                previous_batch = (
                    await self._session.execute(
                        text(
                            """
                            SELECT batch_id FROM offline_resource_sync_batch
                            WHERE status = 'published' AND target_cursor = :base_cursor
                              AND target_tool_cursor = :base_tool_cursor
                              AND batch_id <> :batch_id
                            ORDER BY published_at DESC NULLS LAST LIMIT 1
                            """
                        ),
                        {
                            "base_cursor": batch["base_cursor"],
                            "base_tool_cursor": batch["base_tool_cursor"],
                            "batch_id": batch_id,
                        },
                    )
                ).scalar_one_or_none()
                await self._session.execute(
                    text(
                        """
                        UPDATE offline_resource_sync_state SET
                            last_kbd_revision_id = :cursor,
                            last_tool_revision_id = :tool_cursor,
                            last_batch_id = :last_batch_id,
                            lock_version = lock_version + 1, updated_at = CURRENT_TIMESTAMP,
                            trace_id = :trace_id
                        WHERE state_key = 'kbd'
                        """
                    ),
                    {
                        "cursor": batch["base_cursor"],
                        "tool_cursor": batch["base_tool_cursor"],
                        "last_batch_id": previous_batch,
                        "trace_id": trace_id,
                    },
                )
                await self._session.execute(
                    text(
                        """
                        UPDATE offline_resource_sync_batch SET
                            status = 'rolled_back', rollback_by = :actor_id,
                            rollback_reason = :reason, rolled_back_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE batch_id = :batch_id
                        """
                    ),
                    {"batch_id": batch_id, "actor_id": actor.user_id, "reason": reason},
                )
                await self._session.execute(
                    text(
                        """
                        UPDATE offline_resource_sync_batch SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                        WHERE status = 'candidate' AND batch_id <> :batch_id
                        """
                    ),
                    {"batch_id": batch_id},
                )
            await self._event(UUID(batch_id), "rollback", "succeeded", actor.user_id, {}, trace_id)
        except Exception as exc:
            await self._session.execute(
                text(
                    """
                    UPDATE offline_resource_sync_batch SET status = 'rollback_failed',
                        error_json = CAST(:error AS jsonb), updated_at = CURRENT_TIMESTAMP
                    WHERE batch_id = :batch_id
                    """
                ),
                {"batch_id": batch_id, "error": self._json(self._error_summary(exc))},
            )
            await self._event(UUID(batch_id), "rollback", "failed", actor.user_id, self._error_summary(exc), trace_id)
        return await self.get(actor=actor, batch_id=batch_id)

    async def list_history(self, *, actor: ActorContext, offset: int, limit: int) -> dict[str, Any]:
        """分页读取全部同步批次，失败和被拒绝批次同样返回。"""

        self._require_admin(actor)
        total = (await self._session.execute(text("SELECT COUNT(*) FROM offline_resource_sync_batch"))).scalar_one()
        rows = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT * FROM offline_resource_sync_batch
                    ORDER BY created_at DESC, batch_id DESC OFFSET :offset LIMIT :limit
                    """
                    ),
                    {"offset": offset, "limit": limit},
                )
            )
            .mappings()
            .all()
        )
        return {"items": [self._row(row) for row in rows], "total": total, "offset": offset, "limit": limit}

    async def get(self, *, actor: ActorContext, batch_id: str) -> dict[str, Any]:
        """读取同步批次、资源差异和每次动作结果。"""

        self._require_admin(actor)
        batch = (
            (
                await self._session.execute(
                    text("SELECT * FROM offline_resource_sync_batch WHERE batch_id = :batch_id"),
                    {"batch_id": batch_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if batch is None:
            raise DiagnosisError(code="SYNC_BATCH_NOT_FOUND", message="同步批次不存在", http_status=404)
        changes = await self._load_changes(batch_id)
        events = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT * FROM offline_resource_sync_event WHERE batch_id = :batch_id
                    ORDER BY event_sequence
                    """
                    ),
                    {"batch_id": batch_id},
                )
            )
            .mappings()
            .all()
        )
        result = self._row(batch)
        result["changes"] = [self._row(row) for row in changes]
        result["events"] = [self._row(row) for row in events]
        return result

    async def _build_changes(
        self,
        *,
        revision_rows: list[dict[str, Any]],
        tool_revision_rows: list[dict[str, Any]],
        target_cursor: int,
        target_tool_cursor: int,
        full: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """构造候选差异；只使用结构化 KBD 信号和受审安全能力目录。"""

        changed_ids = sorted(
            {int(row["resource_name"]) for row in revision_rows if str(row["resource_name"]).isdigit()}
        )
        current_kbds = await self._load_published_kbds()
        current_by_id = {int(item["id"]): item for item in current_kbds}
        eligible_kbds = [item for item in current_kbds if resolve_scenario(item) is not None]
        unresolved_kbd_ids = sorted(int(item["id"]) for item in current_kbds if resolve_scenario(item) is None)
        impacted_scenarios: set[str] = (
            {
                *{scenario for item in eligible_kbds if (scenario := resolve_scenario(item))},
                *(await self._active_profile_scenarios()),
            }
            if full
            else await self._active_profile_scenarios_for_kbds(changed_ids)
        )
        changed_tools = {str(row["resource_name"]) for row in tool_revision_rows}
        validations: list[dict[str, Any]] = []
        for row in revision_rows:
            content = row["content_json"] or {}
            source_snapshot = {
                "title": content.get("title"),
                "category_id": content.get("category_id"),
                "metadata": (row["contract_json"] or {}).get("metadata") or {},
            }
            scenario = resolve_scenario(source_snapshot)
            if scenario:
                impacted_scenarios.add(scenario)
        for kbd_id in changed_ids:
            if kbd_id in current_by_id and (scenario := resolve_scenario(current_by_id[kbd_id])):
                impacted_scenarios.add(scenario)
        if changed_tools:
            for kbd in eligible_kbds:
                scenario = resolve_scenario(kbd)
                if scenario and any(item["tool"] in changed_tools for item in extract_requirements(kbd)):
                    impacted_scenarios.add(scenario)

        scenario_kbds: dict[str, list[dict[str, Any]]] = defaultdict(list)
        requirements: list[dict[str, Any]] = []
        for kbd in current_kbds:
            scenario = resolve_scenario(kbd)
            if scenario is None:
                validations.append(
                    {
                        "severity": "error",
                        "code": "KBD_CATEGORY_UNRESOLVED",
                        "message": "已发布 KBD 缺少有效最终分类，无法加入在线、离线共用问题场景",
                        "kbd_id": int(kbd["id"]),
                        "support_id": kbd.get("support_id"),
                    }
                )
                continue
            if scenario in impacted_scenarios:
                scenario_kbds[scenario].append(kbd)
                requirements.extend(extract_requirements(kbd))

        version = f"1.0.{target_cursor}.{target_tool_cursor}"
        changes: list[dict[str, Any]] = []
        tools = await self._load_active_signal_tools()
        collector_entries: dict[str, dict[str, Any]] = {}
        resolved_requirements: list[dict[str, Any]] = []
        unsupported_sources: dict[str, set[int]] = defaultdict(set)
        for requirement in requirements:
            tool = requirement["tool"]
            tool_snapshot = tools.get(tool)
            if tool_snapshot is None:
                unsupported_sources[tool].add(int(requirement["kbd_id"]))
                continue
            if not tool_snapshot["available"]:
                validations.append(
                    {
                        "severity": "warning",
                        "code": "TOOL_DISABLED_DOWNSTREAM_RECALCULATED",
                        "message": f"工具 {tool} 已停用，本批次将停止新计划引用其派生采集资源",
                        "kbd_id": int(requirement["kbd_id"]),
                        "support_id": requirement["support_id"],
                        "signal_id": requirement["signal_id"],
                    }
                )
                continue
            candidate: dict[str, Any] = {}
            try:
                candidate, parameters, query_type = build_tool_collector_candidate(
                    requirement,
                    tool_snapshot,
                    version=version,
                )
                command = CollectorDefinitionWrite.model_validate(candidate)
                if command.executor == "shell":
                    validate_collector_contract(command.command_template, command.parameter_schema)
                elif command.executor == "http":
                    validate_hci_api_contract(command.command_template, command.parameter_schema)
                else:
                    validate_manual_guide(command.command_template, command.parameter_schema)
            except ValueError as exc:
                pending_review = bool(requirement.get("needs_review"))
                validations.append(
                    {
                        "severity": "warning" if pending_review else "error",
                        "code": ("KBD_SIGNAL_PENDING_REVIEW_SKIPPED" if pending_review else "KBD_ACQUISITION_INVALID"),
                        "message": str(exc),
                        "resource_name": candidate.get("collector_id") or tool,
                        "kbd_id": int(requirement["kbd_id"]),
                        "support_id": requirement["support_id"],
                        "signal_id": requirement["signal_id"],
                    }
                )
                continue
            except DiagnosisError as exc:
                validation_details = exc.details or {}
                detail_messages = [str(item) for item in validation_details.get("errors") or [] if str(item)]
                validations.append(
                    {
                        "severity": "error",
                        "code": exc.code,
                        "message": "；".join([exc.message, *detail_messages]),
                        "resource_name": candidate.get("collector_id") or tool,
                        "kbd_id": int(requirement["kbd_id"]),
                        "support_id": requirement["support_id"],
                        "signal_id": requirement["signal_id"],
                        "details": validation_details,
                    }
                )
                continue
            except Exception as exc:
                validations.append(
                    {
                        "severity": "error",
                        "code": "COLLECTOR_SECURITY_VALIDATION_FAILED",
                        "message": str(exc),
                        "resource_name": candidate.get("collector_id") or tool,
                        "kbd_id": int(requirement["kbd_id"]),
                        "support_id": requirement["support_id"],
                        "signal_id": requirement["signal_id"],
                    }
                )
                continue
            collector_id = candidate["collector_id"]
            entry = collector_entries.setdefault(
                collector_id,
                {
                    "candidate": candidate,
                    "parameters": parameters,
                    "tool": tool_snapshot,
                    "source_ids": set(),
                    "source_signals": set(),
                },
            )
            entry["source_ids"].add(int(requirement["kbd_id"]))
            entry["source_signals"].add(f"{requirement['kbd_id']}:{requirement['signal_id']}")
            resolved_requirements.append(
                {
                    **requirement,
                    "collector_id": collector_id,
                    "collector_parameters": parameters,
                    "query_type": query_type,
                    "execution_contract_checksum": candidate["generation_metadata"]["execution_contract_checksum"],
                }
            )

        for entry in collector_entries.values():
            entry["candidate"]["generation_metadata"].update(
                {
                    "source_kbd_ids": sorted(entry["source_ids"]),
                    "source_signals": sorted(entry["source_signals"]),
                }
            )

        for tool, source_ids in sorted(unsupported_sources.items()):
            validations.append(
                {
                    "severity": "error",
                    "code": "TOOL_REVISION_UNAVAILABLE",
                    "message": f"KBD 引用的工具 {tool} 未启用或未发布",
                    "source_kbd_ids": sorted(source_ids),
                }
            )

        for _collector_id, entry in sorted(collector_entries.items()):
            candidate = entry["candidate"]
            source_ids = entry["source_ids"]
            before_revision, before = await self._active_resource("collector", candidate["collector_id"])
            if self._equivalent_resource(before, candidate):
                continue
            changes.append(
                {
                    "resource_type": "collector",
                    "resource_name": candidate["collector_id"],
                    "change_type": "create" if before is None else "update",
                    "source_kbd_ids": sorted(source_ids),
                    "before_revision": before_revision,
                    "before_json": before,
                    "candidate_json": candidate,
                    "validation_json": [{"severity": "info", "code": "SECURITY_VALIDATED"}],
                }
            )

        if full or changed_ids or changed_tools:
            required_collector_ids = set(collector_entries)
            collectors_referenced_by_unaffected_profiles = (
                set() if full else await self._collector_ids_for_profiles_excluding(impacted_scenarios)
            )
            stale_rows = (
                (
                    await self._session.execute(
                        text(
                            """
                        SELECT d.collector_id, d.generation_metadata
                        FROM collector_definition d
                        JOIN dynamic_resource_active a
                          ON a.resource_type = 'collector' AND a.resource_name = d.collector_id
                        WHERE d.is_enabled = true
                          AND d.managed_by = 'kbd_sync'
                        ORDER BY d.collector_id
                        """
                        )
                    )
                )
                .mappings()
                .all()
            )
            for stale_row in stale_rows:
                stale_collector_id = str(stale_row["collector_id"])
                if stale_collector_id in required_collector_ids:
                    continue
                if stale_collector_id in collectors_referenced_by_unaffected_profiles:
                    continue
                metadata = dict(stale_row["generation_metadata"] or {})
                if not full:
                    source_ids = {int(item) for item in metadata.get("source_kbd_ids") or []}
                    if not (source_ids.intersection(changed_ids) or metadata.get("tool_name") in changed_tools):
                        continue
                before_revision, before = await self._active_resource("collector", stale_collector_id)
                if before is None:
                    continue
                changes.append(
                    {
                        "resource_type": "collector",
                        "resource_name": stale_collector_id,
                        "change_type": "disable",
                        "source_kbd_ids": sorted(int(item) for item in metadata.get("source_kbd_ids") or []),
                        "before_revision": before_revision,
                        "before_json": before,
                        "candidate_json": {"is_enabled": False},
                        "validation_json": [
                            {
                                "severity": "info",
                                "code": "STALE_SYNC_COLLECTOR_DISABLED",
                                "message": "全量同步后已无 KBD/画像引用该 Collector",
                            }
                        ],
                    }
                )

        mapping_candidates: dict[tuple[int, int, str, str, str], dict[str, Any]] = {}
        for requirement in resolved_requirements:
            key = (
                int(requirement["kbd_id"]),
                int(requirement["kbd_revision"]),
                requirement["signal_id"],
                requirement["execution_contract_checksum"],
                requirement["collector_id"],
            )
            mapping_candidates[key] = requirement

        required_mapping_keys = set(mapping_candidates)
        for key, requirement in sorted(mapping_candidates.items()):
            mapping_identity = "|".join(str(item) for item in key)
            mapping_id = str(uuid5(NAMESPACE_URL, "hci-offline-mapping:" + mapping_identity))
            candidate = {
                "source_kbd_id": key[0],
                "source_kbd_revision": key[1],
                "source_signal_id": key[2],
                "execution_contract_checksum": key[3],
                "mapping_id": mapping_id,
                "acquire_tool": requirement["tool"],
                "category_scope": requirement["category_id"],
                "command_scope": requirement["command"],
                "collector_id": key[4],
                "query_type": requirement["query_type"],
                "field_mapping": {},
                "priority": 50,
                "is_enabled": True,
            }
            before = await self._mapping(mapping_id)
            if self._equivalent_mapping(before, candidate):
                continue
            changes.append(
                {
                    "resource_type": "signal_mapping",
                    "resource_name": mapping_id,
                    "change_type": "create" if before is None else "update",
                    "source_kbd_ids": [key[0]],
                    "before_revision": None,
                    "before_json": before,
                    "candidate_json": candidate,
                    "validation_json": [],
                }
            )

        if full or changed_ids or changed_tools:
            existing_sync_mappings = (
                (
                    await self._session.execute(
                        text(
                            """
                        SELECT m.* FROM offline_signal_collector_mapping m
                        WHERE m.trace_id IN (SELECT trace_id FROM offline_resource_sync_batch)
                          AND m.is_enabled = true
                          AND (
                              CAST(:full AS boolean)
                              OR m.source_kbd_id = ANY(CAST(:changed_ids AS bigint[]))
                              OR m.acquire_tool = ANY(CAST(:changed_tools AS varchar[]))
                          )
                        """
                        ),
                        {
                            "full": full,
                            "changed_ids": changed_ids,
                            "changed_tools": sorted(changed_tools),
                        },
                    )
                )
                .mappings()
                .all()
            )
            for row in existing_sync_mappings:
                key = (
                    row["source_kbd_id"],
                    row["source_kbd_revision"],
                    row["source_signal_id"],
                    row["execution_contract_checksum"],
                    row["collector_id"],
                )
                if key in required_mapping_keys:
                    continue
                before = dict(row)
                candidate = {
                    key_name: before[key_name]
                    for key_name in (
                        "mapping_id",
                        "source_kbd_id",
                        "source_kbd_revision",
                        "source_signal_id",
                        "execution_contract_checksum",
                        "acquire_tool",
                        "category_scope",
                        "command_scope",
                        "collector_id",
                        "query_type",
                        "field_mapping",
                        "priority",
                        "is_enabled",
                    )
                }
                candidate["mapping_id"] = str(candidate["mapping_id"])
                candidate["is_enabled"] = False
                changes.append(
                    {
                        "resource_type": "signal_mapping",
                        "resource_name": str(row["mapping_id"]),
                        "change_type": "disable",
                        "source_kbd_ids": [],
                        "before_revision": None,
                        "before_json": self._row(before),
                        "candidate_json": candidate,
                        "validation_json": [],
                    }
                )

        changed_collector_ids = {
            item["resource_name"]
            for item in changes
            if item["resource_type"] == "collector" and item["change_type"] in {"create", "update"}
        }
        for scenario in sorted(impacted_scenarios):
            kbds = scenario_kbds.get(scenario, [])
            collector_sources: dict[str, set[int]] = defaultdict(set)
            scenario_kbd_ids = {int(kbd["id"]) for kbd in kbds}
            for requirement in resolved_requirements:
                if int(requirement["kbd_id"]) in scenario_kbd_ids:
                    collector_sources[requirement["collector_id"]].add(int(requirement["kbd_id"]))
            before_revision, before = await self._active_resource("collection_profile", scenario)
            if not collector_sources:
                if before is not None:
                    before_governance = await self._resource_governance("collection_profile", scenario)
                    changes.append(
                        {
                            "resource_type": "collection_profile",
                            "resource_name": scenario,
                            "change_type": "disable",
                            "source_kbd_ids": sorted(
                                int(item)
                                for item in (before_governance.get("generation_metadata") or {}).get("source_kbd_ids")
                                or []
                            ),
                            "before_revision": before_revision,
                            "before_json": before,
                            "candidate_json": {"version": version, "profile": before, "is_enabled": False},
                            "validation_json": [
                                {
                                    "severity": "warning",
                                    "code": "PROFILE_NO_PUBLISHED_KBD",
                                    "message": "该场景已无已发布 KBD，发布后停止新采集计划引用",
                                }
                            ],
                        }
                    )
                continue
            items = []
            for collector_id, source_ids in sorted(collector_sources.items()):
                entry = collector_entries[collector_id]
                collector = entry["candidate"]
                tool = next(
                    requirement["tool"]
                    for requirement in resolved_requirements
                    if requirement["collector_id"] == collector_id
                )
                collector_requirements = [
                    requirement
                    for requirement in resolved_requirements
                    if requirement["collector_id"] == collector_id and int(requirement["kbd_id"]) in scenario_kbd_ids
                ]
                requirement_levels = {item.get("required_level") for item in collector_requirements}
                required_level = (
                    "mandatory" if requirement_levels.intersection({"mandatory", "must"}) else "recommended"
                )
                before_minutes = max(
                    [int((item.get("time_window") or {}).get("before_minutes", 60)) for item in collector_requirements]
                    or [60]
                )
                after_minutes = max(
                    [int((item.get("time_window") or {}).get("after_minutes", 30)) for item in collector_requirements]
                    or [30]
                )
                target_scope = resolve_target_scope(
                    collector_requirements,
                    tool,
                    collector["command_template"],
                )
                declared_permissions = sorted(
                    {value for item in collector_requirements for value in item.get("required_permissions") or []}
                )
                declared_sensitivity = sorted(
                    {value for item in collector_requirements for value in item.get("sensitive_data_types") or []}
                )
                items.append(
                    {
                        "collector_id": collector_id,
                        "display_name": collector["display_name"],
                        "required_level": required_level,
                        "target_scope": target_scope,
                        "time_window": {"before_minutes": before_minutes, "after_minutes": after_minutes},
                        "parameters": dict(entry["parameters"]),
                        "reason": f"由 {len(source_ids)} 条已发布 KBD 的结构化信号要求生成",
                        "expected_size_mb": collector["max_output_mb"],
                        "timeout_seconds": collector["timeout_seconds"],
                        "required_permissions": declared_permissions
                        or (["hci_readonly"] if collector["platform"] == "hci_api" else ["system_read"]),
                        "sensitive_data_types": declared_sensitivity
                        or (["system_logs"] if tool.startswith("qfk_") else ["hci_metadata"]),
                    }
                )
            scenario_config = scenario_metadata(kbds, scenario)
            profile = {
                "profile_id": scenario,
                "display_name": f"{scenario_config['display_name']}采集画像",
                "product_line": "HCI",
                "scenario": scenario,
                "supported_product_versions": scenario_config["supported_product_versions"],
                "items": items,
            }
            source_tools = sorted(
                {
                    (
                        entry["tool"]["tool_name"],
                        int(entry["tool"]["revision"]),
                        str(entry["tool"]["version"]),
                        str(entry["tool"]["checksum"]),
                    )
                    for collector_id in collector_sources
                    for entry in [collector_entries[collector_id]]
                }
            )
            profile_generation_metadata = {
                "source_kbd_ids": sorted({int(kbd["id"]) for kbd in kbds}),
                "source_tools": [
                    {
                        "tool_name": name,
                        "revision": revision,
                        "version": tool_version,
                        "checksum": checksum,
                    }
                    for name, revision, tool_version, checksum in source_tools
                ],
            }
            try:
                CollectionProfileDefinition.model_validate(profile)
            except ValueError as exc:
                validations.append(
                    {
                        "severity": "error",
                        "code": "COLLECTION_PROFILE_INVALID",
                        "message": str(exc),
                        "resource_name": scenario,
                        "source_kbd_ids": sorted({int(kbd["id"]) for kbd in kbds}),
                        "details": {
                            "items": [
                                {
                                    "collector_id": item["collector_id"],
                                    "required_level": item["required_level"],
                                }
                                for item in items
                            ]
                        },
                    }
                )
                continue
            current_governance = await self._resource_governance("collection_profile", scenario)
            dependency_changed = bool(set(collector_sources).intersection(changed_collector_ids))
            provenance_changed = (
                dict(current_governance.get("generation_metadata") or {}) != profile_generation_metadata
            )
            if not self._equivalent_resource(before, profile) or dependency_changed or provenance_changed:
                changes.append(
                    {
                        "resource_type": "collection_profile",
                        "resource_name": scenario,
                        "change_type": "create" if before is None else "update",
                        "source_kbd_ids": sorted({int(kbd["id"]) for kbd in kbds}),
                        "before_revision": before_revision,
                        "before_json": before,
                        "candidate_json": {
                            "version": version,
                            "profile": profile,
                            "generation_metadata": profile_generation_metadata,
                        },
                        "validation_json": [{"severity": "info", "code": "COLLECTOR_REFERENCES_VALIDATED"}],
                    }
                )

        if not revision_rows and not tool_revision_rows:
            validations.append(
                {
                    "severity": "info",
                    "code": "NO_SOURCE_CHANGES",
                    "message": "上次同步后没有新的 KBD 或 Tool Registry 修订",
                }
            )
        summary = {
            "changed_kbd_ids": changed_ids,
            "changed_tools": sorted(changed_tools),
            "impacted_scenarios": sorted(impacted_scenarios),
            "published_kbd_count": len(current_kbds),
            "shared_scenario_kbd_count": len(eligible_kbds),
            "unresolved_category_kbd_count": len(unresolved_kbd_ids),
            "unresolved_category_kbd_ids": unresolved_kbd_ids,
            "scenario_source": "kbd.category_id",
            # 兼容旧管理端和历史批次读取；含义已收敛为“有最终分类的已发布 KBD”。
            "offline_eligible_kbd_count": len(eligible_kbds),
            "skipped_non_offline_kbd_count": 0,
            "candidate_change_count": len(changes),
            "unsupported_tools": sorted(
                {item["message"] for item in validations if item.get("code") == "TOOL_REVISION_UNAVAILABLE"}
            ),
            "policy": "候选版本不影响当前 active revision；批准后按批次原子切换",
        }
        return summary, validations, changes

    async def _publish_change(self, change: dict[str, Any], actor_id: str, trace_id: str) -> None:
        """发布单项候选；调用方负责批次级事务和依赖顺序。"""

        candidate = change["candidate_json"] or {}
        after_revision = None
        after_json = candidate
        if change["resource_type"] == "collector":
            if change["change_type"] == "disable":
                await self._session.execute(
                    text(
                        """
                        DELETE FROM dynamic_resource_active
                        WHERE resource_type = 'collector' AND resource_name = :collector_id
                        """
                    ),
                    {"collector_id": change["resource_name"]},
                )
                await self._session.execute(
                    text(
                        """
                        UPDATE collector_definition SET is_enabled = false,
                            lock_version = lock_version + 1, trace_id = :trace_id,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE collector_id = :collector_id
                        """
                    ),
                    {"collector_id": change["resource_name"], "trace_id": trace_id},
                )
                after_json = {"is_enabled": False}
            else:
                command = CollectorDefinitionWrite.model_validate(candidate)
                await self._upsert_collector(command, actor_id, trace_id)
                snapshot = await DynamicResourcePublisher(self._session).ensure_published(
                    resource_type="collector",
                    resource_name=command.collector_id,
                    version=command.version,
                    content=command.model_dump(mode="json"),
                    contract={
                        "parameter_schema": command.parameter_schema,
                        "output_contract": command.output_contract.model_dump(mode="json"),
                        "risk_level": command.risk_level,
                    },
                    dependencies=[
                        *list(change.get("source_tool_revisions") or []),
                        *list(change.get("source_kbd_revisions") or []),
                    ],
                    trace_id=trace_id,
                )
                after_revision = snapshot.revision
                after_json = snapshot.content
        elif change["resource_type"] == "collection_profile":
            if change["change_type"] == "disable":
                await self._session.execute(
                    text(
                        """
                        DELETE FROM dynamic_resource_active
                        WHERE resource_type = 'collection_profile' AND resource_name = :profile_id
                        """
                    ),
                    {"profile_id": change["resource_name"]},
                )
                await self._session.execute(
                    text(
                        """
                        UPDATE collection_profile_definition SET is_enabled = false,
                            lock_version = lock_version + 1, trace_id = :trace_id,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE profile_id = :profile_id
                        """
                    ),
                    {"profile_id": change["resource_name"], "trace_id": trace_id},
                )
                after_json = {"is_enabled": False}
            else:
                profile = CollectionProfileDefinition.model_validate(candidate["profile"])
                await self._upsert_profile(
                    profile,
                    candidate["version"],
                    actor_id,
                    trace_id,
                    generation_metadata=dict(candidate.get("generation_metadata") or {}),
                )
                snapshot = await DynamicResourcePublisher(self._session).ensure_published(
                    resource_type="collection_profile",
                    resource_name=profile.profile_id,
                    version=candidate["version"],
                    content=profile.model_dump(mode="json"),
                    contract={
                        "product_line": profile.product_line,
                        "supported_product_versions": profile.supported_product_versions,
                    },
                    dependencies=[
                        *(
                            await self._active_revision_references(
                                "collector",
                                sorted({item.collector_id for item in profile.items}),
                            )
                        ),
                        *list(change.get("source_tool_revisions") or []),
                        *list(change.get("source_kbd_revisions") or []),
                    ],
                    trace_id=trace_id,
                )
                after_revision = snapshot.revision
                after_json = snapshot.content
        else:
            await self._upsert_mapping(candidate, trace_id)
        await self._session.execute(
            text(
                """
                UPDATE offline_resource_sync_change SET status = 'published',
                    after_revision = :after_revision, after_json = CAST(:after_json AS jsonb),
                    updated_at = CURRENT_TIMESTAMP
                WHERE change_id = :change_id
                """
            ),
            {
                "change_id": change["change_id"],
                "after_revision": after_revision,
                "after_json": self._json(after_json),
            },
        )

    async def _rollback_change(self, change: dict[str, Any], trace_id: str) -> None:
        """按 change.before 快照恢复资源；历史 revision 永不删除。"""

        resource_type = change["resource_type"]
        before = change["before_json"]
        before_governance = dict(change.get("before_governance_json") or {})
        if resource_type == "signal_mapping":
            if before is None:
                await self._session.execute(
                    text("DELETE FROM offline_signal_collector_mapping WHERE mapping_id = :mapping_id"),
                    {"mapping_id": change["resource_name"]},
                )
            else:
                await self._upsert_mapping(before, trace_id)
        else:
            if before is None or change["before_revision"] is None:
                await self._session.execute(
                    text(
                        "DELETE FROM dynamic_resource_active WHERE resource_type = :resource_type AND resource_name = :name"
                    ),
                    {"resource_type": resource_type, "name": change["resource_name"]},
                )
                table = "collector_definition" if resource_type == "collector" else "collection_profile_definition"
                key = "collector_id" if resource_type == "collector" else "profile_id"
                await self._session.execute(
                    text(
                        f"UPDATE {table} SET is_enabled = false, review_status = 'draft', trace_id = :trace_id WHERE {key} = :name"
                    ),  # noqa: S608
                    {"trace_id": trace_id, "name": change["resource_name"]},
                )
            else:
                revision = (
                    (
                        await self._session.execute(
                            text(
                                """
                            SELECT checksum, version FROM dynamic_resource_revision
                            WHERE resource_type = :resource_type AND resource_name = :name AND revision = :revision
                            """
                            ),
                            {
                                "resource_type": resource_type,
                                "name": change["resource_name"],
                                "revision": change["before_revision"],
                            },
                        )
                    )
                    .mappings()
                    .one()
                )
                await self._session.execute(
                    text(
                        """
                        INSERT INTO dynamic_resource_active (
                            resource_type, resource_name, active_revision, checksum, trace_id
                        ) VALUES (:resource_type, :name, :revision, :checksum, :trace_id)
                        ON CONFLICT (resource_type, resource_name) DO UPDATE SET
                            active_revision = EXCLUDED.active_revision, checksum = EXCLUDED.checksum,
                            trace_id = EXCLUDED.trace_id, updated_at = CURRENT_TIMESTAMP
                        """
                    ),
                    {
                        "resource_type": resource_type,
                        "name": change["resource_name"],
                        "revision": change["before_revision"],
                        "checksum": revision["checksum"],
                        "trace_id": trace_id,
                    },
                )
                if resource_type == "collector":
                    command = CollectorDefinitionWrite.model_validate(before)
                    command = command.model_copy(
                        update={
                            "managed_by": before_governance.get("managed_by", command.managed_by),
                            "generation_metadata": dict(
                                before_governance.get("generation_metadata") or command.generation_metadata
                            ),
                        }
                    )
                    await self._upsert_collector(command, "sync-rollback", trace_id)
                else:
                    profile = CollectionProfileDefinition.model_validate(before)
                    await self._upsert_profile(
                        profile,
                        revision["version"],
                        "sync-rollback",
                        trace_id,
                        managed_by=str(before_governance.get("managed_by") or "manual"),
                        generation_metadata=dict(before_governance.get("generation_metadata") or {}),
                    )
        await self._session.execute(
            text(
                """
                UPDATE offline_resource_sync_change SET status = 'rolled_back', updated_at = CURRENT_TIMESTAMP
                WHERE change_id = :change_id
                """
            ),
            {"change_id": change["change_id"]},
        )

    async def _upsert_collector(self, command: CollectorDefinitionWrite, actor_id: str, trace_id: str) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO collector_definition (
                    collector_id, display_name, description, platform, executor, command_template,
                    parameter_schema, risk_level, timeout_seconds, max_output_mb,
                    supported_product_versions, output_contract, managed_by, generation_metadata,
                    semantic_version,
                    review_status, is_enabled, approved_by, approved_at, trace_id
                ) VALUES (
                    :collector_id, :display_name, :description, :platform, :executor, :command_template,
                    CAST(:parameter_schema AS jsonb), :risk_level, :timeout_seconds, :max_output_mb,
                    CAST(:versions AS jsonb), CAST(:output_contract AS jsonb),
                    :managed_by, CAST(:generation_metadata AS jsonb), :version,
                    'approved', true, :actor_id, CURRENT_TIMESTAMP, :trace_id
                )
                ON CONFLICT (collector_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name, description = EXCLUDED.description,
                    platform = EXCLUDED.platform, executor = EXCLUDED.executor,
                    command_template = EXCLUDED.command_template, parameter_schema = EXCLUDED.parameter_schema,
                    risk_level = EXCLUDED.risk_level, timeout_seconds = EXCLUDED.timeout_seconds,
                    max_output_mb = EXCLUDED.max_output_mb,
                    supported_product_versions = EXCLUDED.supported_product_versions,
                    output_contract = EXCLUDED.output_contract, managed_by = EXCLUDED.managed_by,
                    generation_metadata = EXCLUDED.generation_metadata,
                    semantic_version = EXCLUDED.semantic_version,
                    review_status = 'approved', is_enabled = true, approved_by = EXCLUDED.approved_by,
                    approved_at = CURRENT_TIMESTAMP, rejection_reason = NULL,
                    lock_version = collector_definition.lock_version + 1,
                    trace_id = EXCLUDED.trace_id, updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                **command.model_dump(
                    mode="json", exclude={"parameter_schema", "supported_product_versions", "output_contract"}
                ),
                "parameter_schema": self._json(command.parameter_schema),
                "versions": self._json(command.supported_product_versions),
                "output_contract": self._json(command.output_contract.model_dump(mode="json")),
                "managed_by": command.managed_by,
                "generation_metadata": self._json(command.generation_metadata),
                "actor_id": actor_id,
                "trace_id": trace_id,
            },
        )

    async def _upsert_profile(
        self,
        profile: CollectionProfileDefinition,
        version: str,
        actor_id: str,
        trace_id: str,
        managed_by: str = "kbd_sync",
        generation_metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO collection_profile_definition (
                    profile_id, profile_json, managed_by, generation_metadata,
                    semantic_version, review_status, is_enabled,
                    approved_by, approved_at, trace_id
                ) VALUES (
                    :profile_id, CAST(:profile_json AS jsonb), :managed_by, CAST(:generation_metadata AS jsonb),
                    :version, 'approved', true,
                    :actor_id, CURRENT_TIMESTAMP, :trace_id
                )
                ON CONFLICT (profile_id) DO UPDATE SET
                    profile_json = EXCLUDED.profile_json, semantic_version = EXCLUDED.semantic_version,
                    managed_by = EXCLUDED.managed_by, generation_metadata = EXCLUDED.generation_metadata,
                    review_status = 'approved', is_enabled = true, approved_by = EXCLUDED.approved_by,
                    approved_at = CURRENT_TIMESTAMP, rejection_reason = NULL,
                    lock_version = collection_profile_definition.lock_version + 1,
                    trace_id = EXCLUDED.trace_id, updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "profile_id": profile.profile_id,
                "profile_json": self._json(profile.model_dump(mode="json")),
                "managed_by": managed_by,
                "generation_metadata": self._json(generation_metadata or {}),
                "version": version,
                "actor_id": actor_id,
                "trace_id": trace_id,
            },
        )

    async def _upsert_mapping(self, candidate: dict[str, Any], trace_id: str) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO offline_signal_collector_mapping (
                    mapping_id, source_kbd_id, source_kbd_revision, source_signal_id,
                    execution_contract_checksum, acquire_tool, category_scope, command_scope, collector_id,
                    query_type, field_mapping, priority, is_enabled, trace_id
                ) VALUES (
                    :mapping_id, :source_kbd_id, :source_kbd_revision, :source_signal_id,
                    :execution_contract_checksum, :acquire_tool, :category_scope, :command_scope, :collector_id,
                    :query_type, CAST(:field_mapping AS jsonb), :priority, :is_enabled, :trace_id
                )
                ON CONFLICT (mapping_id) DO UPDATE SET
                    source_kbd_id = EXCLUDED.source_kbd_id,
                    source_kbd_revision = EXCLUDED.source_kbd_revision,
                    source_signal_id = EXCLUDED.source_signal_id,
                    execution_contract_checksum = EXCLUDED.execution_contract_checksum,
                    acquire_tool = EXCLUDED.acquire_tool, category_scope = EXCLUDED.category_scope,
                    command_scope = EXCLUDED.command_scope, collector_id = EXCLUDED.collector_id,
                    query_type = EXCLUDED.query_type, field_mapping = EXCLUDED.field_mapping,
                    priority = EXCLUDED.priority, is_enabled = EXCLUDED.is_enabled,
                    lock_version = offline_signal_collector_mapping.lock_version + 1,
                    trace_id = EXCLUDED.trace_id, updated_at = CURRENT_TIMESTAMP
                """
            ),
            {**candidate, "field_mapping": self._json(candidate.get("field_mapping") or {}), "trace_id": trace_id},
        )

    async def _load_changed_kbd_revisions(self, *, base_cursor: int, full: bool) -> list[dict[str, Any]]:
        clause = "" if full else "AND id > :base_cursor"
        rows = (
            (
                await self._session.execute(
                    text(
                        f"""
                    SELECT id, resource_name, revision, status, content_json, contract_json, created_at
                    FROM dynamic_resource_revision
                    WHERE resource_type = 'kbd' {clause}
                    ORDER BY id
                    """  # noqa: S608
                    ),
                    {"base_cursor": base_cursor},
                )
            )
            .mappings()
            .all()
        )
        if not full:
            return [dict(row) for row in rows]
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[row["resource_name"]] = dict(row)
        return list(latest.values())

    async def _load_changed_tool_revisions(self, *, base_cursor: int, full: bool) -> list[dict[str, Any]]:
        """按不可变修订序号读取 QKV/QFK Tool 变化。"""

        clause = "" if full else "AND id > :base_cursor"
        rows = (
            (
                await self._session.execute(
                    text(
                        f"""
                    SELECT id, resource_name, revision, version, status, content_json,
                           contract_json, checksum, created_at
                    FROM dynamic_resource_revision
                    WHERE resource_type = 'tool'
                      AND content_json->>'category' IN ('qkv', 'qfk')
                      {clause}
                    ORDER BY id
                    """  # noqa: S608
                    ),
                    {"base_cursor": base_cursor},
                )
            )
            .mappings()
            .all()
        )
        if not full:
            return [dict(row) for row in rows]
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[str(row["resource_name"])] = dict(row)
        return list(latest.values())

    async def _load_published_kbds(self) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT e.id, e.support_id,
                           COALESCE(r.content_json->>'title', e.title) AS title,
                           COALESCE(r.content_json->>'category_id', e.category_id::text) AS category_id,
                           c.name AS category_name,
                           c.domain AS category_domain,
                           COALESCE(r.content_json->'signals_json', e.signals_json) AS signals_json,
                           COALESCE(r.contract_json->'metadata', e.metadata) AS metadata,
                           r.revision AS resource_revision,
                           r.version AS resource_version,
                           r.checksum AS resource_checksum,
                           r.published_at AS updated_at
                    FROM kbd_entry e
                    JOIN dynamic_resource_active a
                      ON a.resource_type = 'kbd' AND a.resource_name = e.id::text
                    JOIN dynamic_resource_revision r
                      ON r.resource_type = a.resource_type
                     AND r.resource_name = a.resource_name
                     AND r.revision = a.active_revision
                    LEFT JOIN kb_category c
                      ON c.code = COALESCE(r.content_json->>'category_id', e.category_id::text)
                    WHERE e.status = 'published' AND r.status = 'published'
                    ORDER BY e.id
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def _active_profile_scenarios_for_kbds(self, kbd_ids: list[int]) -> set[str]:
        """查找变化 KBD 之前支撑的画像，确保退出离线范围后仍能增量清理。"""

        if not kbd_ids:
            return set()
        rows = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT profile_id, generation_metadata
                    FROM collection_profile_definition
                    WHERE managed_by = 'kbd_sync' AND is_enabled = true
                    ORDER BY profile_id
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
        changed = set(kbd_ids)
        return {
            str(row["profile_id"])
            for row in rows
            if changed.intersection(
                int(item) for item in (row["generation_metadata"] or {}).get("source_kbd_ids") or []
            )
        }

    async def _active_profile_scenarios(self) -> set[str]:
        """读取全部 KBD 同步管理的生效画像，供全量检测清理已失去来源的场景。"""

        rows = (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT profile_id
                        FROM collection_profile_definition
                        WHERE managed_by = 'kbd_sync' AND is_enabled = true
                        ORDER BY profile_id
                        """
                    )
                )
            )
            .scalars()
            .all()
        )
        return {str(profile_id) for profile_id in rows}

    async def _collector_ids_for_profiles_excluding(self, excluded_scenarios: set[str]) -> set[str]:
        """读取未受本批次影响画像仍引用的 Collector，避免增量清理误伤共享资源。"""

        rows = (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT DISTINCT item->>'collector_id' AS collector_id
                        FROM collection_profile_definition p
                        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(p.profile_json->'items', '[]'::jsonb)) item
                        WHERE p.managed_by = 'kbd_sync'
                          AND p.is_enabled = true
                          AND NOT (p.profile_id = ANY(CAST(:excluded_scenarios AS varchar[])))
                          AND COALESCE(item->>'collector_id', '') <> ''
                        ORDER BY collector_id
                        """
                    ),
                    {"excluded_scenarios": sorted(excluded_scenarios)},
                )
            )
            .scalars()
            .all()
        )
        return {str(collector_id) for collector_id in rows}

    async def _load_active_signal_tools(self) -> dict[str, dict[str, Any]]:
        """读取 QKV/QFK Tool 的当前修订；可变事实表只用于启停门禁。"""

        rows = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT t.tool_name, r.revision, r.version, r.checksum,
                           r.content_json, r.contract_json
                    FROM tool_definition t
                    JOIN dynamic_resource_active a
                      ON a.resource_type = 'tool' AND a.resource_name = t.tool_name
                    JOIN dynamic_resource_revision r
                      ON r.resource_type = a.resource_type
                     AND r.resource_name = a.resource_name
                     AND r.revision = a.active_revision
                    WHERE t.category IN ('qkv', 'qfk')
                    ORDER BY t.tool_name
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            content = dict(row["content_json"] or {})
            contract = dict(row["contract_json"] or {})
            result[str(row["tool_name"])] = {
                **content,
                "parameters_schema": dict(contract.get("parameters_schema") or {}),
                "risk_level": int(contract.get("risk_level") or 1),
                "revision": int(row["revision"]),
                "version": str(row["version"]),
                "checksum": str(row["checksum"]),
                "available": bool(content.get("is_active")),
            }
        return result

    async def _active_resource(self, resource_type: str, name: str) -> tuple[int | None, dict[str, Any] | None]:
        row = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT r.revision, r.content_json FROM dynamic_resource_active a
                    JOIN dynamic_resource_revision r
                      ON r.resource_type = a.resource_type AND r.resource_name = a.resource_name
                     AND r.revision = a.active_revision
                    WHERE a.resource_type = :resource_type AND a.resource_name = :name
                    """
                    ),
                    {"resource_type": resource_type, "name": name},
                )
            )
            .mappings()
            .one_or_none()
        )
        return (int(row["revision"]), dict(row["content_json"])) if row else (None, None)

    async def _mapping(self, mapping_id: str) -> dict[str, Any] | None:
        row = (
            (
                await self._session.execute(
                    text("SELECT * FROM offline_signal_collector_mapping WHERE mapping_id = :mapping_id"),
                    {"mapping_id": mapping_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._row(row) if row else None

    async def _ensure_state(self, trace_id: str) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO offline_resource_sync_state (state_key, last_kbd_revision_id, trace_id)
                VALUES ('kbd', 0, :trace_id) ON CONFLICT (state_key) DO NOTHING
                """
            ),
            {"trace_id": trace_id},
        )

    async def _supersede_candidates(self, actor_id: str, trace_id: str) -> None:
        rows = (
            (
                await self._session.execute(
                    text("SELECT batch_id FROM offline_resource_sync_batch WHERE status = 'candidate' FOR UPDATE")
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        await self._session.execute(
            text(
                "UPDATE offline_resource_sync_batch SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE status = 'candidate'"
            )
        )
        for batch_id in rows:
            await self._event(
                batch_id,
                "reject",
                "succeeded",
                actor_id,
                {"reason": "新的同步预览已生成，旧候选自动失效"},
                trace_id,
            )

    async def _insert_change(
        self,
        *,
        batch_id: UUID,
        resource_type: str,
        resource_name: str,
        change_type: str,
        source_kbd_ids: list[int],
        before_revision: int | None,
        before_json: dict[str, Any] | None,
        candidate_json: dict[str, Any],
        validation_json: list[dict[str, Any]],
        trace_id: str,
    ) -> None:
        before_governance = await self._resource_governance(resource_type, resource_name)
        candidate_governance: dict[str, Any] = {}
        if resource_type in {"collector", "collection_profile"}:
            candidate_governance = {
                "managed_by": "kbd_sync",
                "generation_metadata": dict(candidate_json.get("generation_metadata") or {}),
            }
        source_kbd_revisions = await self._active_revision_references(
            "kbd",
            [str(item) for item in source_kbd_ids],
        )
        tool_names: set[str] = set()
        metadata = candidate_json.get("generation_metadata") if isinstance(candidate_json, dict) else None
        if not isinstance(metadata, dict) or not metadata:
            metadata = before_governance.get("generation_metadata")
        if isinstance(metadata, dict) and metadata.get("tool_name"):
            tool_names.add(str(metadata["tool_name"]))
        if isinstance(metadata, dict):
            for item in metadata.get("source_tools") or []:
                if isinstance(item, dict) and item.get("tool_name"):
                    tool_names.add(str(item["tool_name"]))
        if resource_type == "signal_mapping" and candidate_json.get("acquire_tool"):
            tool_names.add(str(candidate_json["acquire_tool"]))
        if resource_type == "collection_profile":
            for item in (candidate_json.get("profile") or {}).get("items", []):
                collector_metadata = await self._collector_generation_metadata(str(item.get("collector_id") or ""))
                if collector_metadata.get("tool_name"):
                    tool_names.add(str(collector_metadata["tool_name"]))
        source_tool_revisions = await self._active_revision_references("tool", sorted(tool_names))
        await self._session.execute(
            text(
                """
                INSERT INTO offline_resource_sync_change (
                    batch_id, resource_type, resource_name, change_type, source_kbd_ids,
                    source_kbd_revisions, source_tool_revisions,
                    before_revision, before_governance_json, candidate_governance_json,
                    before_json, candidate_json, validation_json, trace_id
                ) VALUES (
                    :batch_id, :resource_type, :resource_name, :change_type, CAST(:source_kbd_ids AS jsonb),
                    CAST(:source_kbd_revisions AS jsonb), CAST(:source_tool_revisions AS jsonb),
                    :before_revision, CAST(:before_governance_json AS jsonb),
                    CAST(:candidate_governance_json AS jsonb), CAST(:before_json AS jsonb), CAST(:candidate_json AS jsonb),
                    CAST(:validation_json AS jsonb), :trace_id
                )
                """
            ),
            {
                "batch_id": batch_id,
                "resource_type": resource_type,
                "resource_name": resource_name,
                "change_type": change_type,
                "source_kbd_ids": self._json(source_kbd_ids),
                "source_kbd_revisions": self._json(source_kbd_revisions),
                "source_tool_revisions": self._json(source_tool_revisions),
                "before_revision": before_revision,
                "before_governance_json": self._json(before_governance),
                "candidate_governance_json": self._json(candidate_governance),
                "before_json": self._json(before_json) if before_json is not None else None,
                "candidate_json": self._json(candidate_json),
                "validation_json": self._json(validation_json),
                "trace_id": trace_id,
            },
        )

    async def _active_revision_references(self, resource_type: str, names: list[str]) -> list[dict[str, Any]]:
        """生成批次审计使用的不可变资源引用。"""

        if not names:
            return []
        rows = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT r.resource_name, r.revision, r.version, r.checksum
                    FROM dynamic_resource_active a
                    JOIN dynamic_resource_revision r
                      ON r.resource_type = a.resource_type
                     AND r.resource_name = a.resource_name
                     AND r.revision = a.active_revision
                    WHERE a.resource_type = :resource_type
                      AND a.resource_name = ANY(:names)
                    ORDER BY r.resource_name
                    """
                    ),
                    {"resource_type": resource_type, "names": names},
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "resource_type": resource_type,
                "resource_name": str(row["resource_name"]),
                "revision": int(row["revision"]),
                "version": str(row["version"]),
                "checksum": str(row["checksum"]),
            }
            for row in rows
        ]

    async def _collector_generation_metadata(self, collector_id: str) -> dict[str, Any]:
        """读取本次候选外已存在 Collector 的来源元数据。"""

        if not collector_id:
            return {}
        row = (
            await self._session.execute(
                text("SELECT generation_metadata FROM collector_definition WHERE collector_id = :collector_id"),
                {"collector_id": collector_id},
            )
        ).scalar_one_or_none()
        return dict(row or {})

    async def _resource_governance(self, resource_type: str, resource_name: str) -> dict[str, Any]:
        """读取可变事实表的治理属性，作为批次回滚快照。"""

        if resource_type not in {"collector", "collection_profile"}:
            return {}
        table = "collector_definition" if resource_type == "collector" else "collection_profile_definition"
        key = "collector_id" if resource_type == "collector" else "profile_id"
        row = (
            (
                await self._session.execute(
                    text(
                        f"SELECT managed_by, generation_metadata FROM {table} WHERE {key} = :resource_name"  # noqa: S608
                    ),
                    {"resource_name": resource_name},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return {}
        return {
            "managed_by": str(row["managed_by"]),
            "generation_metadata": dict(row["generation_metadata"] or {}),
        }

    async def _event(
        self,
        batch_id: UUID,
        action: str,
        result: str,
        actor_id: str,
        details: dict[str, Any],
        trace_id: str,
    ) -> None:
        sequence = (
            await self._session.execute(
                text(
                    """
                    SELECT COALESCE(MAX(event_sequence), 0) + 1
                    FROM offline_resource_sync_event WHERE batch_id = :batch_id
                    """
                ),
                {"batch_id": batch_id},
            )
        ).scalar_one()
        await self._session.execute(
            text(
                """
                INSERT INTO offline_resource_sync_event (
                    batch_id, event_sequence, action, result, actor_id, details_json, trace_id
                ) VALUES (
                    :batch_id, :event_sequence, :action, :result, :actor_id,
                    CAST(:details AS jsonb), :trace_id
                )
                """
            ),
            {
                "batch_id": batch_id,
                "event_sequence": sequence,
                "action": action,
                "result": result,
                "actor_id": actor_id,
                "details": self._json(details),
                "trace_id": trace_id,
            },
        )

    async def _mark_failed(self, batch_id: UUID, action: str, actor_id: str, trace_id: str, exc: Exception) -> None:
        error = self._error_summary(exc)
        await self._session.execute(
            text(
                """
                UPDATE offline_resource_sync_batch SET status = 'failed',
                    error_json = CAST(:error AS jsonb), updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = :batch_id
                """
            ),
            {"batch_id": batch_id, "error": self._json(error)},
        )
        await self._event(batch_id, action, "failed", actor_id, error, trace_id)

    async def _get_batch_locked(self, batch_id: str):
        row = (
            (
                await self._session.execute(
                    text("SELECT * FROM offline_resource_sync_batch WHERE batch_id = :batch_id FOR UPDATE"),
                    {"batch_id": batch_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise DiagnosisError(code="SYNC_BATCH_NOT_FOUND", message="同步批次不存在", http_status=404)
        return row

    async def _load_changes(self, batch_id: str) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    text(
                        "SELECT * FROM offline_resource_sync_change WHERE batch_id = :batch_id ORDER BY created_at, change_id"
                    ),
                    {"batch_id": batch_id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _equivalent_resource(before: dict[str, Any] | None, candidate: dict[str, Any]) -> bool:
        if before is None:
            return False
        left = dict(before)
        right = dict(candidate)
        left.pop("version", None)
        right.pop("version", None)
        return left == right

    @staticmethod
    def _equivalent_mapping(before: dict[str, Any] | None, candidate: dict[str, Any]) -> bool:
        if before is None:
            return False
        keys = (
            "mapping_id",
            "source_kbd_id",
            "source_kbd_revision",
            "source_signal_id",
            "execution_contract_checksum",
            "acquire_tool",
            "category_scope",
            "command_scope",
            "collector_id",
            "query_type",
            "field_mapping",
            "priority",
            "is_enabled",
        )
        return all(str(before.get(key)) == str(candidate.get(key)) for key in keys)

    @staticmethod
    def _error_summary(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, DiagnosisError):
            return {"code": exc.code, "message": exc.message, "details": exc.details}
        return {"code": type(exc).__name__, "message": str(exc)[:2000]}

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _row(row) -> dict[str, Any]:
        result = dict(row)
        for key, value in list(result.items()):
            if isinstance(value, UUID):
                result[key] = str(value)
        return result

    @staticmethod
    def _require_admin(actor: ActorContext) -> None:
        if not actor.has_any_role("platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权同步离线采集资源", http_status=403)
