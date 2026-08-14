"""离线证据上传、查询、诊断、报告审核和删除 API。"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from app.auth import ActorContext, require_actor
from app.config import settings
from app.dependencies import (
    get_deletion_service,
    get_evidence_upload_service,
    get_offline_analysis_service,
    get_offline_evidence_provider,
    get_offline_governance_service,
)
from app.errors import DiagnosisError
from app.schemas.evidence_lifecycle import (
    DeletionJobResponse,
    DeletionRequest,
    DiagnosisCandidateResponse,
    DiagnosisReportResponse,
    DiagnosisRunResponse,
    DiagnosisStartRequest,
    DiagnosisTimelineEvent,
    EvidenceAssessmentResponse,
    EvidenceBundleResponse,
    EvidenceQueryRequest,
    EvidenceQueryResult,
    LegalHoldRequest,
    LegalHoldResponse,
    OfflineSignalMappingResponse,
    OfflineSignalMappingWrite,
    ReportReviewRequest,
    SignalEvaluationResponse,
    SupplementPlanResponse,
    UploadCompleteRequest,
    UploadPartResponse,
    UploadPartTarget,
    UploadSessionCreate,
    UploadSessionResponse,
)
from app.services.deletion_service import DiagnosisDeletionService
from app.services.evidence_upload_service import EvidenceUploadService
from app.services.offline_analysis_service import OfflineAnalysisService, OfflineEvidenceProvider
from app.services.offline_governance_service import OfflineGovernanceService

router = APIRouter(tags=["offline-evidence"])


@router.post(
    "/api/diagnosis-sessions/{session_id}/uploads",
    response_model=UploadSessionResponse,
    status_code=201,
)
async def create_upload_session(
    session_id: UUID,
    body: UploadSessionCreate,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[EvidenceUploadService, Depends(get_evidence_upload_service)],
) -> UploadSessionResponse:
    """创建 Upload Session（上传会话），返回绕过网关的直传分片地址。"""

    source_ip = request.client.host if request.client else None
    result = await service.create(
        actor=actor,
        session_id=str(session_id),
        command=body,
        idempotency_key=idempotency_key,
        source_ip=source_ip,
    )
    response.headers["Idempotent-Replayed"] = "false" if result.created else "true"
    return _upload_response(result.row, result.token)


@router.get(
    "/api/diagnosis-sessions/{session_id}/uploads/{upload_id}",
    response_model=UploadSessionResponse,
)
async def get_upload_session(
    session_id: UUID,
    upload_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[EvidenceUploadService, Depends(get_evidence_upload_service)],
) -> UploadSessionResponse:
    """查询分片续传状态；已签发直传 token 不会再次返回。"""

    row = await service.get_upload(actor=actor, session_id=str(session_id), upload_id=str(upload_id))
    return _upload_response(row, None)


@router.put(
    "/api/direct/diagnosis-uploads/{upload_id}/parts/{part_number}",
    response_model=UploadPartResponse,
)
async def upload_part_direct(
    upload_id: UUID,
    part_number: int,
    request: Request,
    upload_token: Annotated[str, Header(alias="X-Upload-Token", min_length=32, max_length=256)],
    part_sha256: Annotated[str | None, Header(alias="X-Part-SHA256", pattern=r"^[0-9a-f]{64}$")] = None,
    service: Annotated[EvidenceUploadService, Depends(get_evidence_upload_service)] = None,
) -> UploadPartResponse:
    """对象数据面流式写分片；该路径禁止配置到 API Gateway 控制面代理。"""

    result = await service.record_part(
        upload_id=str(upload_id),
        part_number=part_number,
        token=upload_token,
        chunks=request.stream(),
        claimed_sha256=part_sha256,
    )
    return UploadPartResponse.model_validate(result)


@router.post(
    "/api/diagnosis-sessions/{session_id}/uploads/{upload_id}/complete",
    response_model=EvidenceBundleResponse,
)
async def complete_upload(
    session_id: UUID,
    upload_id: UUID,
    body: UploadCompleteRequest,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[EvidenceUploadService, Depends(get_evidence_upload_service)],
) -> EvidenceBundleResponse:
    """完成上传并创建 Diagnostic Evidence Bundle（诊断证据包）异步任务。"""

    bundle, created = await service.complete(
        actor=actor,
        session_id=str(session_id),
        upload_id=str(upload_id),
        part_numbers=body.parts,
    )
    response.headers["Idempotent-Replayed"] = "false" if created else "true"
    return EvidenceBundleResponse.model_validate(bundle)


@router.post("/api/diagnosis-sessions/{session_id}/uploads/{upload_id}/abort")
async def abort_upload(
    session_id: UUID,
    upload_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[EvidenceUploadService, Depends(get_evidence_upload_service)],
) -> dict[str, Any]:
    """终止失败或不再需要的上传会话。"""

    return await service.abort(actor=actor, session_id=str(session_id), upload_id=str(upload_id))


@router.get(
    "/api/diagnosis-sessions/{session_id}/bundles",
    response_model=list[EvidenceBundleResponse],
)
async def list_bundles(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[EvidenceUploadService, Depends(get_evidence_upload_service)],
) -> list[EvidenceBundleResponse]:
    """列出初始包和补采包。"""

    rows = await service.list_bundles(actor=actor, session_id=str(session_id))
    return [EvidenceBundleResponse.model_validate(row) for row in rows]


@router.get(
    "/api/diagnosis-sessions/{session_id}/bundles/{bundle_id}",
    response_model=EvidenceBundleResponse,
)
async def get_bundle(
    session_id: UUID,
    bundle_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[EvidenceUploadService, Depends(get_evidence_upload_service)],
) -> EvidenceBundleResponse:
    """读取单个证据包的安全处理状态。"""

    row = await service.get_bundle(actor=actor, session_id=str(session_id), bundle_id=str(bundle_id))
    return EvidenceBundleResponse.model_validate(row)


@router.post(
    "/api/diagnosis-sessions/{session_id}/evidence/query",
    response_model=EvidenceQueryResult,
)
async def query_evidence(
    session_id: UUID,
    body: EvidenceQueryRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    provider: Annotated[OfflineEvidenceProvider, Depends(get_offline_evidence_provider)],
) -> EvidenceQueryResult:
    """查询标准化离线证据，不暴露对象存储内部键。"""

    result = await provider.query(actor=actor, session_id=str(session_id), command=body)
    return EvidenceQueryResult.model_validate(result)


@router.get(
    "/api/diagnosis-sessions/{session_id}/assessment",
    response_model=EvidenceAssessmentResponse,
)
async def get_assessment(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineAnalysisService, Depends(get_offline_analysis_service)],
) -> EvidenceAssessmentResponse:
    """读取最新 Evidence Assessment（证据评估）。"""

    row = await service.get_assessment(actor=actor, session_id=str(session_id))
    return EvidenceAssessmentResponse(
        assessment_id=row["assessment_id"],
        session_id=row["session_id"],
        bundle_ids=[UUID(value) for value in row["bundle_ids"]],
        algorithm_version=row["algorithm_version"],
        completeness_score=row["completeness_score"],
        mandatory={"total": row["mandatory_total"], "available": row["mandatory_available"]},
        missing_evidence=row["missing_evidence"],
        diagnosable_scope=row["diagnosable_scope"],
        non_diagnosable_scope=row["non_diagnosable_scope"],
        ready_for_diagnosis=row["ready_for_diagnosis"],
        calculation_details=row["calculation_details"],
        trace_id=row["trace_id"],
        created_at=row["created_at"],
    )


@router.get(
    "/api/diagnosis-sessions/{session_id}/supplement-plan",
    response_model=SupplementPlanResponse,
)
async def get_supplement_plan(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineGovernanceService, Depends(get_offline_governance_service)],
) -> SupplementPlanResponse:
    """读取当前会话唯一 Supplement Plan（补充采集计划）。"""

    row = await service.get_supplement_plan(actor=actor, session_id=str(session_id))
    return SupplementPlanResponse.model_validate(row)


@router.post(
    "/api/internal/diagnosis-sessions/{session_id}/diagnose",
    response_model=DiagnosisRunResponse,
)
async def start_diagnosis(
    session_id: UUID,
    body: DiagnosisStartRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineAnalysisService, Depends(get_offline_analysis_service)],
) -> DiagnosisRunResponse:
    """显式重放诊断；P0 只允许原规则快照，最新规则重放预留到 P1。"""

    if not actor.has_any_role("diagnosis_worker", "platform_admin"):
        raise DiagnosisError(code="FORBIDDEN", message="当前角色无权触发诊断运行", http_status=403)
    if body.use_latest_rules:
        raise DiagnosisError(
            code="LATEST_RULE_REPLAY_NOT_SUPPORTED",
            message="P0 只支持按原规则快照复现，最新规则重放属于 P1",
            http_status=422,
        )
    existing_runs = await service.list_runs(actor=actor, session_id=str(session_id))
    row = (
        existing_runs[-1]
        if existing_runs
        else await service.assess_and_diagnose(
            tenant_id=actor.tenant_id,
            session_id=str(session_id),
            trace_id=request_trace_id(),
        )
    )
    return DiagnosisRunResponse.model_validate(row)


@router.get(
    "/api/diagnosis-sessions/{session_id}/runs",
    response_model=list[DiagnosisRunResponse],
)
async def list_runs(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineAnalysisService, Depends(get_offline_analysis_service)],
) -> list[DiagnosisRunResponse]:
    """列出初始分析和补采重分析。"""

    rows = await service.list_runs(actor=actor, session_id=str(session_id))
    return [DiagnosisRunResponse.model_validate(row) for row in rows]


@router.get(
    "/api/diagnosis-sessions/{session_id}/runs/{run_id}/signals",
    response_model=list[SignalEvaluationResponse],
)
async def list_signal_evaluations(
    session_id: UUID,
    run_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineAnalysisService, Depends(get_offline_analysis_service)],
) -> list[SignalEvaluationResponse]:
    """读取指定 Diagnosis Run（诊断运行）的三态 Signal（信号）明细。"""

    rows = await service.list_signal_evaluations(
        actor=actor,
        session_id=str(session_id),
        run_id=str(run_id),
    )
    return [SignalEvaluationResponse.model_validate(row) for row in rows]


@router.get(
    "/api/diagnosis-sessions/{session_id}/runs/{run_id}/candidates",
    response_model=list[DiagnosisCandidateResponse],
)
async def list_diagnosis_candidates(
    session_id: UUID,
    run_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineAnalysisService, Depends(get_offline_analysis_service)],
) -> list[DiagnosisCandidateResponse]:
    """读取指定 Diagnosis Run（诊断运行）的 KBD（知识库文档）候选快照。"""

    rows = await service.list_diagnosis_candidates(
        actor=actor,
        session_id=str(session_id),
        run_id=str(run_id),
    )
    return [DiagnosisCandidateResponse.model_validate(row) for row in rows]


@router.get(
    "/api/diagnosis-sessions/{session_id}/timeline",
    response_model=list[DiagnosisTimelineEvent],
)
async def get_timeline(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineGovernanceService, Depends(get_offline_governance_service)],
) -> list[DiagnosisTimelineEvent]:
    """读取 Diagnosis Timeline（诊断时间线）。"""

    rows = await service.get_timeline(actor=actor, session_id=str(session_id))
    return [DiagnosisTimelineEvent.model_validate(row) for row in rows]


@router.get(
    "/api/diagnosis-sessions/{session_id}/reports",
    response_model=list[DiagnosisReportResponse],
)
async def list_reports(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineAnalysisService, Depends(get_offline_analysis_service)],
) -> list[DiagnosisReportResponse]:
    """按身份返回可见的报告版本。"""

    rows = await service.list_reports(actor=actor, session_id=str(session_id))
    return [DiagnosisReportResponse.model_validate(row) for row in rows]


@router.post(
    "/api/diagnosis-sessions/{session_id}/reports/{report_id}/review",
    response_model=DiagnosisReportResponse,
)
async def review_report(
    session_id: UUID,
    report_id: UUID,
    body: ReportReviewRequest,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match")],
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineAnalysisService, Depends(get_offline_analysis_service)],
) -> DiagnosisReportResponse:
    """审核、修改、确认或发布报告，使用 If-Match 乐观锁。"""

    expected_version = _parse_if_match(if_match)
    row = await service.review_report(
        actor=actor,
        session_id=str(session_id),
        report_id=str(report_id),
        expected_version=expected_version,
        command=body,
    )
    response.headers["ETag"] = f'"{row["version"]}"'
    return DiagnosisReportResponse.model_validate(row)


@router.post(
    "/api/diagnosis-sessions/{session_id}/deletion",
    response_model=DeletionJobResponse,
    status_code=202,
)
async def request_deletion(
    session_id: UUID,
    body: DeletionRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisDeletionService, Depends(get_deletion_service)],
) -> DeletionJobResponse:
    """发起异步删除。"""

    return DeletionJobResponse.model_validate(
        await service.request(actor=actor, session_id=str(session_id), reason=body.reason)
    )


@router.post(
    "/api/internal/diagnosis-sessions/{session_id}/deletion/execute",
    response_model=DeletionJobResponse,
)
async def execute_deletion(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisDeletionService, Depends(get_deletion_service)],
) -> DeletionJobResponse:
    """由 Worker 执行删除清单。"""

    return DeletionJobResponse.model_validate(await service.execute(actor=actor, session_id=str(session_id)))


@router.get(
    "/api/diagnosis-sessions/{session_id}/deletion",
    response_model=DeletionJobResponse,
)
async def get_deletion(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisDeletionService, Depends(get_deletion_service)],
) -> DeletionJobResponse:
    """读取删除清单状态。"""

    return DeletionJobResponse.model_validate(await service.get(actor=actor, session_id=str(session_id)))


@router.get(
    "/api/diagnosis-sessions/{session_id}/legal-hold",
    response_model=LegalHoldResponse,
)
async def get_legal_hold(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineGovernanceService, Depends(get_offline_governance_service)],
) -> LegalHoldResponse:
    """读取 Legal Hold（法务保全）状态。"""

    return LegalHoldResponse.model_validate(await service.get_legal_hold(actor=actor, session_id=str(session_id)))


@router.post(
    "/api/diagnosis-sessions/{session_id}/legal-hold",
    response_model=LegalHoldResponse,
)
async def update_legal_hold(
    session_id: UUID,
    body: LegalHoldRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineGovernanceService, Depends(get_offline_governance_service)],
) -> LegalHoldResponse:
    """设置或由另一名管理员解除 Legal Hold（法务保全）。"""

    result = await service.update_legal_hold(actor=actor, session_id=str(session_id), command=body)
    return LegalHoldResponse.model_validate(result)


@router.get(
    "/api/internal/offline-signal-mappings",
    response_model=list[OfflineSignalMappingResponse],
)
async def list_offline_signal_mappings(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineGovernanceService, Depends(get_offline_governance_service)],
) -> list[OfflineSignalMappingResponse]:
    """列出 Offline Signal Mapping（离线信号映射）。"""

    rows = await service.list_signal_mappings(actor=actor)
    return [OfflineSignalMappingResponse.model_validate(row) for row in rows]


@router.get("/api/internal/kbd-collection-impact/{kbd_id}")
async def analyze_kbd_collection_impact(
    kbd_id: int,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineGovernanceService, Depends(get_offline_governance_service)],
) -> dict[str, Any]:
    """查看 KBD 对离线采集资源和历史计划/制品的影响。"""

    return await service.analyze_kbd_collection_impact(actor=actor, kbd_id=kbd_id)


@router.put(
    "/api/internal/offline-signal-mappings/{mapping_id}",
    response_model=OfflineSignalMappingResponse,
)
async def save_offline_signal_mapping(
    mapping_id: UUID,
    body: OfflineSignalMappingWrite,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineGovernanceService, Depends(get_offline_governance_service)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> OfflineSignalMappingResponse:
    """创建或按 If-Match 更新离线信号映射。"""

    row = await service.save_signal_mapping(
        actor=actor,
        mapping_id=str(mapping_id),
        command=body,
        if_match=if_match,
    )
    response.headers["ETag"] = f'"{row["lock_version"]}"'
    return OfflineSignalMappingResponse.model_validate(row)


@router.get("/api/internal/diagnosis-security/encryption-key")
async def get_encryption_key(
    request: Request,
    actor: Annotated[ActorContext, Depends(require_actor)],
) -> dict[str, str]:
    """获取证据包加密公钥；私钥仅存在于隔离 Worker。"""

    if not actor.has_any_role("platform_admin", "support_engineer", "diagnosis_worker"):
        raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取诊断包加密公钥", http_status=403)
    encryption = getattr(request.app.state, "envelope_encryption", None)
    if encryption is None:
        raise DiagnosisError(
            code="ENCRYPTION_PROVIDER_UNAVAILABLE",
            message="诊断包加密密钥尚未配置",
            http_status=503,
        )
    return encryption.public_metadata()


def _upload_response(row: dict[str, Any], token: str | None) -> UploadSessionResponse:
    targets = []
    if token:
        base_url = settings.DIAGNOSIS_DIRECT_UPLOAD_BASE_URL.rstrip("/")
        targets = [
            UploadPartTarget(
                part_number=part_number,
                upload_url=f"{base_url}/api/direct/diagnosis-uploads/{row['upload_id']}/parts/{part_number}",
                expires_at=row["expires_at"],
            )
            for part_number in range(1, row["part_count"] + 1)
        ]
    return UploadSessionResponse(
        upload_id=row["upload_id"],
        session_id=row["session_id"],
        status=row["status"],
        bundle_type=row["bundle_type"],
        total_size_bytes=row["total_size_bytes"],
        chunk_size_bytes=row["chunk_size_bytes"],
        part_count=row["part_count"],
        uploaded_parts=row["uploaded_parts"],
        upload_token=token,
        upload_targets=targets,
        expires_at=row["expires_at"],
        trace_id=row["trace_id"],
    )


def _parse_if_match(value: str) -> int:
    normalized = value.strip().strip('"')
    if not normalized.isdigit() or int(normalized) < 1:
        raise DiagnosisError(code="INVALID_IF_MATCH", message="If-Match 必须是有效报告版本号", http_status=422)
    return int(normalized)


def request_trace_id() -> str:
    """为显式内部重放生成关联标识；正常 Worker 使用原上传 trace。"""

    from shared.observability.otel import get_current_trace_id

    return get_current_trace_id() or UUID(int=0).hex
