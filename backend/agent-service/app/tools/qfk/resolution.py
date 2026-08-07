"""把既有 ``BackendSignal`` 接入 Shared Resolution Runtime。"""

from __future__ import annotations

from typing import Any

from shared.resolution import ResolvedAcquisition, SignalIntent, build_resolution_audit_snapshot, get_resolution_runtime

from app.tools.qfk.signal import BackendSignal


def resolve_backend_signal(
    signal: BackendSignal,
    *,
    variables: dict[str, Any] | None = None,
    path_exists: Any | None = None,
) -> ResolvedAcquisition:
    """在 Handler 构造命令前完成统一编译和消费前解析。

    旧 Handler 仍负责把日志 Matcher 粗筛、aCLI 全局参数等领域细节渲染为最终文本；
    Runtime 负责参数规范化、Catalog、候选路径和可审计 resolution 状态。现场路径探针
    由调用方在可用时提供；未提供时会返回 ``needs_probe`` warning，不会伪称物理文件存在。
    """

    resolver_id = "domain" if signal.namespace in {"vm", "network", "storage", "hardware", "platform"} else signal.namespace
    args = {
        "command": signal.command,
        "command_args": signal.command_args,
        "container": signal.container,
        "service": signal.service,
        "action": signal.action,
        "resource_keyword": signal.resource_keyword,
        "file": signal.file,
        # BackendSignal 为兼容既有 Handler 会填入 Catalog 默认 path；只有显式 path
        # 才能覆盖 Runtime 的 END/D/DD 候选展开。
        "path": None if signal.path_inferred else signal.path,
        "time_window": signal.time_window,
        "source_family": signal.source_family,
        "parser": signal.parser,
        "request_id": signal.request_id,
        "include_archives": signal.include_archives,
        "query": signal.namespace if signal.namespace in {"alert", "task", "dialog"} else None,
        "domain": signal.namespace,
    }
    args = {key: value for key, value in args.items() if value not in (None, "", [], False)}
    runtime = get_resolution_runtime()
    plan = runtime.compile(SignalIntent(resolver_id=resolver_id, tool=f"qfk_{signal.namespace}", args=args))
    acquisition = runtime.resolve(plan, {"variables": variables or {}, "path_exists": path_exists})
    # Keep a deterministic immutable snapshot alongside the QFK result until the
    # shared audit persistence table is available.
    snapshot = build_resolution_audit_snapshot(plan, acquisition)
    return acquisition.model_copy(update={"evidence": {**acquisition.evidence, "audit_snapshot": snapshot.model_dump(mode="json")}})
