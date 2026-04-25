"""
SSH 终端会话管理服务
Task 37: SSH 代理与终端交互后端能力
Task 42: 终端操作录制功能

功能：
- 创建/关闭 SSH 会话
- 管理 SSH 连接池
- 执行命令并流式返回输出
- 会话空闲自动回收
- 终端操作记录写入和查询
"""

import asyncio
import contextlib
import hashlib
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncssh
from fastapi import WebSocket
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from shared.database.postgres import DatabaseManager
from shared.database.redis import RedisManager
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

from app.config import settings

from ..models.terminal import (
    AuthType,
    OperationDirection,
    TerminalOperationResponse,
    TerminalSessionInfo,
    TerminalSessionStatus,
    TerminalStatusMessage,
    TerminalWSMessage,
    WSMessageType,
)

logger = get_logger("terminal-service")

# 会话配置
SESSION_PREFIX = "terminal:session:"
SESSION_INDEX_KEY = "terminal:sessions"
SESSION_EXPIRE_SECONDS = 3600  # 1 小时过期
SESSION_IDLE_TIMEOUT = 1800  # 30 分钟无操作超时


def mask_host(host: str) -> str:
    """
    脱敏主机地址
    192.168.1.100 -> 192.168.*.*
    """
    parts = host.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"

    # 处理域名或 IPv6
    if len(host) > 8:
        return host[:4] + "*" * 4 + host[-4:]

    return "***"


def hash_host(host: str) -> str:
    """计算主机地址哈希（用于审计日志）"""
    return hashlib.sha256(host.encode()).hexdigest()[:16]


