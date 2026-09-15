"""DiagnosticItemRepository 的幂等重试语义。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.repositories.diagnostic_item_repository import DiagnosticItemRepository
from sqlalchemy.dialects import postgresql


@pytest.mark.asyncio
async def test_create_upserts_duplicate_conversation_type_and_sequence():
    existing = MagicMock()
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: existing))
    repository = DiagnosticItemRepository(session)

    item = await repository.create(
        conversation_id=uuid4(),
        stage="S3",
        type="verification_step",
        seq=4,
        content={"signal_id": "sig_004"},
        status="rejected",
        trace_id="trace",
    )

    assert item is existing
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql and "DO UPDATE" in sql
    assert "updated_at" in sql
    session.add.assert_not_called()
