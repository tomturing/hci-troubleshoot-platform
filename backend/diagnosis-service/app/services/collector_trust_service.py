"""Collector Artifact（采集器制品）离线信任链服务。"""

import hashlib
import io
import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.auth import ActorContext
from app.domain.signed_document import attach_detached_signature
from app.errors import DiagnosisError
from app.models.diagnosis_session import DiagnosisSession
from app.repositories.collector_artifact_repository import CollectorArtifactRepository
from app.repositories.diagnosis_session_repository import DiagnosisSessionRepository
from app.services.artifact_signer import ArtifactSigner

TRUST_ROLES = frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})
REVOCATION_LIST_VALIDITY = timedelta(hours=24)
GO_RUNTIME_FILE_NAME = "hci-collect-linux-amd64"


@dataclass(frozen=True, slots=True)
class VerificationBundle:
    """可下载的离线验证包。"""

    content: bytes
    file_name: str
    root_fingerprint: str
    revocation_next_update_at: datetime
    bundle_sha256: str


class CollectorTrustService:
    """生成受信根、吊销清单和自包含验证包。"""

    def __init__(
        self,
        *,
        artifact_repository: CollectorArtifactRepository,
        session_repository: DiagnosisSessionRepository,
        signer: ArtifactSigner | None,
    ):
        self._artifact_repository = artifact_repository
        self._session_repository = session_repository
        self._signer = signer

    def trust_store(self, *, actor: ActorContext, now: datetime | None = None) -> dict[str, Any]:
        """返回当前单密钥 P0 受信根。"""

        self._require_role(actor)
        signer = self._require_signer()
        identity = signer.public_identity()
        generated_at = now or datetime.now(UTC)
        return {
            "schema_version": "1.0",
            "generated_at": generated_at.isoformat(),
            "keys": [
                {
                    "algorithm": identity.algorithm,
                    "key_id": identity.key_id,
                    "public_key_base64": identity.public_key_base64,
                    "public_key_fingerprint": identity.public_key_fingerprint,
                    "status": "trusted",
                }
            ],
        }

    async def revocation_list(
        self,
        *,
        actor: ActorContext,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """生成租户范围内、短时有效的签名制品吊销清单。"""

        self._require_role(actor)
        signer = self._require_signer()
        generated_at = now or datetime.now(UTC)
        revoked = await self._artifact_repository.list_revoked_for_tenant(actor.tenant_id)
        document = {
            "schema_version": "1.0",
            "generated_at": generated_at.isoformat(),
            "next_update_at": (generated_at + REVOCATION_LIST_VALIDITY).isoformat(),
            "revoked_artifacts": [
                {
                    "artifact_id": str(artifact.artifact_id),
                    "artifact_sha256": artifact.artifact_sha256,
                    "revoked_at": artifact.revoked_at.isoformat() if artifact.revoked_at else None,
                    "reason": artifact.revocation_reason or "platform_revoked",
                }
                for artifact in revoked
            ],
        }
        return attach_detached_signature(document, signer)

    async def build_verification_bundle(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        artifact_id: str,
        now: datetime | None = None,
    ) -> VerificationBundle:
        """构建包含 Go 运行时、结构化制品、签名文档和吊销快照的 ZIP。"""

        self._require_role(actor)
        signer = self._require_signer()
        artifact = await self._artifact_repository.get_by_id_for_tenant(artifact_id, actor.tenant_id)
        if artifact is None or str(artifact.session_id) != str(session_id):
            raise DiagnosisError(code="COLLECTOR_ARTIFACT_NOT_FOUND", message="采集器制品不存在", http_status=404)
        diagnosis_session = await self._session_repository.get_by_id_for_tenant(session_id, actor.tenant_id)
        if diagnosis_session is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        generated_at = now or datetime.now(UTC)
        if artifact.status != "ready" or artifact.expires_at <= generated_at:
            raise DiagnosisError(
                code="COLLECTOR_ARTIFACT_EXPIRED",
                message="采集器制品已过期或已撤销，不能再次分发验证包",
                http_status=410,
            )

        identity = signer.public_identity()
        if (
            artifact.signing_key_id != identity.key_id
            or artifact.public_key_fingerprint != identity.public_key_fingerprint
            or artifact.public_key_base64 != identity.public_key_base64
        ):
            raise DiagnosisError(
                code="ARTIFACT_SIGNING_KEY_NOT_TRUSTED",
                message="制品签名密钥不属于当前 P0 单密钥受信根",
                http_status=409,
                details={"artifact_key_id": artifact.signing_key_id, "trusted_key_id": identity.key_id},
            )

        manifest = dict(artifact.manifest_json or {})
        if (
            manifest.get("schema_version") != "1.2"
            or manifest.get("artifact_type") != "structured_collector"
            or not isinstance(manifest.get("document_signature"), dict)
        ):
            raise DiagnosisError(
                code="ARTIFACT_VERIFICATION_MANIFEST_UNAVAILABLE",
                message="该历史制品不是无 Shell 结构化制品，请重新生成制品",
                http_status=409,
            )
        encryption = manifest.get("bundle_encryption")
        if (
            not isinstance(encryption, dict)
            or encryption.get("algorithm") != "AES-256-GCM"
            or encryption.get("key_wrap_algorithm") != "RSA-OAEP-SHA256"
            or encryption.get("format") not in {"HCIEB1", "HCIEB2"}
            or not encryption.get("key_id")
            or not encryption.get("public_key_pem_base64")
        ):
            raise DiagnosisError(
                code="ARTIFACT_ENCRYPTION_METADATA_UNAVAILABLE",
                message="该制品缺少证据包加密公钥，请配置密钥后重新生成制品",
                http_status=409,
            )

        trust_store = self.trust_store(actor=actor, now=generated_at)
        revocations = await self.revocation_list(actor=actor, now=generated_at)
        go_runtime = self._load_resource_bytes(f"bin/{GO_RUNTIME_FILE_NAME}")
        runtime_manifest = attach_detached_signature(
            {
                "schema_version": "1.0",
                "file_name": GO_RUNTIME_FILE_NAME,
                "os": "linux",
                "arch": "amd64",
                "sha256": hashlib.sha256(go_runtime).hexdigest(),
                "size_bytes": len(go_runtime),
                "built_with": "Go standard library",
            },
            signer,
        )
        case_document = attach_detached_signature(self._build_case_document(diagnosis_session), signer)
        readme = self._build_readme(
            artifact_file_name=artifact.file_name,
            fingerprint=identity.public_key_fingerprint,
            next_update_at=revocations["next_update_at"],
        )

        output = io.BytesIO()
        with ZipFile(output, mode="w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr(artifact.file_name, artifact.content_text.encode("utf-8"))
            archive.writestr("artifact-manifest.json", self._pretty_json(manifest))
            archive.writestr("runtime-manifest.json", self._pretty_json(runtime_manifest))
            archive.writestr("trust-store.json", self._pretty_json(trust_store))
            archive.writestr("revocations.json", self._pretty_json(revocations))
            self._write_executable(archive, GO_RUNTIME_FILE_NAME, go_runtime)
            archive.writestr("case.json", self._pretty_json(case_document))
            archive.writestr("README.txt", readme)

        content = output.getvalue()
        return VerificationBundle(
            content=content,
            file_name=f"collector-verification-{artifact.artifact_id}.zip",
            root_fingerprint=identity.public_key_fingerprint,
            revocation_next_update_at=datetime.fromisoformat(revocations["next_update_at"]),
            bundle_sha256=hashlib.sha256(content).hexdigest(),
        )

    @staticmethod
    def _load_resource_bytes(file_name: str) -> bytes:
        """读取随验证包分发的二进制离线运行时资源。"""

        resources_root = (Path(__file__).resolve().parents[2] / "resources").resolve()
        resource_path = resources_root.joinpath(*Path(file_name).parts)
        if resources_root not in resource_path.resolve().parents:
            raise DiagnosisError(
                code="COLLECTOR_RESOURCE_UNAVAILABLE",
                message="Collector 二进制资源路径不安全",
                http_status=503,
                details={"file_name": file_name},
            )
        try:
            return resource_path.read_bytes()
        except OSError as exc:
            raise DiagnosisError(
                code="COLLECTOR_RESOURCE_UNAVAILABLE",
                message="Collector 二进制资源不可用",
                http_status=503,
                details={"file_name": file_name},
            ) from exc

    @staticmethod
    def _write_executable(archive: ZipFile, file_name: str, content: bytes) -> None:
        """写入带 Unix 0755 权限的 Go 运行时，常见 unzip 解压后可直接执行。"""

        info = ZipInfo(file_name)
        info.create_system = 3
        info.compress_type = ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, content)

    @staticmethod
    def _pretty_json(document: dict[str, Any]) -> str:
        """生成便于人工审计的 UTF-8 JSON。"""

        return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    @staticmethod
    def _build_case_document(session: DiagnosisSession) -> dict[str, Any]:
        """将诊断会话映射为离线打包所需的工单上下文（case.json）。

        字段形状与 Go 离线运行时的证据清单契约一致；
        incident_timezone 仅供运行时取默认时区，Worker 不读取。
        targets 只保留 type/id（客户侧数据最小化）。
        """

        targets: list[dict[str, str]] = [
            {"type": str(obj["type"]), "id": str(obj["id"])}
            for obj in (session.affected_objects or [])
            if isinstance(obj, dict) and obj.get("type") and obj.get("id")
        ]
        if not targets:
            targets = [
                {"type": "node", "id": str(obj["source_node"])}
                for obj in (session.affected_objects or [])
                if isinstance(obj, dict) and obj.get("source_node")
            ]
        if not targets:
            raise DiagnosisError(
                code="CASE_TARGETS_UNAVAILABLE",
                message="诊断会话既没有故障对象也没有采集执行节点，无法生成离线工单上下文",
                http_status=409,
            )
        return {
            "case_id": session.case_id,
            "session_id": str(session.session_id),
            "selected_scenario": session.selected_scenario,
            "incident_window": {
                "start": session.incident_start_time.isoformat(),
                "end": session.incident_end_time.isoformat(),
            },
            "incident_timezone": session.incident_timezone,
            "targets": targets,
        }

    @staticmethod
    def _build_readme(*, artifact_file_name: str, fingerprint: str, next_update_at: str) -> str:
        """生成 Go 单程序离线操作说明。"""

        return (
            "HCI 离线诊断 Collector Verification Bundle（采集器验证包）\n"
            "===========================================================\n\n"
            "1. 通过独立可信渠道核对下方 Ed25519 公钥 SHA-256 指纹：\n"
            f"   {fingerprint}\n\n"
            "2. 在 Linux x86_64 客户机解压后，直接执行：\n"
            f"   ./hci-collect-linux-amd64 --expected-root-fingerprint {fingerprint}\n"
            "   若解压工具没有保留权限，先执行 chmod +x hci-collect-linux-amd64。\n"
            "   该静态 Go 程序不依赖 Python、pip、cryptography、OpenSSL、glibc 或网络安装。\n"
            "   它会依次验证自身运行时清单、结构化采集制品、签名 Manifest 和吊销快照，\n"
            "   展示采集范围并等待人工确认，随后直接按 argv 参数数组执行只读命令，不调用\n"
            "   /bin/sh、bash、curl 或 timeout，最后完成 AES-256-GCM 加密打包。\n"
            "   --yes 可跳过范围确认，仅供有审计留痕的自动化场景。建议增加\n"
            "   --cleanup-plaintext，在加密成功后清理清单声明的明文输出。\n\n"
            f"   当前签名结构化采集制品：{artifact_file_name}\n"
            "   若清单包含 HCI API Collector（HCI 接口采集器），执行前设置客户本地 HTTPS 地址\n"
            "   HCI_API_BASE_URL 和短期 HCI_API_TOKEN；令牌只进入进程内存，不写入输出。\n"
            "   若清单包含 Manual Attachment Guide（人工附件采集指引），按 manual-guides 目录\n"
            "   的说明把导出文件放到指定 attachments 路径后再打包。\n\n"
            f"内置吊销清单下次更新时间：{next_update_at}\n"
            "超过该时间后验证器会拒绝继续使用，必须从可信平台下载最新验证包。\n"
        )

    def _require_signer(self) -> ArtifactSigner:
        """缺失签名器时默认拒绝信任材料生成。"""

        if self._signer is None:
            raise DiagnosisError(
                code="ARTIFACT_SIGNER_UNAVAILABLE",
                message="Collector Artifact 签名器尚未配置",
                http_status=503,
            )
        return self._signer

    @staticmethod
    def _require_role(actor: ActorContext) -> None:
        """仅允许诊断控制面角色读取信任材料。"""

        if actor.roles.isdisjoint(TRUST_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取 Collector 信任材料", http_status=403)
