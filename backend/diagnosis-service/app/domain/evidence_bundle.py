"""Diagnostic Evidence Bundle（诊断证据包）格式和安全解压。"""

import hashlib
import json
import mimetypes
import os
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.errors import DiagnosisError

ALLOWED_TOP_LEVEL = frozenset(
    {
        "case.json",
        "manifest.json",
        "logs",
        "metrics",
        "configs",
        "topology",
        "hardware",
        "changes",
        "tasks",
        "states",
        "commands",
        "exports",
        "captures",
        "attachments",
    }
)
EICAR_MARKER = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR"


class ManifestFile(BaseModel):
    """清单中的文件声明。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=2048)
    original_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=100)
    sensitivity: str = Field(min_length=1, max_length=32)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManifestTimeCoverage(BaseModel):
    """单采集项时间覆盖。"""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_order(self) -> "ManifestTimeCoverage":
        """时间覆盖必须有序。"""

        if self.end < self.start:
            raise ValueError("time_coverage.end 不能早于 start")
        return self


class ManifestCollectionItem(BaseModel):
    """清单中的采集项。"""

    model_config = ConfigDict(extra="forbid")

    collector_id: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern=r"^(success|partial|failed|not_applicable|skipped_by_user|out_of_time_range)$")
    source: str = Field(min_length=1, max_length=255)
    source_timezone: str = Field(min_length=1, max_length=64)
    clock_offset_ms: int = Field(default=0, ge=-86400000, le=86400000)
    time_coverage: ManifestTimeCoverage | None = None
    files: list[ManifestFile] = Field(default_factory=list, max_length=10000)
    exit_code: int | None = None
    failure_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_status(self) -> "ManifestCollectionItem":
        """成功项必须有文件，失败项必须说明原因。"""

        if self.status == "success" and not self.files:
            raise ValueError("success 采集项必须至少包含一个文件")
        if self.status in {"failed", "partial"} and not self.failure_reason:
            raise ValueError("failed/partial 采集项必须提供 failure_reason")
        return self


class EvidenceBundleManifest(BaseModel):
    """manifest.json 的 P0 权威契约。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^1\.\d+$")
    bundle_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(pattern=r"^Q\d{12,13}$")
    session_id: str = Field(min_length=1, max_length=128)
    bundle_type: str = Field(pattern=r"^(initial|supplement|verification)$")
    parent_bundle_id: str | None = Field(default=None, max_length=128)
    selected_scenario: str = Field(min_length=1, max_length=100)
    collection_profile_version: str = Field(min_length=1, max_length=64)
    collection_plan_id: str = Field(min_length=1, max_length=128)
    collector_artifact_version: str = Field(min_length=1, max_length=64)
    collector_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_key_id: str = Field(min_length=1, max_length=128)
    generated_at: datetime
    incident_window: ManifestTimeCoverage
    targets: list[dict[str, Any]] = Field(min_length=1, max_length=256)
    collection_items: list[ManifestCollectionItem] = Field(min_length=1, max_length=10000)
    encryption: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bundle_parent(self) -> "EvidenceBundleManifest":
        """初始包和补采包父子约束。"""

        if self.bundle_type == "supplement" and not self.parent_bundle_id:
            raise ValueError("supplement manifest 必须提供 parent_bundle_id")
        if self.bundle_type == "initial" and self.parent_bundle_id:
            raise ValueError("initial manifest 不能提供 parent_bundle_id")
        paths = [file.path for item in self.collection_items for file in item.files]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest 文件 path 不能重复")
        return self


@dataclass(frozen=True, slots=True)
class ExtractedBundle:
    """安全解压结果。"""

    manifest: EvidenceBundleManifest
    work_dir: Path
    file_count: int
    extracted_bytes: int
    scanned_bytes: int
    security_results: dict[str, Any]


