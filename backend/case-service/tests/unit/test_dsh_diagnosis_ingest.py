"""DSH 诊断会话归档解析单测。

固化三条曾差点写错的判断，任何一条回退都会让工单详情显示错误的「诊断记录」：

1. **多帧解码**：DSH 的 session.v4.jsonl.zstd 是逐批追加的多帧流，只用一次性
   decompress 接口只能拿到第一帧（实测 193 字节），会得出「会话是空的」结论；
2. **工单号唯一绑定**：会话正文里会列出日志根目录下的其他工单号，
   按「出现次数最多」猜必然错挂；只有 WeCom 锚点 / cwd / 唯一命中才允许绑定；
3. **结论取最后一条助手消息**：DSH 的报告是最后一条 assistant 消息，
   取「最长的一条」会把中间过程当成结论。
"""

from __future__ import annotations

import json

import pytest
import zstandard
from app.config import settings
from app.main import app
from app.routes import case_insights
from app.services.dsh_diagnosis_service import (
    decode_zstd_frames,
    parse_session_text,
    resolve_binding,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

pytestmark = pytest.mark.unit


def _frame(payload: dict) -> bytes:
    return zstandard.ZstdCompressor().compress((json.dumps(payload, ensure_ascii=False) + "\n").encode())


def _session_lines(
    *,
    session_id: str = "session-test-001",
    title: str | None = None,
    cwd: str = "/data/tickets",
    user_texts: list[str] | None = None,
    assistant_texts: list[str] | None = None,
) -> str:
    lines = [
        json.dumps(
            {
                "type": "session",
                "version": 4,
                "id": session_id,
                "createdAt": 1_787_472_000_000,
                "cwd": cwd,
                "agentPreset": "standard",
            }
        )
    ]
    if title:
        lines.append(json.dumps({"type": "session/title", "seq": 1, "data": {"title": title}}))
    for index, text in enumerate(user_texts or []):
        lines.append(
            json.dumps(
                {
                    "type": "user/message",
                    "seq": 10 + index,
                    "time": 1_787_472_001_000 + index,
                    "data": {"content": [{"type": "text", "text": text}], "role": "user"},
                }
            )
        )
    for index, text in enumerate(assistant_texts or []):
        lines.append(
            json.dumps(
                {
                    "type": "assistant/message",
                    "seq": 20 + index,
                    "time": 1_787_472_002_000 + index,
                    "data": {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "reasoning", "text": "内部思考不应进入正文"},
                                {"type": "text", "text": text},
                            ],
                        }
                    },
                }
            )
        )
    return "\n".join(lines) + "\n"


def test_decode_reads_all_frames_not_just_first():
    """多帧拼接必须全部解出：只解第一帧是 2026-11 现场差点采信的假结论。"""
    raw = _frame({"type": "session", "id": "s1"}) + _frame({"type": "user/message", "seq": 1})
    decoded = decode_zstd_frames(raw, max_bytes=1 << 20).decode("utf-8")

    assert '"id": "s1"' in decoded
    assert "user/message" in decoded


def test_decode_tolerates_truncated_tail_frame():
    """会话正在写入时最后一帧可能不完整：必须返回已解出的部分而不是抛错。"""
    raw = _frame({"type": "session", "id": "s2"}) + _frame({"type": "user/message"})[:20]
    decoded = decode_zstd_frames(raw, max_bytes=1 << 20).decode("utf-8")

    assert '"id": "s2"' in decoded


def test_parse_builds_transcript_and_uses_last_assistant_as_conclusion():
    text = _session_lines(
        title="虚拟机快照删除失败排查",
        user_texts=["[WeCom log task directory: \"Q2026103000409\"; 看看快照"],
        assistant_texts=["先列目录。", "## 一、根本原因\n\n存储池空间不足。"],
    )
    parsed = parse_session_text(text)

    assert parsed is not None
    assert parsed.session_id == "session-test-001"
    assert parsed.case_id == "Q2026103000409"
    assert parsed.binding_source == "wecom_marker"
    assert parsed.message_count == 3  # 1 user + 2 assistant
    # 结论是最后一条助手消息，不是最长的那条
    assert parsed.conclusion_markdown is not None
    assert "根本原因" in parsed.conclusion_markdown
    # reasoning block 不进正文
    assert all("内部思考" not in str(item.get("text")) for item in parsed.messages)


