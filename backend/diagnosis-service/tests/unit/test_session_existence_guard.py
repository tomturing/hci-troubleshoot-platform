"""SRC-2026-5358 回归：诊断会话子资源必须校验会话存在性。"""

import uuid

import pytest
from app.auth import ActorContext
from app.errors import DiagnosisError
from app.services.evidence_upload_service import EvidenceUploadService
from app.services.offline_analysis_service import OfflineAnalysisService


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """只实现 _require_session_exists 用到的 execute 接口。"""

    def __init__(self, exists: bool):
        self.exists = exists
        self.calls = []

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _FakeResult([(1,)] if self.exists else [])


def _actor() -> ActorContext:
    return ActorContext(
        tenant_id="tenant-a",
        user_id="user-a",
        roles=frozenset({"customer"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory",
    [
        lambda s: EvidenceUploadService(session=s, storage=None),
        lambda s: OfflineAnalysisService(session=s),
    ],
)
async def test_session_exists_passes(factory):
    session_id = str(uuid.uuid4())
    service = factory(_FakeSession(exists=True))
    await service._require_session_exists(actor=_actor(), session_id=session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "factory",
    [
        lambda s: EvidenceUploadService(session=s, storage=None),
        lambda s: OfflineAnalysisService(session=s),
    ],
)
async def test_missing_session_raises_404(factory):
    session_id = str(uuid.uuid4())
    service = factory(_FakeSession(exists=False))
    with pytest.raises(DiagnosisError) as exc:
        await service._require_session_exists(actor=_actor(), session_id=session_id)
    assert exc.value.code == "DIAGNOSIS_SESSION_NOT_FOUND"
    assert exc.value.http_status == 404