class SafeBundleExtractor:
    """仅允许 gzip tar 普通文件，并逐项验证大小、路径、扫描和哈希。"""

    def __init__(self, *, max_files: int, max_file_bytes: int, max_extracted_bytes: int):
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_extracted_bytes = max_extracted_bytes

    def extract(self, source: Path, work_dir: Path) -> ExtractedBundle:
        """安全解压并验证标准证据包。"""

        with source.open("rb") as magic_source:
            magic = magic_source.read(2)
        if magic != b"\x1f\x8b":
            raise DiagnosisError(code="INVALID_BUNDLE_MAGIC", message="证据包不是 gzip 压缩格式", http_status=422)
        work_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        file_count = 0
        extracted_bytes = 0
        scanned_bytes = 0
        extracted_paths: dict[str, tuple[int, str]] = {}
        try:
            with tarfile.open(source, mode="r|gz") as archive:
                for member in archive:
                    if member.isdir():
                        self._safe_relative_path(member.name)
                        continue
                    if not member.isfile():
                        raise DiagnosisError(
                            code="UNSAFE_BUNDLE_ENTRY",
                            message="证据包包含符号链接、设备或其他非普通文件",
                            http_status=422,
                            details={"path": member.name},
                        )
                    relative = self._safe_relative_path(member.name)
                    file_count += 1
                    extracted_bytes += member.size
                    if file_count > self.max_files:
                        raise DiagnosisError(
                            code="BUNDLE_FILE_COUNT_EXCEEDED", message="证据包文件数量超限", http_status=422
                        )
                    if member.size > self.max_file_bytes:
                        raise DiagnosisError(
                            code="BUNDLE_FILE_TOO_LARGE",
                            message="证据包存在超大单文件",
                            http_status=422,
                            details={"path": member.name},
                        )
                    if extracted_bytes > self.max_extracted_bytes:
                        raise DiagnosisError(
                            code="BUNDLE_EXPANSION_EXCEEDED",
                            message="证据包解压总大小超过限制，疑似压缩炸弹",
                            http_status=422,
                        )
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        raise DiagnosisError(code="BUNDLE_ENTRY_UNREADABLE", message="包内文件不可读", http_status=422)
                    target = work_dir.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    digest = hashlib.sha256()
                    written = 0
                    with target.open("xb") as output:
                        while chunk := source_file.read(1024 * 1024):
                            written += len(chunk)
                            scanned_bytes += len(chunk)
                            if EICAR_MARKER in chunk:
                                raise DiagnosisError(
                                    code="MALWARE_DETECTED",
                                    message="恶意文件扫描命中，证据包已拒绝",
                                    http_status=422,
                                    details={"path": member.name},
                                )
                            digest.update(chunk)
                            output.write(chunk)
                    if written != member.size:
                        raise DiagnosisError(code="BUNDLE_ENTRY_TRUNCATED", message="包内文件长度异常", http_status=422)
                    extracted_paths[relative.as_posix()] = (written, digest.hexdigest())

            manifest_path = work_dir / "manifest.json"
            case_path = work_dir / "case.json"
            if not manifest_path.is_file() or not case_path.is_file():
                raise DiagnosisError(
                    code="BUNDLE_REQUIRED_FILE_MISSING",
                    message="证据包必须包含 manifest.json 和 case.json",
                    http_status=422,
                )
            try:
                manifest = EvidenceBundleManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
                json.loads(case_path.read_text(encoding="utf-8"))
            except (ValidationError, ValueError, UnicodeDecodeError) as exc:
                raise DiagnosisError(
                    code="INVALID_BUNDLE_MANIFEST",
                    message="manifest.json 或 case.json 不符合标准契约",
                    http_status=422,
                    details={"error": str(exc)[:2000]},
                ) from exc

            declared_paths = {
                file.path: (file.size_bytes, file.sha256) for item in manifest.collection_items for file in item.files
            }
            actual_business_paths = {
                path: value for path, value in extracted_paths.items() if path not in {"manifest.json", "case.json"}
            }
            if set(declared_paths) != set(actual_business_paths):
                raise DiagnosisError(
                    code="BUNDLE_FILE_SET_MISMATCH",
                    message="manifest 声明的文件集合与证据包实际文件不一致",
                    http_status=422,
                    details={
                        "missing": sorted(set(declared_paths) - set(actual_business_paths))[:100],
                        "undeclared": sorted(set(actual_business_paths) - set(declared_paths))[:100],
                    },
                )
            mismatches = [
                path for path, expected in declared_paths.items() if expected != actual_business_paths.get(path)
            ]
            if mismatches:
                raise DiagnosisError(
                    code="BUNDLE_FILE_HASH_MISMATCH",
                    message="包内文件大小或 SHA-256 与 manifest 不一致",
                    http_status=422,
                    details={"paths": mismatches[:100]},
                )
            return ExtractedBundle(
                manifest=manifest,
                work_dir=work_dir,
                file_count=file_count,
                extracted_bytes=extracted_bytes,
                scanned_bytes=scanned_bytes,
                security_results={
                    "magic_valid": True,
                    "path_traversal_blocked": True,
                    "symlink_blocked": True,
                    "content_policy_scan": "passed",
                    "eicar_sentinel_scan": "not_detected",
                    "malware_scan_engine": "not_configured",
                    "file_count": file_count,
                    "extracted_bytes": extracted_bytes,
                    "hashes_valid": True,
                    "schema_valid": True,
                },
            )
        except BaseException:
            _clear_directory(work_dir)
            raise

    @staticmethod
    def _safe_relative_path(raw: str) -> PurePosixPath:
        """拒绝绝对路径、反斜杠、空段、点段和未授权顶层目录。"""

        if "\\" in raw or "\x00" in raw:
            raise DiagnosisError(code="UNSAFE_BUNDLE_PATH", message="包内路径不安全", http_status=422)
        path = PurePosixPath(raw)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise DiagnosisError(code="UNSAFE_BUNDLE_PATH", message="包内路径穿越被拒绝", http_status=422)
        if path.parts[0] not in ALLOWED_TOP_LEVEL:
            raise DiagnosisError(
                code="UNSUPPORTED_BUNDLE_DIRECTORY",
                message="包内包含未允许的顶层目录",
                http_status=422,
                details={"path": raw},
            )
        return path


