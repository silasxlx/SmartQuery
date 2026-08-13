from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from excel_agent.errors import AppError
from excel_agent.security import (
    TempResourceManager,
    cleanup_path,
    save_upload_stream,
    validate_file_signature,
    validate_upload_filename,
    validate_xlsx_zip,
)


class AsyncUpload:
    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


class CancelledUpload:
    async def read(self, size: int = -1) -> bytes:
        raise asyncio.CancelledError()


def test_upload_filename_rejects_path_and_double_extension():
    with pytest.raises(AppError) as path_error:
        validate_upload_filename("..\\secret.xlsx")
    assert path_error.value.code == "UPLOAD_FILENAME_INVALID"

    with pytest.raises(AppError) as extension_error:
        validate_upload_filename("report.xlsx.exe")
    assert extension_error.value.code == "UPLOAD_FORMAT_UNSUPPORTED"

    for filename in (None, " report.xlsx ", "..xlsx", ".xlsx"):
        with pytest.raises(AppError):
            validate_upload_filename(filename)


def test_xlsx_signature_and_zip_limits(tmp_path):
    valid = tmp_path / "valid.xlsx"
    with zipfile.ZipFile(valid, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    validate_file_signature(valid, ".xlsx")
    validate_xlsx_zip(valid)

    invalid = tmp_path / "invalid.xlsx"
    invalid.write_bytes(b"not a workbook")
    with pytest.raises(AppError) as error:
        validate_file_signature(invalid, ".xlsx")
    assert error.value.code == "UPLOAD_SIGNATURE_INVALID"


def test_xlsx_path_traversal_is_rejected(tmp_path):
    unsafe = tmp_path / "unsafe.xlsx"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.txt", "bad")
    with pytest.raises(AppError) as error:
        validate_xlsx_zip(unsafe)
    assert error.value.code == "UPLOAD_ARCHIVE_UNSAFE"

    drive_path = tmp_path / "drive-path.xlsx"
    with zipfile.ZipFile(drive_path, "w") as archive:
        archive.writestr("C:\\outside.txt", "bad")
    with pytest.raises(AppError) as drive_error:
        validate_xlsx_zip(drive_path)
    assert drive_error.value.code == "UPLOAD_ARCHIVE_UNSAFE"


def test_archive_entry_and_uncompressed_limits_and_non_xlsx(tmp_path):
    many_entries = tmp_path / "many.xlsx"
    with zipfile.ZipFile(many_entries, "w") as archive:
        archive.writestr("a", "1")
        archive.writestr("b", "2")
    with pytest.raises(AppError):
        validate_xlsx_zip(many_entries, max_entries=1)

    large_entry = tmp_path / "large.xlsx"
    with zipfile.ZipFile(large_entry, "w") as archive:
        archive.writestr("large", "12345")
    with pytest.raises(AppError):
        validate_xlsx_zip(large_entry, max_uncompressed_bytes=1)

    validate_xlsx_zip(tmp_path / "not-xlsx.xls")
    invalid_zip = tmp_path / "broken.xlsx"
    invalid_zip.write_bytes(b"broken")
    with pytest.raises(AppError):
        validate_xlsx_zip(invalid_zip)


def test_ole_signature_and_read_errors(tmp_path):
    valid_xls = tmp_path / "valid.xls"
    valid_xls.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"data")
    validate_file_signature(valid_xls, ".xls")

    invalid_xls = tmp_path / "invalid.xls"
    invalid_xls.write_bytes(b"not-ole")
    with pytest.raises(AppError):
        validate_file_signature(invalid_xls, ".xls")
    with pytest.raises(AppError):
        validate_file_signature(tmp_path / "missing.xlsx", ".xlsx")


def test_chunked_upload_enforces_actual_size_and_cleans(tmp_path):
    asyncio.run(_test_chunked_upload_enforces_actual_size_and_cleans(tmp_path))


async def _test_chunked_upload_enforces_actual_size_and_cleans(tmp_path):
    manager = TempResourceManager(tmp_path / "app-temp")
    destination = manager.create_path(".xlsx")
    with pytest.raises(AppError) as error:
        await save_upload_stream(AsyncUpload(b"0123456789"), destination, max_bytes=5, chunk_size=2)
    assert error.value.code == "UPLOAD_LIMIT_EXCEEDED"
    assert not destination.exists()
    manager.cleanup()
    assert list((tmp_path / "app-temp").glob("resource-*")) == []


def test_cancelled_upload_cleans_file(tmp_path):
    asyncio.run(_test_cancelled_upload_cleans_file(tmp_path))


async def _test_cancelled_upload_cleans_file(tmp_path):
    destination = tmp_path / "cancelled" / "upload.xlsx"
    with pytest.raises(asyncio.CancelledError):
        await save_upload_stream(CancelledUpload(), destination)
    assert not destination.exists()


def test_temp_manager_never_cleans_outside_root(tmp_path):
    manager = TempResourceManager(tmp_path / "app-temp")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    manager.cleanup()
    assert outside.exists()


def test_temp_orphan_cleanup_and_path_registration(tmp_path):
    root = tmp_path / "app-temp"
    orphan = root / "resource-old"
    orphan.mkdir(parents=True)
    (orphan / "stale").write_text("stale", encoding="utf-8")
    manager = TempResourceManager(root)
    manager.clean_orphans()
    assert not orphan.exists()

    owned = manager.create_path(".xlsx")
    manager.register(owned)
    with pytest.raises(ValueError):
        manager.register(tmp_path / "outside.xlsx")
    manager.cleanup()


def test_cleanup_path_handles_none_and_directories(tmp_path):
    cleanup_path(None)
    directory = tmp_path / "owned"
    directory.mkdir()
    cleanup_path(directory)
    assert not directory.exists()
