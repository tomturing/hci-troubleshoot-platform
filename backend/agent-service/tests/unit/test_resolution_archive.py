import io
import tarfile
import zipfile

from shared.resolution import ArchiveKind, inspect_archive


def test_zip_member_is_verified_without_extraction(tmp_path):
    archive_path = tmp_path / "snapshot.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("LOG_dmesg.txt", "ok")
    result = inspect_archive(archive_path, member="LOG_dmesg.txt")
    assert result.kind is ArchiveKind.ZIP
    assert result.readable is True
    assert result.member_exists is True


def test_tar_gz_member_is_verified_and_path_traversal_is_rejected(tmp_path):
    archive_path = tmp_path / "snapshot.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"ok"
        info = tarfile.TarInfo("nested/log.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    result = inspect_archive(archive_path, member="nested/log.txt")
    assert result.kind is ArchiveKind.TAR_GZIP
    assert result.member_exists is True
    blocked = inspect_archive(archive_path, member="../../etc/passwd")
    assert blocked.readable is False
    assert any("traversal" in issue for issue in blocked.issues)