MATCHER_TEXT_MAX_BYTES = 4 * 1024 * 1024


def bounded_structured_data(path: Path, media_type: str) -> dict[str, Any] | list[Any] | None:
    """生成有界结构化索引，不把全量日志复制进 PostgreSQL。"""

    size = path.stat().st_size
    if size > MATCHER_TEXT_MAX_BYTES:
        if media_type.startswith("text/") or path.suffix in {".log", ".txt", ".stdout", ".stderr"}:
            with path.open("rb") as source:
                preview = source.read(64 * 1024).decode("utf-8", errors="replace")
            return {"preview": preview, "truncated": True}
        return None
    if media_type == "application/json" or path.suffix == ".json":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"parse_error": "invalid_json"}
        return value if isinstance(value, (dict, list)) else {"value": value}
    if media_type.startswith("text/") or path.suffix in {".log", ".txt", ".stdout", ".stderr"} or mimetypes.guess_type(path.name)[0] in {"text/plain", "text/csv"}:
        content = path.read_text(encoding="utf-8", errors="replace")
        return {"preview": content, "truncated": False, "indexed_bytes": size}
    return None


def _clear_directory(path: Path) -> None:
    """仅清理 Worker 创建的明确工作目录。"""

    if not path.exists():
        return
    for root, directories, files in os.walk(path, topdown=False):
        for file_name in files:
            Path(root, file_name).unlink(missing_ok=True)
        for directory_name in directories:
            Path(root, directory_name).rmdir()
    path.rmdir()
