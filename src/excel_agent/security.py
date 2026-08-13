"""上传文件与应用临时资源的安全基线。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import BinaryIO, Iterable

from .errors import AppError

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
DEFAULT_MAX_ZIP_ENTRIES = 2_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


def validate_upload_filename(
    filename: str | None,
    *,
    allowed_extensions: Iterable[str] = DEFAULT_ALLOWED_EXTENSIONS,
) -> str:
    if not filename or "\x00" in filename:
        raise AppError("UPLOAD_FILENAME_INVALID", "文件名无效", 400)
    if "/" in filename or "\\" in filename or Path(filename).is_absolute():
        raise AppError("UPLOAD_FILENAME_INVALID", "文件名不得包含路径", 400)
    if filename in {".", ".."} or filename.strip() != filename:
        raise AppError("UPLOAD_FILENAME_INVALID", "文件名无效", 400)
    suffix = Path(filename).suffix.lower()
    normalized = {extension.lower() for extension in allowed_extensions}
    if suffix not in normalized:
        raise AppError("UPLOAD_FORMAT_UNSUPPORTED", "不支持的文件格式", 400)
    # A dangerous final suffix (for example report.xlsx.exe) is rejected by
    # the extension check above; reject a hidden empty stem as well.
    if (
        not Path(filename).stem
        or Path(filename).stem in {".", ".."}
        or filename.startswith(".")
    ):
        raise AppError("UPLOAD_FILENAME_INVALID", "文件名无效", 400)
    return suffix


def validate_xlsx_zip(
    path: Path,
    *,
    max_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> None:
    """检查 XLSX 容器，防止路径穿越和明显压缩炸弹。"""
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > max_entries:
                raise AppError("UPLOAD_ARCHIVE_UNSAFE", "压缩包条目数量超过限制", 400)
            total_size = 0
            for entry in entries:
                entry_path = Path(entry.filename)
                if (
                    entry.filename.startswith(("/", "\\"))
                    or entry_path.is_absolute()
                    or entry_path.drive
                    or ".." in entry_path.parts
                    or any(":" in part for part in entry_path.parts)
                ):
                    raise AppError("UPLOAD_ARCHIVE_UNSAFE", "压缩包包含非法路径", 400)
                total_size += entry.file_size
                if total_size > max_uncompressed_bytes:
                    raise AppError("UPLOAD_ARCHIVE_UNSAFE", "压缩包解压大小超过限制", 400)
    except AppError:
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        raise AppError("UPLOAD_SIGNATURE_INVALID", "文件内容不是有效的工作簿", 400) from exc


def validate_file_signature(path: Path, suffix: str) -> None:
    """校验扩展名对应的最小文件签名，不信任客户端 MIME。"""
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError as exc:
        raise AppError("UPLOAD_SIGNATURE_INVALID", "无法读取上传文件", 400) from exc
    if suffix in {".xlsx", ".xlsm"}:
        if not zipfile.is_zipfile(path):
            raise AppError("UPLOAD_SIGNATURE_INVALID", "文件内容不是有效的工作簿", 400)
    elif suffix == ".xls" and header != bytes.fromhex("D0CF11E0A1B11AE1"):
        raise AppError("UPLOAD_SIGNATURE_INVALID", "文件内容不是有效的工作簿", 400)


class TempResourceManager:
    """只管理应用专用根目录，绝不触碰工作区外其他临时文件。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else Path(tempfile.gettempdir()) / "excel_agent_v2"
        self.root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._owned: set[Path] = set()

    def clean_orphans(self) -> None:
        for child in self.root.iterdir():
            if child.is_dir() and (
                child.name.startswith("resource-") or child.name.startswith("task-")
            ):
                shutil.rmtree(child, ignore_errors=True)

    def create_path(self, suffix: str) -> Path:
        resource_dir = self.root / f"resource-{uuid.uuid4()}"
        resource_dir.mkdir(parents=True, exist_ok=False)
        path = resource_dir / f"upload{suffix.lower()}"
        self._owned.add(resource_dir)
        return path

    def task_directory(self, task_id: str) -> Path:
        """Return an application-owned directory dedicated to one task."""
        safe_task_id = str(task_id).strip()
        if not safe_task_id or any(char in safe_task_id for char in "\\/:\x00"):
            raise ValueError("invalid task id")
        directory = self.root / f"task-{safe_task_id}"
        directory.mkdir(parents=True, exist_ok=True)
        self._owned.add(directory)
        return directory

    def create_task_path(self, task_id: str, suffix: str) -> Path:
        """Create a UUID-named upload path below the task-owned directory."""
        task_dir = self.task_directory(task_id)
        path = task_dir / f"upload-{uuid.uuid4()}{suffix.lower()}"
        self._owned.add(task_dir)
        return path

    def cleanup_task(self, task_id: str) -> None:
        """Clean only the task directory below the application root."""
        task_dir = self.root / f"task-{str(task_id).strip()}"
        resolved = task_dir.resolve()
        if self.root not in resolved.parents or not task_dir.name.startswith("task-"):
            return
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
        self._owned.discard(task_dir)

    def register(self, path: Path) -> None:
        resolved = path.resolve()
        if self.root not in resolved.parents:
            raise ValueError("resource path outside application temp root")
        self._owned.add(resolved.parent)

    def cleanup(self) -> None:
        for resource in list(self._owned):
            if resource.exists() and self.root in resource.resolve().parents:
                shutil.rmtree(resource, ignore_errors=True)
        self._owned.clear()


async def save_upload_stream(
    upload: BinaryIO,
    destination: Path,
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    chunk_size: int = 64 * 1024,
) -> int:
    """分块写入并按实际字节数限制大小。"""
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("wb") as output:
            while True:
                chunk = await upload.read(chunk_size)  # type: ignore[attr-defined]
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AppError("UPLOAD_LIMIT_EXCEEDED", "文件超过大小限制", 413)
                output.write(chunk)
    except asyncio.CancelledError:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return total


def cleanup_path(path: Path | None) -> None:
    if path is None:
        return
    try:
        resolved = path.resolve()
        if resolved.is_file():
            resolved.unlink(missing_ok=True)
        elif resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
        pass
