"""Collector 离线信任链与 Go Verification Bundle（验证包）测试。"""

import base64
import hashlib
import io
import json
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
from app.auth import ActorContext
from app.domain.signed_document import attach_detached_signature, canonical_json_bytes
from app.errors import DiagnosisError
from app.services.artifact_signer import Ed25519ArtifactSigner
from app.services.collector_trust_service import GO_RUNTIME_FILE_NAME, CollectorTrustService
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class FakeArtifactRepository:
    """离线信任链测试仓储。"""

    def __init__(self, artifact, revoked=None):
        self.artifact = artifact
        self.revoked = revoked or []

    async def get_by_id_for_tenant(self, artifact_id, tenant_id):
        """返回匹配租户和制品 ID 的对象。"""

        if str(artifact_id) == str(self.artifact.artifact_id) and tenant_id == self.artifact.tenant_id:
            return self.artifact
        return None

    async def list_revoked_for_tenant(self, tenant_id):
        """返回租户范围的撤销对象。"""

        return [artifact for artifact in self.revoked if artifact.tenant_id == tenant_id]


class FakeSessionRepository:
    """离线工单上下文测试仓储。"""

    def __init__(self, session_obj):
        self.session_obj = session_obj

    async def get_by_id_for_tenant(self, session_id, tenant_id):
        """返回匹配租户和会话 ID 的会话对象。"""

        if self.session_obj is None:
            return None
        if str(session_id) == str(self.session_obj.session_id) and tenant_id == self.session_obj.tenant_id:
            return self.session_obj
        return None


def build_signer() -> Ed25519ArtifactSigner:
    """构造随机测试签名器。"""

    private_key = Ed25519PrivateKey.generate()
    return Ed25519ArtifactSigner(
        private_key_base64=base64.b64encode(private_key.private_bytes_raw()).decode("ascii"),
        key_id="collector-test-key",
    )


def build_session(session_id):
    """构造含离线工单上下文全字段的诊断会话。"""

    return SimpleNamespace(
        session_id=session_id,
        tenant_id="tenant-a",
        case_id="Q2026073100001",
        selected_scenario="vm_start_failed",
        incident_start_time=datetime(2026, 7, 31, 9, 0, tzinfo=UTC),
        incident_end_time=datetime(2026, 7, 31, 10, 0, tzinfo=UTC),
        incident_timezone="Asia/Shanghai",
        affected_objects=[{"type": "vm", "id": "vm-1", "name": "web-01", "source_node": "node-1"}],
    )


