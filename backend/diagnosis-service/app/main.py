"""Diagnosis Service（诊断服务）主应用。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app

from app.auth import InternalTokenIdentityVerifier, OidcJwtIdentityVerifier
from app.config import settings
from app.dependencies import build_artifact_signer, build_envelope_encryption, build_object_storage
from app.errors import register_error_handlers
from app.routes import (
    collection_plans,
    collection_profiles,
    collector_artifacts,
    collector_definitions,
    collector_trust,
    diagnosis_management,
    diagnosis_sessions,
    evidence_lifecycle,
    offline_resource_sync,
)

init_telemetry(settings.SERVICE_NAME)
logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理数据库和身份验证器生命周期。"""

    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT,
    )
    app.state.database_manager = DatabaseManager(settings.DATABASE_URL)
    if not hasattr(app.state, "identity_verifier"):
        # 本地控制面可使用内部令牌；正式客户入口使用 OIDC 公钥验签。
        if settings.IDENTITY_MODE == "oidc":
            app.state.identity_verifier = OidcJwtIdentityVerifier(
                public_key_pem_base64=settings.OIDC_PUBLIC_KEY_PEM_B64,
                jwks_url=settings.OIDC_JWKS_URL,
                issuer=settings.OIDC_ISSUER,
                audience=settings.OIDC_AUDIENCE,
                clock_skew_seconds=settings.OIDC_CLOCK_SKEW_SECONDS,
            )
        elif settings.IDENTITY_MODE == "internal":
            app.state.identity_verifier = (
                InternalTokenIdentityVerifier(settings.INTERNAL_API_TOKEN)
                if settings.INTERNAL_API_TOKEN.strip()
                else None
            )
        else:
            app.state.identity_verifier = None
    if not hasattr(app.state, "artifact_signer"):
        app.state.artifact_signer = build_artifact_signer()
    if not hasattr(app.state, "object_storage"):
        app.state.object_storage = build_object_storage()
    if not hasattr(app.state, "envelope_encryption"):
        app.state.envelope_encryption = build_envelope_encryption()

    yield

    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    await app.state.database_manager.close()


app = FastAPI(
    title="HCI Troubleshoot - Diagnosis Service",
    description="离线诊断领域服务",
    version="0.4.0",
    lifespan=lifespan,
)
instrument_app(app)
app.add_middleware(HTTPMetricsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.diagnosis_cors_origins,
    allow_credentials=False,
    allow_methods=["PUT", "OPTIONS"],
    allow_headers=["Content-Type", "X-Upload-Token", "X-Part-SHA256", "Traceparent", "Tracestate"],
    max_age=600,
)
register_error_handlers(app)
app.include_router(diagnosis_sessions.router)
app.include_router(diagnosis_management.router)
app.include_router(collection_profiles.scenario_router)
app.include_router(collection_profiles.router)
app.include_router(collection_plans.router)
app.include_router(collection_plans.management_router)
app.include_router(collector_definitions.router)
app.include_router(collector_trust.router)
app.include_router(collector_artifacts.router)
app.include_router(collector_artifacts.management_router)
app.include_router(evidence_lifecycle.router)
app.include_router(offline_resource_sync.router)


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus 指标端点。"""

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    """存活探针。"""

    return {"status": "alive"}


@app.get("/health")
async def health() -> dict[str, str]:
    """统一健康检查端点，供网关和部署冒烟使用。"""

    return {"status": "healthy"}


@app.get("/health/startup")
async def health_startup() -> dict[str, str]:
    """启动探针。"""

    return {"status": "started"}


@app.get("/health/ready")
async def health_ready() -> dict[str, str]:
    """就绪探针，同时验证数据库可达性。"""

    if not await app.state.database_manager.health_check():
        from app.errors import DiagnosisError

        raise DiagnosisError(
            code="DATABASE_UNAVAILABLE",
            message="诊断服务数据库不可用",
            http_status=503,
            retryable=True,
        )
    return {"status": "ready"}
