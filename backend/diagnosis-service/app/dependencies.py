"""离线诊断服务依赖注入。"""

from collections.abc import AsyncIterator

from fastapi import Request

from app.auth import InternalCaseAuthorizer
from app.config import settings
from app.errors import DiagnosisError
from app.repositories.collection_plan_repository import CollectionPlanRepository
from app.repositories.collector_artifact_repository import CollectorArtifactRepository
from app.repositories.diagnosis_session_repository import DiagnosisSessionRepository
from app.services.artifact_signer import Ed25519ArtifactSigner
from app.services.collection_plan_service import CollectionPlanService
from app.services.collection_profile_service import CollectionProfileService
from app.services.collector_artifact_service import CollectorArtifactService
from app.services.collector_definition_service import CollectorDefinitionService
from app.services.collector_trust_service import CollectorTrustService
from app.services.deletion_service import DiagnosisDeletionService
from app.services.diagnosis_management_service import DiagnosisManagementService
from app.services.diagnosis_session_service import DiagnosisSessionService
from app.services.envelope_encryption import EnvelopeEncryptionService
from app.services.evidence_upload_service import EvidenceUploadService
from app.services.object_storage import LocalObjectStorage
from app.services.offline_analysis_service import OfflineAnalysisService, OfflineEvidenceProvider
from app.services.offline_governance_service import OfflineGovernanceService
from app.services.offline_resource_sync_service import OfflineResourceSyncService


async def get_session_service(request: Request) -> AsyncIterator[DiagnosisSessionService]:
    """为请求创建事务级诊断会话服务。"""

    database_manager = getattr(request.app.state, "database_manager", None)
    if database_manager is None:
        raise DiagnosisError(
            code="DATABASE_UNAVAILABLE",
            message="诊断服务数据库尚未初始化",
            http_status=503,
            retryable=True,
        )

    async for session in database_manager.get_session():
        yield DiagnosisSessionService(
            DiagnosisSessionRepository(session),
            case_authorizer=InternalCaseAuthorizer(session),
            scenario_availability=CollectionProfileService(session),
        )


async def get_diagnosis_management_service(request: Request) -> AsyncIterator[DiagnosisManagementService]:
    """为请求创建离线诊断管理工作台服务。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield DiagnosisManagementService(session)


async def get_collection_profile_service(request: Request) -> AsyncIterator[CollectionProfileService]:
    """为请求创建事务级采集画像服务。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield CollectionProfileService(session)


async def get_collection_plan_service(request: Request) -> AsyncIterator[CollectionPlanService]:
    """为请求创建事务级采集计划服务。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield CollectionPlanService(
            session=session,
            session_repository=DiagnosisSessionRepository(session),
            plan_repository=CollectionPlanRepository(session),
        )


async def get_collector_definition_service(request: Request) -> AsyncIterator[CollectorDefinitionService]:
    """为请求创建事务级 Collector 管理服务。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield CollectorDefinitionService(session)


async def get_collector_artifact_service(request: Request) -> AsyncIterator[CollectorArtifactService]:
    """为请求创建事务级 Collector Artifact 服务。"""

    database_manager = _require_database_manager(request)
    signer = getattr(request.app.state, "artifact_signer", None)
    async for session in database_manager.get_session():
        yield CollectorArtifactService(
            session=session,
            session_repository=DiagnosisSessionRepository(session),
            plan_repository=CollectionPlanRepository(session),
            artifact_repository=CollectorArtifactRepository(session),
            signer=signer,
            envelope_encryption=getattr(request.app.state, "envelope_encryption", None),
        )


async def get_collector_trust_service(request: Request) -> AsyncIterator[CollectorTrustService]:
    """为请求创建 Collector 离线信任链服务。"""

    database_manager = _require_database_manager(request)
    signer = getattr(request.app.state, "artifact_signer", None)
    async for session in database_manager.get_session():
        yield CollectorTrustService(
            artifact_repository=CollectorArtifactRepository(session),
            session_repository=DiagnosisSessionRepository(session),
            signer=signer,
        )


