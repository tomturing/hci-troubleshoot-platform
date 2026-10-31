"""任务与产物用量的持久化存储（SQLite）。

为什么需要这两张表：
- `jobs`：任务状态原本只存进程内存，服务一重启就"失忆"——前端 `/artifacts`
  看不到历史，"这个工单上次同步成功没"只能翻 journal 反推。
- `artifacts`：容量与保留期每次都要遍历目录、读 meta。工单体量只增不减，
  遍历成本随规模线性上升；把用量固化成一行后，聚合变成 O(1)。

设计边界：
- 内存 `JobRegistry` 仍是运行态权威，DB 是它的持久化镜像，不参与并发控制；
- 重启时把遗留的 running 阶段改写为 `interrupted`，避免前端看到永远不动的任务；
- SQLite WAL 下单条 upsert 约 0.1ms，与"全盘 walk 3.5 分钟"不是一个量级，
  因此在事件循环里同步调用是安全的，不另起线程池（否则写顺序无从保证）。

DB 文件放在产物根目录下并加前导点：与产物同盘便于一起备份/迁移，
点开头不会被 DSH 的文件选择器展示，且不匹配 `Q\\d{13}` 因此被清理逻辑忽略。
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 历史上限：只保留最近若干条，避免表无限增长。任务历史的价值集中在最近几天。
JOB_HISTORY_LIMIT = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    ticket_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    message TEXT,
    received_bytes INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    extracted_bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    current_archive TEXT,
    error_code TEXT,
    accepted_at REAL NOT NULL,
    started_at REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    ticket_id TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0,
    has_logs INTEGER,
    completed_at REAL NOT NULL,
    dir_mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_order ON artifacts(has_logs, completed_at);
"""