def test_binding_marker_beats_directory_listing_noise():
    """正文里出现别的工单号（目录列举）不能影响锚点判定。"""
    case_id, source = resolve_binding(
        workspace="/data/tickets",
        title="Q2026102600514 的历史对比",
        first_user_text='[WeCom log task directory: "Q2026103000409"]\n参考 Q2026102600514、Q2026102700214',
    )

    assert (case_id, source) == ("Q2026103000409", "wecom_marker")


def test_binding_uses_cwd_when_marker_absent():
    case_id, source = resolve_binding(
        workspace="/data/tickets/Q2026103000154",
        title=None,
        first_user_text="帮我看看日志",
    )

    assert (case_id, source) == ("Q2026103000154", "cwd")


def test_binding_refuses_ambiguous_first_message():
    """首条消息出现两个工单号 = 多义，必须留空，绝不猜。"""
    case_id, source = resolve_binding(
        workspace="/data/tickets",
        title=None,
        first_user_text="对比 Q2026103000154 和 Q2026103000298 的区别",
    )

    assert case_id is None
    assert source == "none"


def test_binding_falls_back_to_title_and_then_none():
    assert resolve_binding(workspace="/data/tickets", title="Q2026103000027 掉线", first_user_text="hi") == (
        "Q2026103000027",
        "title",
    )
    assert resolve_binding(workspace="/data/tickets", title="随便聊聊", first_user_text="hi") == (None, "none")


def test_parse_truncates_long_tool_output_but_keeps_conclusion():
    long_output = "x" * 5000
    lines = [
        json.dumps({"type": "session", "version": 4, "id": "session-tool", "cwd": "/data/tickets"}),
        json.dumps({"type": "tool/call", "seq": 1, "data": {"callId": "c1", "name": "bash", "arguments": "{}"}}),
        json.dumps(
            {
                "type": "tool/result",
                "seq": 2,
                "data": {"message": {"toolCallId": "c1", "content": [{"type": "text", "text": long_output}]}},
            }
        ),
        json.dumps(
            {
                "type": "assistant/message",
                "seq": 3,
                "data": {"message": {"role": "assistant", "content": [{"type": "text", "text": "结论：无异常"}]}},
            }
        ),
    ]
    parsed = parse_session_text("\n".join(lines))

    assert parsed is not None
    tool_rows = [item for item in parsed.messages if item["role"] == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["name"] == "bash"
    assert len(tool_rows[0]["text"]) <= 800 + len("…（已截断）")
    assert parsed.conclusion_markdown == "结论：无异常"


def test_parse_returns_none_without_session_header():
    assert parse_session_text('{"type":"user/message","data":{"content":[{"type":"text","text":"hi"}]}}') is None


@pytest.mark.asyncio
async def test_dsh_sessions_endpoint_requires_admin_token():
    """管理端点必须验管理员凭证：DSH 会话含完整客户日志内容，不能匿名读。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/cases/admin/Q2026103000409/dsh-sessions")

    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_log_collect_evidence_endpoint_requires_admin_token():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/cases/admin/Q2026103000409/log-collect")

    assert resp.status_code in (401, 403)
    assert settings.INTERNAL_API_TOKEN  # 防止测试环境未加载配置时静默通过


@pytest.mark.asyncio
async def test_get_session_prefers_app_state_manager():
    """数据库必须优先从 app.state 取。

    2026-11-02 staging 现场：lifespan 漏调 `case_insights.set_database_manager()`，
    两个管理端点恒返 500「Database not initialized」，而启动日志与健康检查全绿。
    这条用例锁住 app.state 兜底，避免同类漏接再次静默。
    """
    class _Manager:
        def __init__(self) -> None:
            self.called = False

        async def get_session(self):
            self.called = True
            yield "sentinel-session"

    manager = _Manager()
    fake_app = FastAPI()
    fake_app.state.database_manager = manager
    request = Request({"type": "http", "app": fake_app, "headers": []})

    generator = case_insights.get_session(request)
    session = await generator.__anext__()

    assert session == "sentinel-session"
    assert manager.called
    await generator.aclose()