def parse_iso_datetime(value: str) -> datetime:
    """将 ISO 字符串解析为 UTC 时间"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SSHConnectionManager:
    """SSH 连接管理器"""

    def __init__(self):
        # session_id -> asyncssh.SSHClientConnection
        self._connections: dict[str, asyncssh.SSHClientConnection] = {}
        # session_id -> asyncio.Task（命令执行任务）
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        # session_id -> WebSocket
        self._websockets: dict[str, WebSocket] = {}

    async def add_connection(self, session_id: str, conn: asyncssh.SSHClientConnection):
        """添加 SSH 连接"""
        self._connections[session_id] = conn
        logger.info(event="ssh_connection_added", message="SSH 连接已添加", session_id=session_id)

    async def get_connection(self, session_id: str) -> asyncssh.SSHClientConnection | None:
        """获取 SSH 连接"""
        return self._connections.get(session_id)

    async def remove_connection(self, session_id: str):
        """移除 SSH 连接并清理关联资源"""
        if session_id in self._connections:
            conn = self._connections.pop(session_id)
            conn.close()
            logger.info(event="ssh_connection_removed", message="SSH 连接已移除", session_id=session_id)

        await self.cancel_task(session_id)
        await self.remove_websocket(session_id)

    async def add_websocket(self, session_id: str, ws: WebSocket):
        """绑定 WebSocket"""
        self._websockets[session_id] = ws

    async def remove_websocket(self, session_id: str):
        """解绑 WebSocket"""
        if session_id in self._websockets:
            del self._websockets[session_id]

    def get_websocket(self, session_id: str) -> WebSocket | None:
        """获取 WebSocket"""
        return self._websockets.get(session_id)

    async def add_task(self, session_id: str, task: asyncio.Task[Any]):
        """添加命令执行任务"""
        existing_task = self._tasks.get(session_id)
        if existing_task and not existing_task.done():
            raise RuntimeError("已有命令正在执行，请稍后重试")
        self._tasks[session_id] = task

    async def clear_task(self, session_id: str):
        """清理任务引用"""
        task = self._tasks.get(session_id)
        if task and task.done():
            self._tasks.pop(session_id, None)

    async def cancel_task(self, session_id: str):
        """取消命令执行任务"""
        task = self._tasks.pop(session_id, None)
        if not task:
            return

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TerminalService:
    """终端服务"""

    def __init__(self, redis_manager: RedisManager, db_manager: DatabaseManager | None = None):
        self.redis = redis_manager
        self.db = db_manager
        self.ssh_manager = SSHConnectionManager()
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._cleanup_stop_event = asyncio.Event()

    async def start(self):
        """启动后台清理任务"""
        if self._cleanup_task and not self._cleanup_task.done():
            return

        self._cleanup_stop_event.clear()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def shutdown(self):
        """停止后台任务并清理活动连接"""
        self._cleanup_stop_event.set()

        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None

        session_ids = await self._list_indexed_sessions()
        for session_id in session_ids:
            await self.close_session(session_id, reason="service_shutdown")

    async def create_session(
        self,
        host: str,
        port: int,
        username: str,
        auth_type: AuthType,
        password: str | None = None,
        private_key: str | None = None,
        passphrase: str | None = None,
        client_id: str | None = None,
        case_id: str | None = None,
    ) -> tuple[str, TerminalSessionInfo]:
        """
        创建 SSH 会话

        Returns:
            tuple[str, TerminalSessionInfo]: (session_id, 会话信息)
        """
        session_id = str(uuid.uuid4())
        trace_id = get_current_trace_id()

        # 脱敏存储
        masked_host = mask_host(host)
        host_hash = hash_host(host)

        logger.info(
            event="terminal_session_creating",
            message="正在创建终端会话",
            session_id=session_id,
            host=masked_host,
            port=port,
            username=username,
            client_id=client_id,
            case_id=case_id,
            host_hash=host_hash,
            trace_id=trace_id,
        )

        try:
            # 建立 SSH 连接
            conn = await self._connect_ssh(
                host=host,
                port=port,
                username=username,
                auth_type=auth_type,
                password=password,
                private_key=private_key,
                passphrase=passphrase,
            )

            # 存储连接
            await self.ssh_manager.add_connection(session_id, conn)

            # 创建会话信息
            now_iso = datetime.now(UTC).isoformat()
            session_info = TerminalSessionInfo(
                session_id=session_id,
                host=masked_host,
                port=port,
                username=username,
                client_id=client_id,
                case_id=case_id,
                status=TerminalSessionStatus.CONNECTED,
                created_at=now_iso,
                last_activity_at=now_iso,
                trace_id=trace_id,
            )

            # 存储到 Redis
            await self._save_session(session_id, session_info)

            logger.info(
                event="terminal_session_created",
                message="终端会话创建成功",
                session_id=session_id,
                host=masked_host,
                username=username,
                trace_id=trace_id,
            )

            return session_id, session_info

        except asyncssh.PermissionDenied as e:
            logger.error(
                event="terminal_session_auth_failed",
                message="SSH 认证失败",
                session_id=session_id,
                host=masked_host,
                username=username,
                error=str(e),
                trace_id=trace_id,
            )
            raise PermissionError("SSH 认证失败") from e
        except (asyncssh.ConnectionLost, asyncssh.DisconnectError) as e:
            logger.error(
                event="terminal_session_connect_failed",
                message=f"SSH 连接失败: {e}",
                session_id=session_id,
                host=masked_host,
                error=str(e),
                trace_id=trace_id,
            )
            raise ConnectionError(f"SSH 连接被拒绝: {e}") from e
        except ValueError:
            raise
        except Exception as e:
            logger.error(
                event="terminal_session_error",
                message=f"创建终端会话失败: {e}",
                session_id=session_id,
                host=masked_host,
                error=str(e),
                trace_id=trace_id,
            )
            raise

    async def _connect_ssh(
        self,
        host: str,
        port: int,
        username: str,
        auth_type: AuthType,
        password: str | None = None,
        private_key: str | None = None,
        passphrase: str | None = None,
    ) -> asyncssh.SSHClientConnection:
        """建立 SSH 连接"""
        if settings.TERMINAL_ALLOW_INSECURE_HOSTS:
            known_hosts: str | None = None
            logger.warning(
                event="terminal_insecure_known_hosts",
                message="已禁用 SSH 主机指纹校验，请仅用于开发环境",
            )
        else:
            known_hosts = os.path.expanduser(settings.TERMINAL_KNOWN_HOSTS_FILE)

        connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "username": username,
            "known_hosts": known_hosts,
        }

        if auth_type == AuthType.PASSWORD:
            if not password:
                raise ValueError("密码认证方式需要提供 password 字段")
            connect_kwargs["password"] = password
        else:
            if not private_key:
                raise ValueError("密钥认证方式需要提供 private_key 字段")
            try:
                key_obj = asyncssh.import_private_key(private_key, passphrase=passphrase or None)
            except Exception as e:  # pragma: no cover - 异常分支依赖密钥格式
                raise ValueError("私钥解析失败") from e
            connect_kwargs["client_keys"] = [key_obj]

        return await asyncssh.connect(**connect_kwargs)

    async def _save_session(self, session_id: str, session_info: TerminalSessionInfo):
        """保存会话到 Redis"""
        key = f"{SESSION_PREFIX}{session_id}"
        await self.redis.set(
            key,
            session_info.model_dump_json(),
            ex=SESSION_EXPIRE_SECONDS,
        )

        if self.redis.client:
            await self.redis.client.sadd(SESSION_INDEX_KEY, session_id)

    async def _remove_session_record(self, session_id: str):
        """删除 Redis 中的会话记录和索引"""
        key = f"{SESSION_PREFIX}{session_id}"
        await self.redis.delete(key)

        if self.redis.client:
            await self.redis.client.srem(SESSION_INDEX_KEY, session_id)

    async def _list_indexed_sessions(self) -> list[str]:
        """获取索引中的全部会话 ID"""
        if not self.redis.client:
            return []

        session_ids = await self.redis.client.smembers(SESSION_INDEX_KEY)
        return list(session_ids or [])

    async def get_session(self, session_id: str) -> TerminalSessionInfo | None:
        """获取会话信息"""
        key = f"{SESSION_PREFIX}{session_id}"
        data = await self.redis.get(key)
        if not data:
            return None

        return TerminalSessionInfo.model_validate_json(data)

    async def validate_session_owner(self, session_id: str, client_id: str) -> TerminalSessionInfo | None:
        """校验会话归属"""
        session_info = await self.get_session(session_id)
        if not session_info:
            return None

        if not session_info.client_id or session_info.client_id != client_id:
            return None

        return session_info

    async def touch_session(self, session_id: str):
        """刷新会话活动时间"""
        session_info = await self.get_session(session_id)
        if not session_info:
            return

        session_info.last_activity_at = datetime.now(UTC).isoformat()
        await self._save_session(session_id, session_info)

    async def close_session(self, session_id: str, reason: str = "manual") -> bool:
        """
        关闭终端会话

        Returns:
            bool: 是否成功关闭
        """
        trace_id = get_current_trace_id()
        session_info = await self.get_session(session_id)

        if not session_info:
            logger.warning(
                event="terminal_session_not_found",
                message="会话不存在",
                session_id=session_id,
                trace_id=trace_id,
            )
            return False

        ws = self.ssh_manager.get_websocket(session_id)
        if ws:
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="session_closed")

        await self.ssh_manager.remove_connection(session_id)
        await self._remove_session_record(session_id)

        logger.info(
            event="terminal_session_closed",
            message="终端会话已关闭",
            session_id=session_id,
            host=session_info.host,
            reason=reason,
            trace_id=trace_id,
        )

        return True

    async def execute_command(
        self,
        session_id: str,
        command: str,
        websocket: WebSocket,
    ):
        """
        执行命令并通过 WebSocket 流式返回输出

        Args:
            session_id: 会话 ID
            command: 要执行的命令
            websocket: WebSocket 连接
        """
        trace_id = get_current_trace_id()

        # 获取 SSH 连接
        conn = await self.ssh_manager.get_connection(session_id)
        if not conn:
            await self._send_error(websocket, "会话不存在或已断开", trace_id)
            return

        await self.touch_session(session_id)

        logger.info(
            event="terminal_command_start",
            message="开始执行命令",
            session_id=session_id,
            command=command[:100],
            trace_id=trace_id,
        )

        try:
            # 创建进程
            proc = await conn.create_process(
                command,
                stdout=asyncssh.PIPE,
                stderr=asyncssh.PIPE,
            )

            await self._send_status(websocket, TerminalSessionStatus.CONNECTED, "命令执行中", trace_id)

            # 并行读取 stdout / stderr
            stdout_task = asyncio.create_task(self._read_stream(proc.stdout, websocket, WSMessageType.STDOUT, trace_id))
            stderr_task = asyncio.create_task(self._read_stream(proc.stderr, websocket, WSMessageType.STDERR, trace_id))

            await proc.wait()
            await asyncio.gather(stdout_task, stderr_task)

            exit_status = proc.exit_status or 0
            await self._send_status(
                websocket,
                TerminalSessionStatus.CONNECTED,
                f"命令执行完成，退出码: {exit_status}",
                trace_id,
            )

            logger.info(
                event="terminal_command_complete",
                message="命令执行完成",
                session_id=session_id,
                exit_status=exit_status,
                trace_id=trace_id,
            )

        except asyncssh.ChannelOpenError as e:
            logger.error(
                event="terminal_command_channel_error",
                message=f"SSH 通道错误: {e}",
                session_id=session_id,
                error=str(e),
                trace_id=trace_id,
            )
            await self._send_error(websocket, f"SSH 通道错误: {e}", trace_id)
        except asyncio.CancelledError:
            logger.info(
                event="terminal_command_cancelled",
                message="命令执行被取消",
                session_id=session_id,
                trace_id=trace_id,
            )
            raise
        except Exception as e:
            logger.error(
                event="terminal_command_error",
                message=f"命令执行错误: {e}",
                session_id=session_id,
                error=str(e),
                trace_id=trace_id,
            )
            await self._send_error(websocket, f"命令执行错误: {e}", trace_id)
        finally:
            await self.touch_session(session_id)

    async def _read_stream(
        self,
        stream: asyncssh.SSHReader,
        websocket: WebSocket,
        msg_type: WSMessageType,
        trace_id: str | None = None,
    ):
        """读取输出流并发送到 WebSocket"""
        try:
            while True:
                data = await stream.read(1024)
                if not data:
                    break

                message = TerminalWSMessage(
                    type=msg_type,
                    data=data,
                )
                await websocket.send_text(message.model_dump_json())

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                event="terminal_stream_read_error",
                message=f"读取输出流错误: {e}",
                error=str(e),
                trace_id=trace_id,
            )

    async def _send_status(
        self,
        websocket: WebSocket,
        state: TerminalSessionStatus,
        message: str,
        trace_id: str | None = None,
    ):
        """发送状态消息"""
        msg = TerminalStatusMessage(
            state=state,
            message=message,
        )
        await websocket.send_text(msg.model_dump_json())

    async def _send_error(
        self,
        websocket: WebSocket,
        message: str,
        trace_id: str | None = None,
    ):
        """发送错误消息"""
        msg = TerminalWSMessage(
            type=WSMessageType.ERROR,
            data=message,
        )
        await websocket.send_text(msg.model_dump_json())

    async def handle_websocket_message(
        self,
        session_id: str,
        message: TerminalWSMessage,
        websocket: WebSocket,
    ):
        """
        处理 WebSocket 消息

        Args:
            session_id: 会话 ID
            message: WebSocket 消息
            websocket: WebSocket 连接
        """
        trace_id = get_current_trace_id()

        if message.type == WSMessageType.STDIN:
            if not message.data:
                return

            command_task = asyncio.create_task(self.execute_command(session_id, message.data, websocket))
            try:
                await self.ssh_manager.add_task(session_id, command_task)
            except RuntimeError as e:
                command_task.cancel()
                await self._send_error(websocket, str(e), trace_id)
                return

            try:
                await command_task
            finally:
                await self.ssh_manager.clear_task(session_id)

        elif message.type == WSMessageType.PING:
            pong = TerminalWSMessage(type=WSMessageType.PONG)
            await websocket.send_text(pong.model_dump_json())
            await self.touch_session(session_id)

        elif message.type == WSMessageType.RESIZE:
            logger.debug(
                event="terminal_resize",
                message="终端大小调整",
                session_id=session_id,
                cols=message.cols,
                rows=message.rows,
                trace_id=trace_id,
            )
            await self.touch_session(session_id)

    async def _cleanup_loop(self):
        """空闲会话清理循环"""
        logger.info(event="terminal_cleanup_start", message="终端空闲会话清理任务已启动")

        while not self._cleanup_stop_event.is_set():
            try:
                await self._cleanup_idle_sessions()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    event="terminal_cleanup_error",
                    message=f"会话清理异常: {e}",
                    error=str(e),
                )

            try:
                await asyncio.wait_for(
                    self._cleanup_stop_event.wait(),
                    timeout=settings.TERMINAL_CLEANUP_INTERVAL_SECONDS,
                )
            except TimeoutError:
                continue

        logger.info(event="terminal_cleanup_stop", message="终端空闲会话清理任务已停止")

    async def _cleanup_idle_sessions(self):
        """清理空闲会话"""
        now = datetime.now(UTC)

        for session_id in await self._list_indexed_sessions():
            session_info = await self.get_session(session_id)
            if not session_info:
                await self._remove_session_record(session_id)
                continue

            try:
                last_activity = parse_iso_datetime(session_info.last_activity_at or session_info.created_at)
            except ValueError:
                last_activity = now

            idle_seconds = (now - last_activity).total_seconds()
            if idle_seconds <= SESSION_IDLE_TIMEOUT:
                continue

            logger.info(
                event="terminal_idle_session_cleanup",
                message="会话超过空闲阈值，自动关闭",
                session_id=session_id,
                idle_seconds=int(idle_seconds),
            )
            await self.close_session(session_id, reason="idle_timeout")

    # ============================================================
    # 终端操作录制方法 (Task 42)
    # ============================================================

    async def create_operation(
        self,
        case_id: str,
        seq_number: int,
        direction: OperationDirection,
        content: str,
        conversation_id: str | None = None,
        session_id: str | None = None,
        command: str | None = None,
        content_clean: str | None = None,
        exit_code: int | None = None,
        diagnostic_stage: str | None = None,
    ) -> TerminalOperationResponse:
        """
        创建终端操作记录

        Args:
            case_id: 工单 ID
            seq_number: 操作序号
            direction: 操作方向（input/output）
            content: 原始内容（含 ANSI 码）
            conversation_id: 会话 ID（可选）
            session_id: SSH session ID（可选）
            command: 命令文本（仅 input 类型）
            content_clean: 纯文本内容（剔除 ANSI）
            exit_code: 退出码（仅 output 类型）
            diagnostic_stage: 诊断阶段（可选）

        Returns:
            TerminalOperationResponse: 创建的记录响应
        """
        trace_id = get_current_trace_id()

        logger.info(
            event="terminal_operation_create",
            message="创建终端操作记录",
            case_id=case_id,
            seq_number=seq_number,
            direction=direction.value,
            trace_id=trace_id,
        )

        async with self.db.async_session_factory() as session:
            # 插入记录
            result = await session.execute(
                text(
                    """
                    INSERT INTO terminal_operation (
                        case_id, conversation_id, session_id, seq_number, direction,
                        command, content, content_clean, exit_code, diagnostic_stage,
                        created_at, trace_id
                    ) VALUES (
                        :case_id, :conversation_id, :session_id, :seq_number, :direction,
                        :command, :content, :content_clean, :exit_code, :diagnostic_stage,
                        NOW(), :trace_id
                    ) RETURNING id, created_at
                    """
                ),
                {
                    "case_id": case_id,
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "seq_number": seq_number,
                    "direction": direction.value,
                    "command": command,
                    "content": content,
                    "content_clean": content_clean,
                    "exit_code": exit_code,
                    "diagnostic_stage": diagnostic_stage,
                    "trace_id": trace_id,
                },
            )
            row = result.fetchone()
            await session.commit()

            return TerminalOperationResponse(
                id=row.id,
                case_id=case_id,
                seq_number=seq_number,
                direction=direction,
                command=command,
                content=content,
                exit_code=exit_code,
                diagnostic_stage=diagnostic_stage,
                created_at=row.created_at.isoformat(),
            )

    async def list_operations(
        self,
        case_id: str,
        stage: str | None = None,
        search: str | None = None,
        direction: OperationDirection | None = None,
        order: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[int, list[TerminalOperationResponse]]:
        """
        查询终端操作记录

        Args:
            case_id: 工单 ID（必须）
            stage: 诊断阶段过滤（可选）
            search: 关键词搜索（可选）
            direction: 方向过滤（可选）
            order: 排序方向（asc/desc）
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            tuple[int, list]: (总数, 操作列表)
        """
        trace_id = get_current_trace_id()

        async with self.db.async_session_factory() as session:
            # 构建查询条件
            conditions = ["case_id = :case_id"]
            params: dict[str, Any] = {"case_id": case_id}

            if stage:
                conditions.append("diagnostic_stage = :stage")
                params["stage"] = stage

            if direction:
                conditions.append("direction = :direction")
                params["direction"] = direction.value

            if search:
                # 使用全文搜索索引
                conditions.append("to_tsvector('simple', content_clean) @@ to_tsquery('simple', :search)")
                params["search"] = search

            where_clause = " AND ".join(conditions)
            order_direction = "ASC" if order == "asc" else "DESC"

            # 查询总数
            count_result = await session.execute(
                text(f"SELECT COUNT(*) as total FROM terminal_operation WHERE {where_clause}"),
                params,
            )
            total = count_result.fetchone().total

            # 查询列表
            list_result = await session.execute(
                text(
                    f"""
                    SELECT id, case_id, seq_number, direction, command, content,
                           exit_code, diagnostic_stage, created_at
                    FROM terminal_operation
                    WHERE {where_clause}
                    ORDER BY seq_number {order_direction}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": limit, "offset": offset},
            )
            rows = list_result.fetchall()

            operations = [
                TerminalOperationResponse(
                    id=row.id,
                    case_id=row.case_id,
                    seq_number=row.seq_number,
                    direction=OperationDirection(row.direction),
                    command=row.command,
                    content=row.content,
                    exit_code=row.exit_code,
                    diagnostic_stage=row.diagnostic_stage,
                    created_at=row.created_at.isoformat(),
                )
                for row in rows
            ]

            logger.info(
                event="terminal_operation_list",
                message="查询终端操作记录",
                case_id=case_id,
                total=total,
                returned=len(operations),
                trace_id=trace_id,
            )

            return total, operations

    async def get_latest_seq_number(self, case_id: str) -> int:
        """
        获取工单最新的操作序号

        用于前端刷新页面后恢复序号。

        Args:
            case_id: 工单 ID

        Returns:
            int: 最新序号（无记录返回 0）
        """
        async with self.db.async_session_factory() as session:
            result = await session.execute(
                text("SELECT MAX(seq_number) as max_seq FROM terminal_operation WHERE case_id = :case_id"),
                {"case_id": case_id},
            )
            row = result.fetchone()
            return row.max_seq or 0
