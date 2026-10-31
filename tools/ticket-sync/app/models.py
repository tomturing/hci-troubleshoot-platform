"""任务阶段、错误码与统一异常。

错误码与设计文档第 8 节一一对应，前端按 code 做文案映射，
不解析 message 文本。
"""

from __future__ import annotations

import re
from enum import StrEnum


class Phase(StrEnum):
    """任务阶段。对外只暴露当前快照，不暴露迁移过程。"""

    QUEUED = "queued"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # 服务重启导致任务中途消失。必须与 failed 区分：它不是"同步失败"，
    # 重新触发通常就能成功，前端不应提示用户去排查日志源。
    INTERRUPTED = "interrupted"


class ErrorCode(StrEnum):
    """失败原因。每个取值都必须能被用户理解，不允许落到兜底文案。"""

    INVALID_TICKET_ID = "invalid-ticket-id"
    NOT_FOUND = "not-found"
    TOO_LARGE = "too-large"
    BAD_PASSWORD = "bad-password"
    ARCHIVE_CORRUPT = "archive-corrupt"
    NO_SPACE = "no-space"
    QUOTA_EXCEEDED = "quota-exceeded"
    DEPTH_EXCEEDED = "depth-exceeded"
    SSH_FAILED = "ssh-failed"
    EXTRACT_FAILED = "extract-failed"
    # 归档内容不是日志（实测有工单只含 34.6 GiB 的 vmdumpcore），
    # 按日志包同步只会白占磁盘，因此下载前识别并跳过。
    NON_LOG_PAYLOAD = "non-log-payload"


class TicketError(Exception):
    """带错误码的业务异常。

    detail 只进日志，不进接口响应：可能包含远端路径、stderr 片段等内部信息。
    """

    def __init__(self, code: ErrorCode, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# 工单号唯一合法形态：Q + 13 位数字（Q + YYYYMMDD + 5 位当天序号）。
# 与 case-service 的 generate_case_id() 及 LogCollectTaskCreate 契约一致。
TICKET_PATTERN = r"^Q\d{13}$"
TICKET_RE = re.compile(TICKET_PATTERN)

# 任务处于这些阶段时视为“仍在跑”：重复提交拒绝，取消才有意义。
RUNNING_PHASES = frozenset({Phase.QUEUED, Phase.FETCHING, Phase.EXTRACTING})


def validate_ticket_id(value: str) -> str:
    """校验并归一化工单号。

    服务端强制该规则，不接受任何路径分隔符或通配符，使 ticket_id 可以
    安全地拼接进 /sf/log 与 /sf/tickets 路径，不需要再做二次转义。
    """
    candidate = value.strip()
    if not TICKET_RE.match(candidate):
        raise TicketError(
            ErrorCode.INVALID_TICKET_ID,
            "工单号格式错误，应为 Q + 13 位数字",
            f"input={value!r}",
        )
    return candidate


class ExtractStats:
    """解压完成后的统计，用于日志与验收比对。"""

    __slots__ = (
        "depth",
        "files",
        "bytes",
        "archives",
        "symlinks_removed",
        "placeholders_skipped",
        "permissions_fixed",
        "unreadable_archives",
        "depth_truncated",
    )

    def __init__(self) -> None:
        self.depth = 0
        self.files = 0
        self.bytes = 0
        self.archives = 0
        self.symlinks_removed = 0
        # 采集端写坏的占位包数量：跳过而不是让整包失败，需在日志里可追踪。
        self.placeholders_skipped = 0
        # 权限补正条目数：源端目录缺执行位会让整棵子树不可读，必须可观测。
        self.permissions_fixed = 0
        # 解不开但保留原件的归档数（如 p7zip 不支持的 RAR5 压缩方法）。
        self.unreadable_archives = 0
        # 超出嵌套层数上限而停止下钻的归档数：日志主体仍可用，必须可观测。
        self.depth_truncated = 0

    def __repr__(self) -> str:
        return (
            f"ExtractStats(depth={self.depth}, files={self.files}, "
            f"bytes={self.bytes}, archives={self.archives}, "
            f"symlinks_removed={self.symlinks_removed}, "
            f"placeholders_skipped={self.placeholders_skipped}, "
            f"permissions_fixed={self.permissions_fixed}, "
            f"unreadable_archives={self.unreadable_archives}, "
            f"depth_truncated={self.depth_truncated})"
        )