async def get_evidence_upload_service(request: Request) -> AsyncIterator[EvidenceUploadService]:
    """为请求创建上传会话服务。"""

    database_manager = _require_database_manager(request)
    storage = getattr(request.app.state, "object_storage", None)
    if storage is None:
        raise DiagnosisError(
            code="OBJECT_STORAGE_UNAVAILABLE",
            message="诊断对象存储尚未初始化",
            http_status=503,
            retryable=True,
        )
    async for session in database_manager.get_session():
        yield EvidenceUploadService(session=session, storage=storage)


async def get_offline_analysis_service(request: Request) -> AsyncIterator[OfflineAnalysisService]:
    """为请求创建离线分析服务。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield OfflineAnalysisService(session)


async def get_offline_evidence_provider(request: Request) -> AsyncIterator[OfflineEvidenceProvider]:
    """为请求创建离线证据查询器。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield OfflineEvidenceProvider(session)


async def get_deletion_service(request: Request) -> AsyncIterator[DiagnosisDeletionService]:
    """为请求创建诊断数据删除服务。"""

    database_manager = _require_database_manager(request)
    storage = getattr(request.app.state, "object_storage", None)
    if storage is None:
        raise DiagnosisError(code="OBJECT_STORAGE_UNAVAILABLE", message="诊断对象存储尚未初始化", http_status=503)
    async for session in database_manager.get_session():
        yield DiagnosisDeletionService(session=session, storage=storage)


async def get_offline_governance_service(request: Request) -> AsyncIterator[OfflineGovernanceService]:
    """为请求创建离线诊断治理服务。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield OfflineGovernanceService(session)


async def get_offline_resource_sync_service(request: Request) -> AsyncIterator[OfflineResourceSyncService]:
    """为请求创建 KBD 离线采集资源同步服务。"""

    database_manager = _require_database_manager(request)
    async for session in database_manager.get_session():
        yield OfflineResourceSyncService(session)


def build_artifact_signer():
    """按配置构造签名器；缺失配置时保持默认拒绝。"""

    if not settings.COLLECTOR_SIGNING_PRIVATE_KEY_B64.strip() or not settings.COLLECTOR_SIGNING_KEY_ID.strip():
        return None
    return Ed25519ArtifactSigner(
        private_key_base64=settings.COLLECTOR_SIGNING_PRIVATE_KEY_B64,
        key_id=settings.COLLECTOR_SIGNING_KEY_ID,
    )


def build_object_storage() -> LocalObjectStorage:
    """构造对象存储适配器；当前 local 模式用于开发和私有化单机部署。"""

    if settings.DIAGNOSIS_OBJECT_STORAGE_MODE != "local":
        raise RuntimeError("未安装生产对象存储适配器，拒绝回退到 Web 进程文件转发")
    return LocalObjectStorage(settings.DIAGNOSIS_OBJECT_STORAGE_ROOT)


def build_envelope_encryption() -> EnvelopeEncryptionService | None:
    """按配置构造信封加密服务，缺失密钥时正式包处理默认拒绝。"""

    if not settings.DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64.strip() or not settings.DIAGNOSIS_ENCRYPTION_KEY_ID.strip():
        return None
    return EnvelopeEncryptionService(
        private_key_base64=settings.DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64,
        key_id=settings.DIAGNOSIS_ENCRYPTION_KEY_ID,
    )


def _require_database_manager(request: Request):
    """读取已初始化的数据库管理器。"""

    database_manager = getattr(request.app.state, "database_manager", None)
    if database_manager is None:
        raise DiagnosisError(
            code="DATABASE_UNAVAILABLE",
            message="诊断服务数据库尚未初始化",
            http_status=503,
            retryable=True,
        )
    return database_manager
