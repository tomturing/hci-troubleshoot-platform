"""
BackendSignal — QFK 后端排查信号（v2 扁平运行时模型）

字段命名与 acquirer_args 契约及 signals_json v2 完全一致：
- namespace    对应 acquirer 名称（log/service/system/vm/network/storage/hardware/platform）
- host         Terminal Bridge/SSH 的目标主机路由（不拼入 aCLI 命令）
- command      acli <namespace> <command>
- file/path/source_family/parser  qfk_log 的日志源 Catalog 定位与解析
- time_window  qfk_log 的绝对 -t（相对时间在进入 QFK 前解析）
- service/action qfk_service 的 <container> <name> <action>
- container    qfk_system 的 aCLI ``--container`` 执行域（不是 Terminal Bridge 容器）
- keyword      匹配关键字（list[str]）
- instruction  匹配说明
- match_mode   or/and/not
- expected     True=期望命中，False=期望不命中（取反语义）
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.schemas.acquirer_args import (
    DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    VALID_SYSTEM_CONTAINERS,
    normalize_qfk_system_args,
)
from shared.schemas.log_source_catalog import (
    LOG_PARSERS,
    LOG_SOURCE_FAMILIES,
    REQUEST_ARTIFACT_ROOT,
    normalize_log_path,
    resolve_log_source,
    validate_absolute_log_time,
)

# ─── 有效匹配模式 ──────────────────────────────────────────────────────────────
VALID_MATCH_MODES = {"or", "and", "not"}

class BackendSignal(BaseModel):
    """QFK 后端排查信号运行时模型（扁平 v2）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ─── 命名空间 ─────────────────────────────────────────────────────────────
    namespace: str = Field(
        ..., description="acli 子命令空间名：log/service/system/vm/network/storage/hardware/platform"
    )

    # ─── 主机作用域 ───────────────────────────────────────────────────────────
    host: str | None = Field(default=None, description="目标主机；由 Terminal Bridge 选择 SSH 会话，不拼入 aCLI")
    vm: str | None = Field(default=None, description="虚拟机标识")
    timeout: int = Field(default=DEFAULT_SIGNAL_TIMEOUT_SECONDS, ge=1, le=300, description="执行超时（秒，1-300）")
    container: str | None = Field(default=None, description="qfk_system 的 aCLI --container 或 qfk_service 服务组")
    cluster: bool = Field(default=False, description="qfk_system 是否添加 acli --cluster")
    formatter: str | None = Field(default=None, description="qfk_system 的 aCLI --formatter")

    # ─── 特有字段 ─────────────────────────────────────────────────────────────
    command: str | None = Field(default=None, description="acli 子命令（vm/network/storage/hardware/platform/system）")
    command_args: list[str] = Field(default_factory=list, description="qfk_system 结构化命令参数")
    file: str | None = Field(default=None, description="日志安全 basename（qfk_log 的 -f）")
    path: str | None = Field(default=None, description="日志绝对路径；省略时由 Catalog 推断")
    path_inferred: bool = Field(default=False, exclude=True, description="运行时内部字段：path 是否由 Catalog 推断")
    time_window: str | None = Field(default=None, description="日志绝对日期/时间（qfk_log 的 -t）")
    source_family: str = Field(default="auto", description="whitebox/blackbox/vn_blackbox/pod/auto")
    parser: str | None = Field(default=None, description="日志结构 parser；省略时由 Catalog 推断")
    request_id: str | None = Field(default=None, description="调用链 request_id（qfk_log 的 -i）")
    context_lines: int = Field(default=0, ge=0, le=50, description="日志命中上下文行数（qfk_log 的 -c）")
    include_archives: bool = Field(default=False, description="是否搜索 .gz 历史归档（qfk_log 的 -g）")
    archive_precheck: str | None = Field(default=None, description="归档磁盘/日期/路径前置检查状态")
    service: str | None = Field(default=None, description="服务名（qfk_service）")
    action: str | None = Field(default=None, description="服务操作（qfk_service，默认 status）")
    resource_keyword: str | None = Field(default=None, description="命令的只读资源过滤参数")

    # ─── 匹配配置 ─────────────────────────────────────────────────────────────
    keyword: list[str] = Field(default_factory=list, description="匹配关键字列表")
    match_mode: str = Field(default="or", description="关键字匹配模式：or/and/not")
    instruction: str | None = Field(default=None, description="匹配说明（人类可读）")
    expected: bool = Field(default=True, description="预期是否命中；False 表示取反语义")
    matcher: dict[str, Any] | None = Field(default=None, description="完整结构化 Matcher；KBD v2 运行时使用")

    # ─── 校验 ─────────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def _validate(self) -> BackendSignal:
        # 已发布的旧 v2 文档曾把 ``host`` 错写为 qfk_system.container。其原始
        # 运行含义是“不要进入 Bridge 容器”，与当前“省略 aCLI --container”一致；
        # 在运行时显式归一，既不把 acli 放进容器，也不静默改变旧案例的执行域。
        if self.namespace == "system" and self.container == "host":
            self.container = None
        if self.namespace == "system":
            try:
                normalized = normalize_qfk_system_args(
                    {
                        "command": self.command,
                        "command_args": self.command_args,
                        **({"resource_keyword": self.resource_keyword} if self.resource_keyword else {}),
                    }
                )
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            self.command = normalized["command"]
            self.command_args = normalized["command_args"]
            self.resource_keyword = None
        if self.match_mode not in VALID_MATCH_MODES:
            raise ValueError(f"match_mode 必须是 {VALID_MATCH_MODES} 之一，收到: {self.match_mode}")
        if self.namespace == "system" and self.container and self.container not in VALID_SYSTEM_CONTAINERS:
            raise ValueError(
                f"qfk_system 容器必须是 {sorted(VALID_SYSTEM_CONTAINERS)} 之一，收到: {self.container}"
            )
        if self.namespace == "system" and self.formatter and self.formatter not in {"xml", "csv", "keyvalue", "json"}:
            raise ValueError("qfk_system formatter 必须是 xml/csv/keyvalue/json 之一")
        if self.namespace == "log":
            self.path_inferred = self.path is None
            if self.source_family not in LOG_SOURCE_FAMILIES:
                raise ValueError(f"qfk_log source_family 必须是 {LOG_SOURCE_FAMILIES} 之一")
            if self.parser and self.parser not in LOG_PARSERS:
                raise ValueError(f"qfk_log parser 必须是 {LOG_PARSERS} 之一")
            try:
                self.path = normalize_log_path(self.path)
            except ValueError as exc:
                raise ValueError(f"qfk_log 日志路径不可解析: {exc}") from exc
            is_request_artifact = bool(
                self.path
                and (self.path == REQUEST_ARTIFACT_ROOT or self.path.startswith(f"{REQUEST_ARTIFACT_ROOT}/"))
            )
            if is_request_artifact:
                if not self.request_id:
                    raise ValueError("/sf/data/local 不是日志目录；仅允许携带 request_id 的辅助关联搜索")
                if self.source_family != "auto":
                    raise ValueError("/sf/data/local 辅助搜索不得声明日志 source_family")
                source = {"runtime_supported": True, "parser": "plain_text"}
            else:
                if not self.file:
                    raise ValueError("常规 qfk_log 必须提供 /sf/log 下的日志文件 basename")
                try:
                    source = resolve_log_source(
                        self.file,
                        source_family=self.source_family,
                        path=self.path,
                        parser=self.parser,
                    )
                except ValueError as exc:
                    raise ValueError(f"qfk_log 日志源不可解析: {exc}") from exc
            if not source.get("runtime_supported", True):
                raise ValueError(
                    f"日志源 {source.get('source_id')} 不能由 qfk_log 获取，应使用 {source.get('acquisition')}"
                )
            if self.path is None:
                self.path = source.get("path")
            if self.parser is None:
                self.parser = str(source.get("parser") or "plain_text")
            ok, error = validate_absolute_log_time(self.time_window)
            if not ok:
                raise ValueError(error)
            if self.include_archives and self.archive_precheck != "verified":
                raise ValueError("qfk_log 搜索归档前必须设置 archive_precheck=verified")
            if self.archive_precheck and not self.include_archives:
                raise ValueError("archive_precheck 只能与 include_archives=true 同时使用")
        return self

    # ─── 工具方法 ─────────────────────────────────────────────────────────────
    def is_cluster_mode(self) -> bool:
        """判断是否为集群模式"""
        # ``host=cluster`` 是旧数据兼容写法；新信号使用显式 cluster=true。
        return self.cluster or self.host == "cluster"

    @classmethod
    def from_dict(cls, data: Any) -> BackendSignal:
        """从字典/JSON 字符串构建并校验"""
        if isinstance(data, str):
            data = json.loads(data)
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str) -> BackendSignal:
        """从 JSON 字符串构建并校验"""
        return cls.model_validate(json.loads(json_str))