def build_artifact(signer: Ed25519ArtifactSigner, *, revoked: bool = False):
    """构造含双签名清单的制品。"""

    now = datetime.now(UTC)
    artifact_id = uuid4()
    session_id = uuid4()
    file_name = f"collector_{str(session_id)[:8]}_all.hci-collector.json"
    content = (
        json.dumps(
            {
                "schema_version": "1.0",
                "artifact_id": str(artifact_id),
                "session_id": str(session_id),
                "collection_plan_id": "plan-test",
                "target_key": "all",
                "execution_items": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    content_signature = signer.sign(content)
    manifest = attach_detached_signature(
        {
            "schema_version": "1.2",
            "artifact_id": str(artifact_id),
            "artifact_type": "structured_collector",
            "session_id": str(session_id),
            "collection_plan_id": "plan-test",
            "target_key": "all",
            "file_name": file_name,
            "product_version": "6.11",
            "profile": {"name": "test-profile", "revision": 1, "version": "1.0.0", "checksum": "checksum-demo"},
            "bundle_encryption": {
                "algorithm": "AES-256-GCM",
                "key_wrap_algorithm": "RSA-OAEP-SHA256",
                "key_id": "test-encryption-key",
                "public_key_pem_base64": "dGVzdC1wdWJsaWMta2V5",
                "format": "HCIEB1",
            },
            "artifact_sha256": hashlib.sha256(content).hexdigest(),
            "signature": {
                "algorithm": content_signature.algorithm,
                "key_id": content_signature.key_id,
                "signature_base64": content_signature.signature_base64,
                "public_key_base64": content_signature.public_key_base64,
                "public_key_fingerprint": content_signature.public_key_fingerprint,
                "signed_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=2)).isoformat(),
            },
            "collection_items": [],
        },
        signer,
    )
    return SimpleNamespace(
        artifact_id=artifact_id,
        session_id=session_id,
        tenant_id="tenant-a",
        file_name=file_name,
        content_text=content.decode("utf-8"),
        artifact_sha256=manifest["artifact_sha256"],
        signing_key_id=content_signature.key_id,
        public_key_base64=content_signature.public_key_base64,
        public_key_fingerprint=content_signature.public_key_fingerprint,
        manifest_json=manifest,
        status="revoked" if revoked else "ready",
        expires_at=now + timedelta(hours=2),
        revoked_at=now if revoked else None,
        revoked_by="admin-a" if revoked else None,
        revocation_reason="incident_response" if revoked else None,
    )


def build_actor() -> ActorContext:
    """构造可信内部操作者。"""

    return ActorContext(tenant_id="tenant-a", user_id="support-a", roles=frozenset({"support_engineer"}))


def build_service(signer, artifact, *, revoked=None, session="default") -> CollectorTrustService:
    """构造注入测试仓储的离线信任链服务。"""

    if session == "default":
        session_repository = FakeSessionRepository(build_session(artifact.session_id))
    elif session is None:
        session_repository = FakeSessionRepository(None)
    else:
        session_repository = FakeSessionRepository(session)
    return CollectorTrustService(
        artifact_repository=FakeArtifactRepository(artifact, revoked=revoked or []),
        session_repository=session_repository,
        signer=signer,
    )


def verify_signed_document(document: dict, public_key_base64: str) -> dict:
    """在测试侧验证规范 JSON 文档签名。"""

    unsigned = dict(document)
    signature = unsigned.pop("document_signature")
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_base64)).verify(
        base64.b64decode(signature["signature_base64"]), canonical_json_bytes(unsigned)
    )
    return unsigned


@pytest.mark.asyncio
async def test_trust_store_and_revocation_list_are_bound_to_current_key():
    """受信根和吊销清单必须使用当前密钥并覆盖撤销审计字段。"""

    signer = build_signer()
    artifact = build_artifact(signer, revoked=True)
    service = build_service(signer, artifact, revoked=[artifact])
    trust_store = service.trust_store(actor=build_actor())
    revocations = await service.revocation_list(actor=build_actor())
    unsigned = verify_signed_document(revocations, trust_store["keys"][0]["public_key_base64"])
    assert unsigned["revoked_artifacts"][0]["artifact_id"] == str(artifact.artifact_id)
    assert unsigned["revoked_artifacts"][0]["reason"] == "incident_response"


@pytest.mark.asyncio
async def test_verification_bundle_contains_signed_static_go_runtime_and_case():
    """验证包只交付签名的静态 Go 运行时，不再携带 Python、wheel 或 Bash 启动器。"""

    signer = build_signer()
    artifact = build_artifact(signer)
    bundle = await build_service(signer, artifact).build_verification_bundle(
        actor=build_actor(), session_id=str(artifact.session_id), artifact_id=str(artifact.artifact_id)
    )
    assert bundle.bundle_sha256 == hashlib.sha256(bundle.content).hexdigest()
    with ZipFile(io.BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        assert {
            artifact.file_name,
            "artifact-manifest.json",
            "runtime-manifest.json",
            "trust-store.json",
            "revocations.json",
            GO_RUNTIME_FILE_NAME,
            "case.json",
            "README.txt",
        } == names
        assert not any(name.endswith((".py", ".whl", ".sh")) for name in names)
        runtime = archive.read(GO_RUNTIME_FILE_NAME)
        assert runtime.startswith(b"\x7fELF")
        mode = archive.getinfo(GO_RUNTIME_FILE_NAME).external_attr >> 16
        assert stat.S_ISREG(mode)
        assert mode & 0o111 == 0o111
        runtime_manifest = json.loads(archive.read("runtime-manifest.json"))
        trust_store = json.loads(archive.read("trust-store.json"))
        unsigned_runtime = verify_signed_document(runtime_manifest, trust_store["keys"][0]["public_key_base64"])
        assert unsigned_runtime["sha256"] == hashlib.sha256(runtime).hexdigest()
        assert unsigned_runtime["os"] == "linux"
        assert unsigned_runtime["arch"] == "amd64"
        case_document = json.loads(archive.read("case.json"))
        unsigned_case = verify_signed_document(case_document, trust_store["keys"][0]["public_key_base64"])
        readme = archive.read("README.txt").decode("utf-8")
    assert unsigned_case["session_id"] == str(artifact.session_id)
    assert unsigned_case["targets"] == [{"type": "vm", "id": "vm-1"}]
    assert "./hci-collect-linux-amd64" in readme
    assert "不依赖 Python" in readme


@pytest.mark.asyncio
async def test_historical_artifact_without_signed_manifest_is_rejected():
    """历史 1.0 制品不能伪装成具备完整离线信任链。"""

    signer = build_signer()
    artifact = build_artifact(signer)
    artifact.manifest_json = {"schema_version": "1.0"}
    with pytest.raises(DiagnosisError) as exc_info:
        await build_service(signer, artifact).build_verification_bundle(
            actor=build_actor(), session_id=str(artifact.session_id), artifact_id=str(artifact.artifact_id)
        )
    assert exc_info.value.code == "ARTIFACT_VERIFICATION_MANIFEST_UNAVAILABLE"


@pytest.mark.asyncio
async def test_artifact_without_encryption_metadata_cannot_be_redistributed():
    """缺少证据包加密公钥的旧制品必须在下载验证包时提前拒绝。"""

    signer = build_signer()
    artifact = build_artifact(signer)
    artifact.manifest_json["bundle_encryption"] = None
    with pytest.raises(DiagnosisError) as exc_info:
        await build_service(signer, artifact).build_verification_bundle(
            actor=build_actor(), session_id=str(artifact.session_id), artifact_id=str(artifact.artifact_id)
        )
    assert exc_info.value.code == "ARTIFACT_ENCRYPTION_METADATA_UNAVAILABLE"


@pytest.mark.asyncio
async def test_revoked_artifact_cannot_be_redistributed_in_verification_bundle():
    """平台不得借验证包接口重新分发已撤销制品正文。"""

    signer = build_signer()
    artifact = build_artifact(signer, revoked=True)
    with pytest.raises(DiagnosisError) as exc_info:
        await build_service(signer, artifact, revoked=[artifact]).build_verification_bundle(
            actor=build_actor(), session_id=str(artifact.session_id), artifact_id=str(artifact.artifact_id)
        )
    assert exc_info.value.code == "COLLECTOR_ARTIFACT_EXPIRED"


@pytest.mark.asyncio
async def test_verification_bundle_requires_existing_session():
    """会话缺失时不得生成验证包。"""

    signer = build_signer()
    artifact = build_artifact(signer)
    with pytest.raises(DiagnosisError) as exc_info:
        await build_service(signer, artifact, session=None).build_verification_bundle(
            actor=build_actor(), session_id=str(artifact.session_id), artifact_id=str(artifact.artifact_id)
        )
    assert exc_info.value.code == "DIAGNOSIS_SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_verification_bundle_rejects_session_without_targets():
    """受影响对象为空的会话不能产出必被 Worker 拒绝的证据包上下文。"""

    signer = build_signer()
    artifact = build_artifact(signer)
    session = build_session(artifact.session_id)
    session.affected_objects = []
    with pytest.raises(DiagnosisError) as exc_info:
        await build_service(signer, artifact, session=session).build_verification_bundle(
            actor=build_actor(), session_id=str(artifact.session_id), artifact_id=str(artifact.artifact_id)
        )
    assert exc_info.value.code == "CASE_TARGETS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_verification_bundle_uses_execution_node_when_business_object_does_not_exist():
    """创建失败场景用真实执行节点构造 case target，不伪造业务对象。"""

    signer = build_signer()
    artifact = build_artifact(signer)
    session = build_session(artifact.session_id)
    session.affected_objects = [{"type": "execution_node", "id": None, "source_node": "node-1"}]
    result = await build_service(signer, artifact, session=session).build_verification_bundle(
        actor=build_actor(), session_id=str(artifact.session_id), artifact_id=str(artifact.artifact_id)
    )

    with ZipFile(io.BytesIO(result.content)) as archive:
        case_document = json.loads(archive.read("case.json"))
        trust_store = json.loads(archive.read("trust-store.json"))
    unsigned_case = verify_signed_document(case_document, trust_store["keys"][0]["public_key_base64"])
    assert unsigned_case["targets"] == [{"type": "node", "id": "node-1"}]
