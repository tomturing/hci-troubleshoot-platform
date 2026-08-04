"""HCI 日志源 Catalog 与 ``qfk_log`` 路径/时间安全契约。

Catalog 的职责是把稳定的日志知识（日志族、默认目录、解析器、支持的谓词）从
Prompt、Admin 示例和 Agent Handler 的条件分支中收口为一个单一真相源。它不是独立
执行工具：whitebox、blackbox、vn-blackbox 与 pod 日志仍统一由 ``qfk_log`` 通过
``acli log get`` 获取。常规日志根只有 ``/sf/log``；``/sf/data/local`` 不是日志族，
只是 aCLI 为 request_id 关联诊断产物开放的受限辅助搜索域。

设计边界来自 2026-07-30 HCI 实机 ``acli log get --help``：

* ``-p`` 只接受 ``/sf/log`` 和 ``/sf/data/local`` 下的绝对路径；
* ``-f`` 只接受 basename；
* ``-t`` 只接受绝对日期/时间，不接受 ``now``、``-1h`` 等相对表达式；
* ``-g`` 会搜索压缩归档，必须由调用方显式声明归档前置检查已通过。

Catalog 采用“已知源精确配置 + 安全通用回退”。因此新增普通 whitebox 文件不需要
修改代码；只有需要特殊目录、parser 或 predicate 的日志才需要新增条目。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

LOG_SOURCE_CATALOG_VERSION = "1.2"

LOG_SOURCE_FAMILIES = (
    "auto",
    "whitebox",
    "blackbox",
    "vn_blackbox",
    "pod",
)
LOG_PARSERS = (
    "plain_text",
    "timestamped_lines",
    "timestamped_blocks",
    "ifconfig_snapshot",
    "kv_counter_snapshot",
    "process_snapshot",
)
LOG_MATCHER_TYPES = (
    "keyword",
    "regex",
    "state",
    "threshold",
    "delta",
    "trend",
    "exists",
)

# aCLI 的真实授权根目录。注意：这里是 ``-p`` 的授权边界，不等于日志族列表；
# /sf/data/local 只能在 qfk_log Handler 中与 request_id 组合使用。
ALLOWED_LOG_ROOTS = ("/sf/log", "/sf/data/local")
LOG_ROOT = "/sf/log"
REQUEST_ARTIFACT_ROOT = "/sf/data/local"

_PLACEHOLDER = r"\{\{[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\}\}"
ABSOLUTE_LOG_TIME_PATTERN = (
    rf"^(?:{_PLACEHOLDER}|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"(?:[ T]\d{{2}}(?::\d{{2}}:\d{{2}})?)?)$"
)
_ABSOLUTE_LOG_TIME_RE = re.compile(ABSOLUTE_LOG_TIME_PATTERN)
_UNRESOLVED_HUMAN_DATE_RE = re.compile(r"[<>\[\]年月日日期]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class LogSourceDefinition:
    """单类日志的稳定采集与解释元数据。"""

    source_id: str
    file_pattern: str
    family: str
    default_path: str | None
    parser: str
    predicates: tuple[str, ...]
    description: str
    date_subpath: str | None = None
    runtime_supported: bool = True
    acquisition: str = "acli_log_get"

    def matches(self, file_name: str) -> bool:
        return bool(re.fullmatch(self.file_pattern, file_name))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["predicates"] = list(self.predicates)
        return data


# 顺序即优先级：精确/窄模式必须位于通用 LOG_* 和 whitebox 回退之前。
LOG_SOURCE_CATALOG: tuple[LogSourceDefinition, ...] = (
    LogSourceDefinition(
        source_id="external_bmc_event_log",
        file_pattern=r"BMC_Event_Log",
        family="whitebox",
        default_path=None,
        parser="plain_text",
        predicates=("keyword", "regex", "state", "exists"),
        description="BMC SEL/事件记录不是本机 /sf/log 文件，应由 qfk_hardware 对应 aCLI 能力获取",
        runtime_supported=False,
        acquisition="qfk_hardware",
    ),
    LogSourceDefinition(
        source_id="vn_ethtool_statistics",
        file_pattern=r"LOG_ethtool_(?:statistic|offload)\.txt",
        family="vn_blackbox",
        default_path="/sf/log/vn-blackbox/today",
        parser="kv_counter_snapshot",
        predicates=("keyword", "regex", "state", "threshold", "delta", "trend", "exists"),
        description="网络容器网卡计数器周期快照；适合丢包计数阈值、差值和趋势判定",
    ),
    LogSourceDefinition(
        source_id="vn_network_snapshot",
        file_pattern=(
            r"LOG_(?:arp|mgmt_ping_statistic|net_fail_slow|net_session_statics|"
            r"realethtool|sys_class_net|vxlan_ping_statistic)\.txt"
        ),
        family="vn_blackbox",
        default_path="/sf/log/vn-blackbox/today",
        parser="timestamped_blocks",
        predicates=("keyword", "regex", "state", "threshold", "delta", "trend", "exists"),
        description="网络容器专属 blackbox 周期快照",
    ),
    LogSourceDefinition(
        source_id="ifconfig_snapshot",
        file_pattern=r"LOG_ifconfig\.txt",
        family="blackbox",
        default_path="/sf/log/blackbox/today",
        parser="ifconfig_snapshot",
        predicates=("keyword", "regex", "state", "threshold", "delta", "trend", "exists"),
        description="宿主机或网络容器 ifconfig 周期快照；source_family 可显式选择 vn_blackbox",
    ),
    LogSourceDefinition(
        source_id="process_snapshot",
        file_pattern=r"LOG_ps_(?:user|kernel)\.txt",
        family="blackbox",
        default_path="/sf/log/blackbox/today",
        parser="process_snapshot",
        predicates=("keyword", "regex", "state", "threshold", "delta", "trend", "exists"),
        description="进程周期快照；适合进程存在性、CPU/内存阈值和持续趋势判定",
    ),
    LogSourceDefinition(
        source_id="host_blackbox",
        file_pattern=r"LOG_[A-Za-z0-9_.{}-]+",
        family="blackbox",
        default_path="/sf/log/blackbox/today",
        parser="timestamped_blocks",
        predicates=("keyword", "regex", "state", "threshold", "delta", "trend", "exists"),
        description="宿主机 blackbox 固定周期采样文件通用契约",
    ),
    LogSourceDefinition(
        source_id="vtpdaemon",
        file_pattern=r"sfvt_vtpdaemon\.log",
        family="whitebox",
        # 未提供可用 END 时必须回退到 /sf/log；不能把历史查询错误锁在 today。
        # END 已解析时由 qfk_log 按月内日号定位 /sf/log/<D 或 DD>/vt。
        default_path=LOG_ROOT,
        parser="timestamped_lines",
        predicates=("keyword", "regex", "state", "threshold", "exists"),
        description="vtpdaemon 白盒日志；END 可用时位于 /sf/log/<D或DD>/vt，未提供 END 时回退 /sf/log",
        date_subpath="vt",
    ),
    LogSourceDefinition(
        source_id="qemu_vm",
        file_pattern=rf"sfvt_qemu_(?:[A-Za-z0-9_.-]+|{_PLACEHOLDER})\.log",
        family="whitebox",
        default_path=LOG_ROOT,
        parser="timestamped_lines",
        predicates=("keyword", "regex", "state", "threshold", "exists"),
        description="按虚拟机标识分文件的 QEMU 白盒日志；END 可用时位于 /sf/log/<D或DD>/vt",
        date_subpath="vt",
    ),
    LogSourceDefinition(
        source_id="kernel",
        file_pattern=r"kernel\.log",
        family="whitebox",
        default_path=LOG_ROOT,
        parser="timestamped_lines",
        predicates=("keyword", "regex", "state", "threshold", "exists"),
        description="宿主机内核白盒日志；END 可用时位于 /sf/log/<D或DD>",
    ),
    LogSourceDefinition(
        source_id="system_messages",
        file_pattern=r"messages",
        family="whitebox",
        # 不强加 today：不同版本由 aCLI 在 /sf/log 默认搜索范围内定位系统日志。
        default_path=None,
        parser="timestamped_lines",
        predicates=("keyword", "regex", "state", "threshold", "exists"),
        description="系统 messages 日志；由 aCLI 默认日志根兼容不同版本布局",
    ),
    LogSourceDefinition(
        source_id="whitebox_common",
        file_pattern=r"[A-Za-z0-9_.{}-]+",
        family="whitebox",
        default_path=LOG_ROOT,
        parser="timestamped_lines",
        predicates=("keyword", "regex", "state", "threshold", "exists"),
        description="普通 HCI whitebox 文本日志的安全回退；未提供可用 END 时回退 /sf/log",
    ),
)


def validate_absolute_log_time(value: str | None) -> tuple[bool, str | None]:
    """验证 ``acli log get -t`` 的绝对时间语义。"""

    if not value:
        return True, None
    if re.fullmatch(_PLACEHOLDER, value):
        return True, None
    if _ABSOLUTE_LOG_TIME_RE.fullmatch(value):
        normalized = value.replace("T", " ", 1)
        formats = ("%Y-%m-%d", "%Y-%m-%d %H", "%Y-%m-%d %H:%M:%S")
        if any(_is_valid_datetime(normalized, fmt) for fmt in formats):
            return True, None
    return (
        False,
        "日志时间必须是绝对时间：YYYY-MM-DD、YYYY-MM-DD HH、YYYY-MM-DD HH:MM:SS "
        "或单一 {{ABSOLUTE_TIME}} 变量，且日期/时分秒必须真实有效；"
        "now/-1h 等相对时间须由 Agent 先解析",
    )


def _is_valid_datetime(value: str, fmt: str) -> bool:
    """严格按单一格式校验，避免 ``strptime`` 接受缺少前导零的宽松输入。"""

    try:
        parsed = datetime.strptime(value, fmt)
    except ValueError:
        return False
    return parsed.strftime(fmt) == value


def normalize_absolute_log_time(value: str | None) -> str | None:
    """把合法 ISO 日期时间的 ``T`` 转为 aCLI 接受的空格，变量保持原样。"""

    if not value:
        return None
    ok, error = validate_absolute_log_time(value)
    if not ok:
        raise ValueError(error)
    return value.replace("T", " ", 1)


def normalize_log_path(path: str | None) -> str | None:
    """规范化并验证 aCLI 日志路径，拒绝越权、遍历和人工日期占位符。"""

    if path is None or not str(path).strip():
        return None
    raw = str(path).strip()
    if _CONTROL_RE.search(raw) or "\\" in raw:
        raise ValueError("日志路径包含控制字符或反斜杠")
    if _UNRESOLVED_HUMAN_DATE_RE.search(raw):
        raise ValueError("日志路径不能包含 <日期>/[日期] 等人工占位符，请使用 today 或 {{LOG_DATE}}")
    if not raw.startswith("/"):
        raise ValueError("日志路径必须是绝对路径")
    if any(part == ".." for part in raw.split("/")):
        raise ValueError("日志路径禁止包含 ..")
    # aCLI 仅声明 * 通配符；拒绝 shell/正则的其他路径元字符。
    if any(char in raw for char in "?[]{};|&`$<>\n\r"):
        # 允许规范变量占位符，替换后再检查花括号与美元符号。
        without_placeholders = re.sub(_PLACEHOLDER, "VALUE", raw)
        if any(char in without_placeholders for char in "?[]{};|&`$<>\n\r"):
            raise ValueError("日志路径仅允许 aCLI 支持的 * 通配符和规范 {{VAR}} 变量")

    normalized = posixpath.normpath(raw)
    if normalized == ".":
        raise ValueError("日志路径不能为空")
    if not any(normalized == root or normalized.startswith(f"{root}/") for root in ALLOWED_LOG_ROOTS):
        raise ValueError(f"日志路径只允许位于: {ALLOWED_LOG_ROOTS}")
    return normalized


def infer_log_family(path: str | None) -> str | None:
    """从已规范路径推断日志族；未知路径返回 ``None``。"""

    if not path:
        return None
    normalized = normalize_log_path(path)
    if normalized and normalized.startswith("/sf/log/vn-blackbox/"):
        return "vn_blackbox"
    if normalized and normalized.startswith("/sf/log/blackbox/"):
        return "blackbox"
    if normalized and normalized.startswith("/sf/log/pods/"):
        return "pod"
    # /sf/data/local 不是日志族，不能在此伪装成 whitebox/data_local。
    if normalized and normalized.startswith(REQUEST_ARTIFACT_ROOT):
        return None
    if normalized and normalized.startswith("/sf/log"):
        return "whitebox"
    return None


def resolve_log_source(
    file_name: str,
    *,
    source_family: str = "auto",
    path: str | None = None,
    parser: str | None = None,
) -> dict[str, Any]:
    """解析日志文件的 Catalog 元数据和最终默认路径。

    ``source_family``/显式 ``path`` 优先于 Catalog 默认族，用于处理
    ``LOG_ifconfig.txt`` 同时存在于 host 与 vn blackbox 的真实歧义。
    """

    if source_family not in LOG_SOURCE_FAMILIES:
        raise ValueError(f"未知日志族: {source_family}")
    requested_path = normalize_log_path(path)
    path_family = infer_log_family(requested_path)
    definition = next((item for item in LOG_SOURCE_CATALOG if item.matches(file_name)), None)
    if definition is None:  # 理论上由 whitebox_common 兜底，保留 fail-safe。
        raise ValueError(f"没有可用的日志源定义: {file_name}")

    family = path_family or (source_family if source_family != "auto" else definition.family)
    default_paths = {
        "whitebox": definition.default_path if definition.family == "whitebox" else "/sf/log/today",
        "blackbox": "/sf/log/blackbox/today",
        "vn_blackbox": "/sf/log/vn-blackbox/today",
        "pod": "/sf/log/pods",
    }
    resolved_parser = parser or definition.parser
    if resolved_parser not in LOG_PARSERS:
        raise ValueError(f"未知日志 parser: {resolved_parser}")
    return {
        **definition.to_dict(),
        "family": family,
        "path": requested_path if requested_path is not None else default_paths.get(family),
        "parser": resolved_parser,
        "catalog_version": LOG_SOURCE_CATALOG_VERSION,
        "path_inferred": requested_path is None,
    }


def log_source_catalog_document() -> dict[str, Any]:
    """供 Admin/API/文档测试消费的稳定 Catalog 文档。"""

    return {
        "schema_version": 1,
        "catalog_version": LOG_SOURCE_CATALOG_VERSION,
        "acquisition": "qfk_log/acli_log_get",
        "allowed_roots": list(ALLOWED_LOG_ROOTS),
        "log_root": LOG_ROOT,
        "request_artifact_root": REQUEST_ARTIFACT_ROOT,
        "request_artifact_policy": "仅允许携带 request_id 的辅助关联搜索，不属于日志源 family",
        "families": list(LOG_SOURCE_FAMILIES),
        "parsers": list(LOG_PARSERS),
        "matchers": list(LOG_MATCHER_TYPES),
        "sources": [item.to_dict() for item in LOG_SOURCE_CATALOG],
    }
