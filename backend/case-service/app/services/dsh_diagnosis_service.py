"""DSH 诊断会话归档：把「AI 怎么诊断的」变成工单可回看的证据。

第一性原理
----------
DSH 没有给平台留任何 API，也没有回调：它的会话真源就是容器内的
``$DSH_HOME/sessions/<workspace>/<session-id>/session.v4.jsonl.zstd``。
因此平台只能做两件事：
1. **不复制推理**：不重放、不改写，只把会话文件读成结构化记录；
2. **可重放**：解析是幂等的（``session_id`` 唯一键 + 文件 size/mtime/sha256 判重），
   删表后重扫即可恢复，真源永远在 DSH 侧。

两个必须守住的边界
------------------
- **不猜工单**：只有拿到确定性依据（WeCom 注入的 log task directory、cwd、首条消息/标题里
  唯一出现的工单号）才写 ``case_id``；多义或缺失一律留空，宁可这条记录不显示，也不能把
  A 工单的诊断挂到 B 工单上。
- **不吞证据**：解析失败只记日志并跳过该文件，绝不清空已入库记录；文件正在写入导致最后
  一帧解压失败时，按「已解出的部分」入库，下一次扫描再补全。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger("dsh-diagnosis")

# 工单号：Q + 年月日 + 当日序号（与 ticket-sync 的 ^Q\d{13}$ 对齐，同时兼容 12 位历史号）
_CASE_RE = re.compile(r"Q\d{12,13}")
# WeCom 插件在首条消息里注入的确定性锚点；存在时无需任何猜测
_WECOM_MARKER_RE = re.compile(r"log task directory\s*[:：]\s*[\"“]?(Q\d{12,13})")

# 单条消息截断上限：对话是给人看的，不是全量日志；tool 输出最容易爆炸
_MAX_USER_CHARS = 4000
_MAX_ASSISTANT_CHARS = 12000
_MAX_TOOL_RESULT_CHARS = 800
_MAX_TOOL_ARGS_CHARS = 400
_MAX_TITLE_CHARS = 200
# 一条会话最多保留的消息条数；超出后丢弃后续条目并显式标记，避免写进一个几 MB 的 blob
_MAX_MESSAGES = 300

# 解析并发闸门：一次只允许一个协程扫目录，避免同一次页面刷新把同一批文件解压多遍。
# 按事件循环分别持有：asyncio.Lock 一旦被使用就与创建时的 loop 绑定，
# 进程内单 loop 无差异，但测试里每个用例一个新 loop，共用一把会直接 RuntimeError。
_INGEST_LOCKS: dict[int, asyncio.Lock] = {}


def _ingest_lock() -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    lock = _INGEST_LOCKS.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _INGEST_LOCKS[loop_id] = lock
    return lock


@dataclass(slots=True)
class ParsedSession:
    """一条 DSH 会话的解析结果（与数据库列一一对应）。"""

    session_id: str
    case_id: str | None
    binding_source: str
    title: str | None
    workspace: str | None
    agent_preset: str | None
    started_at: datetime | None
    last_activity_at: datetime | None
    message_count: int
    tool_call_count: int
    conclusion_markdown: str | None
    messages: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


@dataclass(slots=True)
class IngestStats:
    """一次扫描的可观测结果（日志与接口响应共用）。"""

    scanned: int = 0
    parsed: int = 0
    skipped: int = 0
    failed: int = 0
    unbounded: int = 0
    root_missing: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "parsed": self.parsed,
            "skipped": self.skipped,
            "failed": self.failed,
            "unbounded": self.unbounded,
            "root_missing": self.root_missing,
        }


def decode_zstd_frames(raw: bytes, *, max_bytes: int) -> bytes:
    """解压 DSH 的会话文件。

    实测：DSH 每批事件独立一帧、顺序追加，所以一个文件是**多帧拼接**的流；
    只用一次性 ``decompress`` 接口只能拿到第一帧（193 字节），会得到「会话是空的」的错误结论。
    这里用 ``read_across_frames=True`` 跨帧读取；最后一帧可能正在写（不完整），
    解到哪算哪，剩余部分等下一次扫描。
    """
    import zstandard

    out = bytearray()
    dctx = zstandard.ZstdDecompressor()
    with dctx.stream_reader(io.BytesIO(raw), read_across_frames=True) as reader:
        try:
            while len(out) < max_bytes:
                chunk = reader.read(1 << 20)
                if not chunk:
                    break
                out.extend(chunk)
        except zstandard.ZstdError as exc:  # 尾部不完整帧属预期，不当作解析失败
            logger.debug(event="dsh_session_partial_frame", message=str(exc), decoded=len(out))
    return bytes(out[:max_bytes])


def _ms_to_dt(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _text_blocks(content: Any) -> str:
    """把 DSH 的 content block 数组压成纯文本（reasoning/tool-call 不算正文）。"""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _sanitize(value: str) -> str:
    """去掉 PostgreSQL 无法存储的字符（NUL 与孤立代理项）。

    第一性原理：PostgreSQL 的 text/jsonb **物理上不接受** NUL，写入时报
    `UntranslatableCharacterError: unsupported Unicode escape sequence`。
    而 DSH 的工具输出里确实会出现 NUL（`cat` 二进制文件、`hexdump` 片段），
    一个字节就能让整条会话入库失败（2026-11-02 staging 实测：端点 500，日志里只有
    asyncpg 的这条错误，没有任何业务线索）。宁可在归档侧丢这个不可见字符，
    也不能丢整条诊断证据。

    ⚠️ 只清洗**数据**，绝不清洗序列化后的 JSON 文本：`json.dumps` 会把 NUL 转成
    字面量 `\\u0000`，而正文里本来就可能出现 `\\u0000` 这 6 个字符（例如日志里贴的
    JS 正则 `/[\\\\-\\u0008]/g`）。对序列化结果做字符串替换会打断转义序列
    （`\\\\u0000` → 只剩 `\\`），生成非法 JSON（2026-11-03 实测：
    `Escape sequence "\\-" is invalid`）。正确做法是清洗结构里的每个字符串，
    再整体序列化。
    """
    if not value:
        return value
    value = value.replace("\x00", "")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # 孤立代理项（JSON 里 `\\ud800` 之类的转义）无法编码为 UTF-8
        value = value.encode("utf-8", "ignore").decode("utf-8")
    return value


def _scrub(value: Any) -> Any:
    """递归清洗待入库结构里的所有字符串，保证后续 `json.dumps` 产出合法 JSON。"""
    if isinstance(value, str):
        return _sanitize(value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def _clip(value: str, limit: int) -> str:
    # 所有入库字符串都经过这里，因此把 NUL 清洗放在入口处，避免漏网
    value = _sanitize(value)
    if len(value) <= limit:
        return value
    return value[:limit] + "…（已截断）"


def unique_case_id(value: str | None) -> str | None:
    """文本里恰好出现一个工单号时才返回；出现多个视为多义。"""
    if not value:
        return None
    found = set(_CASE_RE.findall(value))
    return found.pop() if len(found) == 1 else None


def resolve_binding(
    *,
    workspace: str | None,
    title: str | None,
    first_user_text: str | None,
) -> tuple[str | None, str]:
    """按确定性从强到弱识别工单号。

    顺序即优先级，不可随意调整：
    1. ``wecom_marker``：WeCom 插件注入的 ``log task directory``，一字不差，最强证据；
    2. ``cwd``：会话工作目录本身就落在某个工单目录下；
    3. ``first_user`` / ``title``：用户自己打的工单号，且**全文只有一个**；
    4. 其余一律 ``none``——会话里会大量出现目录列举出来的其他工单号（日志根目录列表），
       用「出现次数最多」去猜必然错挂。
    """
    marker = _WECOM_MARKER_RE.search(first_user_text or "")
    if marker:
        return marker.group(1), "wecom_marker"

    cwd_match = re.search(r"/tickets/(Q\d{12,13})(?:/|$)", workspace or "")
    if cwd_match:
        return cwd_match.group(1), "cwd"

    from_user = unique_case_id(first_user_text)
    if from_user:
        return from_user, "first_user"

    from_title = unique_case_id(title)
    if from_title:
        return from_title, "title"

    return None, "none"


def parse_session_text(raw_text: str) -> ParsedSession | None:
    """把解压后的 JSONL 文本解析成一条会话记录。

    仅保留「用户 / 助手 / 工具」三类可读事件：心跳、retry、request/header 等运维事件
    对事后复盘没有价值，全量落库只会让详情页变成噪声墙。
    """
    header: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    tool_call_count = 0
    title: str | None = None
    last_time: datetime | None = None
    truncated = False
    first_user_text: str | None = None

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # 尾部半行（文件正在写）属正常，跳过即可
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        data = event.get("data") or {}
        event_time = _ms_to_dt(event.get("time"))
        if event_time is not None:
            last_time = event_time

        if event_type == "session" and header is None:
            header = event
            continue
        if event_type == "session/title":
            candidate = data.get("title")
            if isinstance(candidate, str) and candidate.strip():
                title = _clip(candidate.strip(), _MAX_TITLE_CHARS)
            continue
        if event_type == "user/message":
            content = _text_blocks(data.get("content"))
            if not content:
                continue
            if first_user_text is None:
                first_user_text = content
            if len(messages) < _MAX_MESSAGES:
                messages.append(
                    {
                        "seq": event.get("seq"),
                        "role": "user",
                        "text": _clip(content, _MAX_USER_CHARS),
                        "time": event_time.isoformat() if event_time else None,
                    }
                )
            else:
                truncated = True
            continue
        if event_type == "assistant/message":
            message = data.get("message") or {}
            content = _text_blocks(message.get("content"))
            calls = [
                {
                    "name": block.get("name"),
                    "arguments": _clip(str(block.get("arguments") or ""), _MAX_TOOL_ARGS_CHARS),
                }
                for block in (message.get("content") or [])
                if isinstance(block, dict) and block.get("type") == "tool-call"
            ]
            if not content and not calls:
                continue
            item: dict[str, Any] = {
                "seq": event.get("seq"),
                "role": "assistant",
                "text": _clip(content, _MAX_ASSISTANT_CHARS),
                "time": event_time.isoformat() if event_time else None,
            }
            if calls:
                item["tool_calls"] = calls
            if len(messages) < _MAX_MESSAGES:
                messages.append(item)
            else:
                truncated = True
            continue
        if event_type == "tool/call":
            tool_call_count += 1
            call_id = data.get("callId")
            name = data.get("name")
            if call_id and name:
                tool_names[str(call_id)] = str(name)
            continue
        if event_type == "tool/result":
            message = data.get("message") or {}
            content = _text_blocks(message.get("content"))
            call_id = str(message.get("toolCallId") or data.get("callId") or "")
            if len(messages) < _MAX_MESSAGES:
                messages.append(
                    {
                        "seq": event.get("seq"),
                        "role": "tool",
                        "name": tool_names.get(call_id, "tool"),
                        "text": _clip(content, _MAX_TOOL_RESULT_CHARS),
                        "is_error": bool(message.get("isError")),
                        "time": event_time.isoformat() if event_time else None,
                    }
                )
            else:
                truncated = True
            continue

    if header is None:
        return None
    session_id = str(header.get("id") or "").strip()
    if not session_id:
        return None

    workspace = header.get("cwd")
    agent_preset = header.get("agentPreset")
    case_id, binding_source = resolve_binding(
        workspace=workspace,
        title=title,
        first_user_text=first_user_text,
    )

    conclusion = next(
        (item["text"] for item in reversed(messages) if item["role"] == "assistant" and item["text"]),
        None,
    )
    if truncated:
        messages.append(
            {
                "seq": None,
                "role": "system",
                "text": f"（会话过长，仅保留前 {_MAX_MESSAGES} 条；最终结论不受影响）",
                "time": None,
            }
        )

    return ParsedSession(
        session_id=session_id,
        case_id=case_id,
        binding_source=binding_source,
        title=title,
        workspace=str(workspace)[:255] if workspace else None,
        agent_preset=str(agent_preset)[:64] if agent_preset else None,
        started_at=_ms_to_dt(header.get("createdAt")),
        last_activity_at=last_time,
        message_count=sum(1 for item in messages if item["role"] in {"user", "assistant"}),
        tool_call_count=tool_call_count,
        conclusion_markdown=conclusion,
        messages=messages,
        truncated=truncated,
    )


def parse_session_file(path: Path, *, max_bytes: int) -> tuple[ParsedSession | None, bytes]:
    """读取并解析单个会话文件，返回 (解析结果, 解压后的正文)。

    正文一并返回是为了让调用方算 ``content_sha256``：文件名不变但内容被追加时，
    只有哈希能判断「要不要重写这一行」。
    """
    raw = path.read_bytes()
    # .zstd 是多帧拼接流；未压缩的 .jsonl（旧版本/调试产物）直接截断读取
    decoded = decode_zstd_frames(raw, max_bytes=max_bytes) if path.name.endswith(".zstd") else raw[:max_bytes]
    try:
        text_body = decoded.decode("utf-8")
    except UnicodeDecodeError:
        text_body = decoded.decode("utf-8", errors="replace")
    return parse_session_text(text_body), decoded


_UPSERT_SQL = text(
    """
    INSERT INTO dsh_diagnosis_session (
        session_id, case_id, title, workspace, agent_preset,
        started_at, last_activity_at, message_count, tool_call_count,
        conclusion_markdown, messages, truncated, binding_source,
        source_path, source_size, source_mtime, content_sha256, trace_id, ingested_at
    ) VALUES (
        :session_id, :case_id, :title, :workspace, :agent_preset,
        :started_at, :last_activity_at, :message_count, :tool_call_count,
        :conclusion_markdown, CAST(:messages AS jsonb), :truncated, :binding_source,
        :source_path, :source_size, :source_mtime, :content_sha256, :trace_id, :ingested_at
    )
    ON CONFLICT (session_id) DO UPDATE SET
        case_id = EXCLUDED.case_id,
        title = EXCLUDED.title,
        workspace = EXCLUDED.workspace,
        agent_preset = EXCLUDED.agent_preset,
        started_at = EXCLUDED.started_at,
        last_activity_at = EXCLUDED.last_activity_at,
        message_count = EXCLUDED.message_count,
        tool_call_count = EXCLUDED.tool_call_count,
        conclusion_markdown = EXCLUDED.conclusion_markdown,
        messages = EXCLUDED.messages,
        truncated = EXCLUDED.truncated,
        binding_source = EXCLUDED.binding_source,
        source_path = EXCLUDED.source_path,
        source_size = EXCLUDED.source_size,
        source_mtime = EXCLUDED.source_mtime,
        content_sha256 = EXCLUDED.content_sha256,
        trace_id = EXCLUDED.trace_id,
        ingested_at = EXCLUDED.ingested_at,
        updated_at = CURRENT_TIMESTAMP
    """
)

_LIST_BY_CASE_SQL = text(
    """
    SELECT session_id, case_id, title, workspace, agent_preset,
           started_at, last_activity_at, message_count, tool_call_count,
           conclusion_markdown, messages, truncated, binding_source,
           source_path, source_size, source_mtime, ingested_at
    FROM dsh_diagnosis_session
    WHERE case_id = :case_id
    ORDER BY COALESCE(started_at, ingested_at) DESC
    LIMIT :limit
    """
)


class DshDiagnosisService:
    """会话归档的读写入口。"""

    def __init__(self, session: AsyncSession, *, root: str, max_files: int, max_bytes: int):
        self.session = session
        self.root = Path(root)
        self.max_files = max_files
        self.max_bytes = max_bytes

    async def ingest(self, *, force: bool = False) -> IngestStats:
        """增量扫描会话目录并入库。

        判重只看 (size, mtime)：会话文件只追加、不改写，两者不变就不会有新内容。
        文件被追加后 size 必变，因而不会被漏掉。
        """
        stats = IngestStats()
        if not self.root.is_dir():
            stats.root_missing = True
            logger.debug(event="dsh_sessions_dir_missing", message=str(self.root))
            return stats

        candidates = sorted(
            self.root.rglob("session*.jsonl*"),
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        )[: self.max_files]
        if not candidates:
            return stats

        stats.scanned = len(candidates)
        known = await self._load_known([str(p) for p in candidates])
        trace_id = get_current_trace_id()

        async with _ingest_lock():
            for path in candidates:
                try:
                    stat = path.stat()
                except OSError:
                    stats.failed += 1
                    continue
                previous = known.get(str(path))
                if (
                    not force
                    and previous
                    and previous["source_size"] == stat.st_size
                    and previous["source_mtime"] is not None
                    and int(previous["source_mtime"].timestamp()) == int(stat.st_mtime)
                ):
                    stats.skipped += 1
                    continue
                try:
                    parsed, decoded = parse_session_file(path, max_bytes=self.max_bytes)
                except (OSError, ValueError) as exc:
                    stats.failed += 1
                    logger.warning(
                        event="dsh_session_parse_failed",
                        message=str(exc),
                        path=str(path),
                    )
                    continue
                if parsed is None:
                    stats.failed += 1
                    continue
                digest = hashlib.sha256(decoded).hexdigest()
                if not force and previous and previous["content_sha256"] == digest:
                    # 内容没变（例如仅 mtime 被 touch）：只刷新判重字段，不做整行重写
                    await self._touch(str(path), stat.st_size, stat.st_mtime, digest)
                    stats.skipped += 1
                    continue
                # 每条会话一个 SAVEPOINT：任一条数据被数据库拒绝时只丢这条，
                # 不能把同批其它会话一起回滚。2026-11-02/03 两次 500 都是
                # 「一条会话有问题 → 整批入库失败 → 页面显示没有诊断记录」。
                try:
                    async with self.session.begin_nested():
                        await self.session.execute(
                            _UPSERT_SQL,
                            {
                                "session_id": parsed.session_id,
                                "case_id": parsed.case_id,
                                "title": parsed.title,
                                "workspace": parsed.workspace,
                                "agent_preset": parsed.agent_preset,
                                "started_at": parsed.started_at,
                                "last_activity_at": parsed.last_activity_at,
                                "message_count": parsed.message_count,
                                "tool_call_count": parsed.tool_call_count,
                                "conclusion_markdown": parsed.conclusion_markdown,
                                # 只清洗结构中的字符串再序列化（见 _sanitize 文档：
                                # 对序列化结果做替换会打断转义、生成非法 JSON）
                                "messages": json.dumps(_scrub(parsed.messages), ensure_ascii=False),
                                "truncated": parsed.truncated,
                                "binding_source": parsed.binding_source,
                                "source_path": str(path),
                                "source_size": stat.st_size,
                                "source_mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                                "content_sha256": digest,
                                "trace_id": trace_id,
                                "ingested_at": datetime.now(UTC),
                            },
                        )
                except SQLAlchemyError as exc:
                    stats.failed += 1
                    logger.warning(
                        event="dsh_session_store_failed",
                        message=str(exc)[:500],
                        path=str(path),
                        session_id=parsed.session_id,
                    )
                    continue
                stats.parsed += 1
                if parsed.case_id is None:
                    stats.unbounded += 1

        await self.session.flush()
        return stats

    async def _load_known(self, paths: list[str]) -> dict[str, dict[str, Any]]:
        if not paths:
            return {}
        rows = (
            (
                await self.session.execute(
                    text(
                        "SELECT source_path, source_size, source_mtime, content_sha256 "
                        "FROM dsh_diagnosis_session WHERE source_path = ANY(:paths)"
                    ),
                    {"paths": paths},
                )
            )
            .mappings()
            .all()
        )
        return {str(row["source_path"]): dict(row) for row in rows}

    async def _touch(self, path: str, size: int, mtime: float, digest: str) -> None:
        await self.session.execute(
            text(
                "UPDATE dsh_diagnosis_session "
                "SET source_size = :size, source_mtime = :mtime, content_sha256 = :digest, "
                "    ingested_at = :now, updated_at = CURRENT_TIMESTAMP "
                "WHERE source_path = :path"
            ),
            {
                "path": path,
                "size": size,
                "mtime": datetime.fromtimestamp(mtime, tz=UTC),
                "digest": digest,
                "now": datetime.now(UTC),
            },
        )

    async def list_by_case(self, case_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = (await self.session.execute(_LIST_BY_CASE_SQL, {"case_id": case_id, "limit": limit})).mappings().all()
        return [dict(row) for row in rows]