def _locked(method):
    """串行化连接访问。

    `check_same_thread=False` 让维护任务能在 `asyncio.to_thread` 的线程里
    读写同一连接，代价是必须自己保证不并发——sqlite3 连接不是线程安全的。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    """单文件 SQLite 存储。进程内单连接，调用方保证不跨线程使用。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 维护跑在线程池里，同一连接必须允许跨线程使用（串行化由 _locked 保证）。
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL 让读写不互相阻塞；NORMAL 在"掉电丢最后一条"与性能之间取平衡，
        # 这里丢一条任务状态的代价远小于 fsync 的开销。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """补列式迁移。

        `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加新列，后续加字段必须
        显式 ALTER，否则老库会在第一次写入时报 "no column named ..."。
        """
        try:
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(artifacts)")}
            if columns and "dir_mtime" not in columns:
                self._conn.execute("ALTER TABLE artifacts ADD COLUMN dir_mtime REAL")
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("存储迁移失败 path=%s error=%s", self.path, exc)

    def close(self) -> None:
        self._conn.close()

    # ---------------- 任务历史 ----------------

    @_locked
    def upsert_job(
        self,
        ticket_id: str,
        job_id: str,
        phase: str,
        accepted_at: datetime,
        updated_at: datetime,
        *,
        message: str | None = None,
        received_bytes: int = 0,
        total_bytes: int = 0,
        extracted_bytes: int = 0,
        file_count: int = 0,
        current_archive: str | None = None,
        error_code: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        """写入一条任务快照。失败只记日志：持久化不能影响同步主流程。"""
        try:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    ticket_id, job_id, phase, message, received_bytes, total_bytes,
                    extracted_bytes, file_count, current_archive, error_code,
                    accepted_at, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    job_id=excluded.job_id,
                    phase=excluded.phase,
                    message=excluded.message,
                    received_bytes=excluded.received_bytes,
                    total_bytes=excluded.total_bytes,
                    extracted_bytes=excluded.extracted_bytes,
                    file_count=excluded.file_count,
                    current_archive=excluded.current_archive,
                    error_code=excluded.error_code,
                    accepted_at=excluded.accepted_at,
                    started_at=COALESCE(excluded.started_at, jobs.started_at),
                    updated_at=excluded.updated_at
                """,
                (
                    ticket_id,
                    job_id,
                    phase,
                    message,
                    received_bytes,
                    total_bytes,
                    extracted_bytes,
                    file_count,
                    current_archive,
                    error_code,
                    accepted_at.timestamp(),
                    started_at.timestamp() if started_at else None,
                    updated_at.timestamp(),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("写入任务历史失败 ticket=%s error=%s", ticket_id, exc)

    @_locked
    def mark_interrupted(self) -> int:
        """把上次进程遗留的运行态任务标记为 interrupted，返回改写条数。

        进程被杀或重启后，这些任务不可能再继续；保持 running 会让前端永久
        显示"同步中"，而实际没有任何协程在跑。
        """
        try:
            cursor = self._conn.execute(
                "UPDATE jobs SET phase='interrupted', "
                "message='服务重启，任务已中断，可重新触发', updated_at=? "
                "WHERE phase IN ('queued','fetching','extracting')",
                (datetime.now().timestamp(),),
            )
            self._conn.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            logger.warning("标记中断任务失败 error=%s", exc)
            return 0

    @_locked
    def load_jobs(self, limit: int = JOB_HISTORY_LIMIT) -> list[dict[str, Any]]:
        """按更新时间倒序读取任务历史。"""
        try:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("读取任务历史失败 error=%s", exc)
            return []
        return [dict(row) for row in rows]

    @_locked
    def prune_jobs(self, keep: int = JOB_HISTORY_LIMIT) -> int:
        """只保留最近 keep 条历史，返回删除条数。"""
        try:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE ticket_id NOT IN "
                "(SELECT ticket_id FROM jobs ORDER BY updated_at DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            logger.warning("裁剪任务历史失败 error=%s", exc)
            return 0

    # ---------------- 产物用量 ----------------

    @_locked
    def upsert_artifact(
        self,
        ticket_id: str,
        bytes_: int,
        files: int,
        has_logs: bool | None,
        completed_at: float,
        dir_mtime: float | None = None,
    ) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO artifacts (ticket_id, bytes, files, has_logs, completed_at, dir_mtime)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    bytes=excluded.bytes,
                    files=excluded.files,
                    has_logs=excluded.has_logs,
                    completed_at=excluded.completed_at,
                    dir_mtime=excluded.dir_mtime
                """,
                (
                    ticket_id,
                    bytes_,
                    files,
                    None if has_logs is None else int(has_logs),
                    completed_at,
                    dir_mtime,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("写入产物用量失败 ticket=%s error=%s", ticket_id, exc)

    @_locked
    def delete_artifact(self, ticket_id: str) -> None:
        try:
            self._conn.execute("DELETE FROM artifacts WHERE ticket_id=?", (ticket_id,))
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("删除产物用量失败 ticket=%s error=%s", ticket_id, exc)

    @_locked
    def artifact_map(self) -> dict[str, dict[str, Any]]:
        """返回 {工单号: 行}，供容量/保留期与磁盘现状对账。"""
        try:
            rows = self._conn.execute("SELECT * FROM artifacts").fetchall()
        except sqlite3.Error as exc:
            logger.warning("读取产物用量失败 error=%s", exc)
            return {}
        return {row["ticket_id"]: dict(row) for row in rows}

    @_locked
    def artifact_total(self) -> int:
        try:
            row = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM artifacts").fetchone()
        except sqlite3.Error as exc:
            logger.warning("汇总产物用量失败 error=%s", exc)
            return 0
        return int(row[0]) if row else 0

    @_locked
    def eviction_order(self, oldest_allowed: float) -> list[dict[str, Any]]:
        """按"非日志优先、再最旧优先"返回可淘汰行。

        `oldest_allowed` 是最短保护期对应的截止时间戳（completed_at 必须小于它）。
        """
        try:
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE completed_at < ? "
                "ORDER BY COALESCE(has_logs, 1) ASC, completed_at ASC",
                (oldest_allowed,),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("读取淘汰候选失败 error=%s", exc)
            return []
        return [dict(row) for row in rows]
