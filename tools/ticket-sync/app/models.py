"""任务阶段、错误码与统一异常。

错误码与设计文档第 8 节一一对应，前端按 code 做文案映射，
不解析 message 文本。
"""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """任务阶段。对外只暴露当前快照，不暴露迁移过程。"""

    QUEUED = "queued"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


class ExtractStats:
    """解压完成后的统计，用于日志与验收比对。"""

    __slots__ = ("depth", "files", "bytes", "archives", "symlinks_removed")

    def __init__(self) -> None:
        self.depth = 0
        self.files = 0
        self.bytes = 0
        self.archives = 0
        self.symlinks_removed = 0

    def __repr__(self) -> str:
        return (
            f"ExtractStats(depth={self.depth}, files={self.files}, "
            f"bytes={self.bytes}, archives={self.archives}, "
            f"symlinks_removed={self.symlinks_removed})"
        )
