"""Read-only archive inspection used by LogResolver/CapabilityProbe.

The inspector never extracts or writes an archive.  It only classifies the
container and, when requested, proves that a safe member exists.
"""

from __future__ import annotations

import gzip
import os
import posixpath
import tarfile
import zipfile
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ArchiveKind(StrEnum):
    GZIP = "gzip"
    ZIP = "zip"
    TAR_GZIP = "tar.gz"
    TAR_ZSTD = "tar.zst"
    PLAIN = "plain"
    UNKNOWN = "unknown"


class ArchiveInspection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: ArchiveKind
    readable: bool
    member: str | None = None
    member_exists: bool | None = None
    member_count: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    issues: list[str] = Field(default_factory=list)


def _safe_member(member: str) -> str:
    value = member.replace("\\", "/")
    normalized = posixpath.normpath(value)
    if not value or normalized in {".", ".."} or normalized.startswith("/") or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise ValueError("archive member path traversal is not allowed")
    return normalized


def inspect_archive(path: str | os.PathLike[str], *, member: str | None = None, max_members: int = 100_000) -> ArchiveInspection:
    target = Path(path)
    try:
        stat = target.stat()
    except OSError as exc:
        return ArchiveInspection(path=str(target), kind=ArchiveKind.UNKNOWN, readable=False, issues=[f"stat failed: {exc}"])
    if not target.is_file():
        return ArchiveInspection(path=str(target), kind=ArchiveKind.UNKNOWN, readable=False, size=stat.st_size, mtime_ns=stat.st_mtime_ns, issues=["archive target is not a regular file"])

    try:
        with target.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        return ArchiveInspection(path=str(target), kind=ArchiveKind.UNKNOWN, readable=False, size=stat.st_size, mtime_ns=stat.st_mtime_ns, issues=[f"read failed: {exc}"])

    suffix = target.name.lower()
    kind = ArchiveKind.PLAIN
    if magic[:2] == b"PK" or suffix.endswith(".zip"):
        kind = ArchiveKind.ZIP
    elif suffix.endswith(".tar.zst") or magic[:4] == b"(\xb5/\xfd":
        kind = ArchiveKind.TAR_ZSTD
    elif magic[:2] == b"\x1f\x8b" or suffix.endswith(".gz"):
        kind = ArchiveKind.TAR_GZIP if suffix.endswith(".tar.gz") else ArchiveKind.GZIP

    safe_member = None
    if member is not None:
        try:
            safe_member = _safe_member(member)
        except ValueError as exc:
            return ArchiveInspection(path=str(target), kind=kind, readable=False, member=member, size=stat.st_size, mtime_ns=stat.st_mtime_ns, issues=[str(exc)])

    try:
        if kind is ArchiveKind.PLAIN:
            return ArchiveInspection(path=str(target), kind=kind, readable=True, member=safe_member, member_exists=None, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if kind is ArchiveKind.TAR_ZSTD:
            return ArchiveInspection(path=str(target), kind=kind, readable=False, member=safe_member, size=stat.st_size, mtime_ns=stat.st_mtime_ns, issues=["tar.zst inspection requires a zstd-capable read-only backend"])
        if kind is ArchiveKind.ZIP:
            with zipfile.ZipFile(target) as archive:
                infos = archive.infolist()
                if len(infos) > max_members:
                    raise ValueError("archive member count exceeds limit")
                names = {item.filename.rstrip("/") for item in infos}
                return ArchiveInspection(path=str(target), kind=kind, readable=True, member=safe_member, member_exists=(safe_member in names) if safe_member else None, member_count=len(infos), size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if kind is ArchiveKind.TAR_GZIP:
            with tarfile.open(target, mode="r:gz") as archive:
                members = archive.getmembers()
                if len(members) > max_members:
                    raise ValueError("archive member count exceeds limit")
                names = {item.name.rstrip("/") for item in members}
                return ArchiveInspection(path=str(target), kind=kind, readable=True, member=safe_member, member_exists=(safe_member in names) if safe_member else None, member_count=len(members), size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if kind is ArchiveKind.GZIP:
            with gzip.open(target, "rb") as handle:
                handle.read(1)
            return ArchiveInspection(path=str(target), kind=kind, readable=True, member=safe_member, member_exists=None, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
        return ArchiveInspection(path=str(target), kind=kind, readable=False, member=safe_member, size=stat.st_size, mtime_ns=stat.st_mtime_ns, issues=[f"archive inspection failed: {exc}"])
    return ArchiveInspection(path=str(target), kind=kind, readable=False, member=safe_member, size=stat.st_size, mtime_ns=stat.st_mtime_ns, issues=["unsupported archive kind"])
