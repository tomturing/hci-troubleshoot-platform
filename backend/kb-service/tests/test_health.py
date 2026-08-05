"""kb-service 三级健康探针契约测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from app.routes import health


class _SessionContext:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def _request_with_session(session):
    manager = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database_manager=manager)))


@pytest.mark.asyncio
async def test_readiness_returns_200_only_when_database_responds():
    session = SimpleNamespace(execute=lambda _query: asyncio.sleep(0))

    response = await health.health_ready(_request_with_session(session))

    assert response.status_code == 200
    assert b'"database":"ok"' in response.body


@pytest.mark.asyncio
async def test_readiness_times_out_and_returns_503(monkeypatch):
    session = SimpleNamespace(execute=lambda _query: asyncio.sleep(0.1))
    monkeypatch.setattr(health, "_READINESS_DB_TIMEOUT_SECONDS", 0.01)

    response = await health.health_ready(_request_with_session(session))

    assert response.status_code == 503
    assert b'"database":"unavailable"' in response.body


@pytest.mark.asyncio
async def test_liveness_does_not_depend_on_database():
    assert await health.health_live() == {"status": "alive"}
