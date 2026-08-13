"""阶段1内存仓储与DataFrame边界。"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Iterator

import pandas as pd

from ..domain.analysis import AnalysisRecord, AnalysisStatus
from ..domain.task_dataset import (
    AnalysisTask,
    Dataset,
    UploadRecord,
    UploadStatus,
)
from ..errors import AppError


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskRepository:
    """进程内任务仓储；任务、上传和Dataset元数据均按task_id隔离。"""

    def __init__(
        self,
        *,
        root_for_task: Callable[[str], str],
        max_tasks: int = 5,
        semantic_model_version: str | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.max_tasks = max_tasks
        self.semantic_model_version = semantic_model_version
        self._root_for_task = root_for_task
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._tasks: dict[str, AnalysisTask] = {}
        self._uploads: dict[str, UploadRecord] = {}
        self._analyses: dict[str, AnalysisRecord] = {}
        self._lock = threading.RLock()

    def new_id(self) -> str:
        value = self._id_factory()
        return str(value)

    def create(self, name: str | None = None) -> AnalysisTask:
        with self._lock:
            active_count = sum(task.status.value != "deleted" for task in self._tasks.values())
            if active_count >= self.max_tasks:
                raise AppError(
                    "TASK_LIMIT_REACHED",
                    f"运行中任务已达到上限（{self.max_tasks}）",
                    409,
                    {"max_tasks": self.max_tasks},
                )
            task_id = self.new_id()
            task = AnalysisTask(
                task_id=task_id,
                name=(name or f"任务-{task_id[:8]}"),
                resource_dir=self._root_for_task(task_id),
                semantic_model_version=self.semantic_model_version,
            )
            self._tasks[task_id] = task
            return task

    def get(self, task_id: str) -> AnalysisTask | None:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None or task.status.value == "deleted":
                return None
            return task

    def require(self, task_id: str) -> AnalysisTask:
        task = self.get(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
        return task

    def list(self) -> list[AnalysisTask]:
        with self._lock:
            return [task for task in self._tasks.values() if task.status.value != "deleted"]

    @asynccontextmanager
    async def task_lock(self, task_id: str) -> Iterator[AnalysisTask]:
        task = self.require(task_id)
        async with task.lock:
            if task.status.value != "active":
                raise AppError("TASK_NOT_AVAILABLE", "任务当前不可用", 409)
            if task.busy:
                raise AppError("TASK_MUTATION_BLOCKED", "任务当前有分析正在执行", 409)
            yield task

    def add_upload(self, upload: UploadRecord) -> None:
        with self._lock:
            task = self.require(upload.task_id)
            if upload.upload_id in self._uploads:
                raise AppError("UPLOAD_ID_CONFLICT", "上传记录ID冲突", 500)
            self._uploads[upload.upload_id] = upload
            task.upload_ids.append(upload.upload_id)
            task.touch()

    def get_upload(self, task_id: str, upload_id: str) -> UploadRecord:
        task = self.require(task_id)
        upload = self._uploads.get(str(upload_id))
        if upload is None or upload.task_id != task.task_id:
            raise AppError("UPLOAD_NOT_FOUND", "上传记录不存在", 404)
        return upload

    def mark_upload_imported(self, task_id: str, upload_id: str) -> None:
        upload = self.get_upload(task_id, upload_id)
        upload.imported = True
        upload.status = UploadStatus.IMPORTED

    def add_dataset_id(self, task_id: str, dataset_id: str) -> None:
        task = self.require(task_id)
        if dataset_id not in task.dataset_ids:
            task.dataset_ids.append(dataset_id)
            task.touch()

    def set_active_dataset(self, task_id: str, dataset_id: str | None) -> None:
        task = self.require(task_id)
        task.active_dataset_id = dataset_id
        task.touch()

    async def begin_delete(self, task_id: str) -> AnalysisTask:
        task = self.require(task_id)
        async with task.lock:
            if task.busy:
                raise AppError("TASK_MUTATION_BLOCKED", "任务当前有操作正在执行", 409)
            task.status = task.status.DELETING
            task.touch()
            return task

    def finalize_delete(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(str(task_id))
            if task is None:
                return
            for upload_id in list(task.upload_ids):
                self._uploads.pop(upload_id, None)
            for analysis_id in list(task.analysis_ids):
                self._analyses.pop(analysis_id, None)
            task.analysis_ids.clear()
            task.conversation.clear()
            task.conversation_summary.clear()
            task.status = task.status.DELETED
            task.touch()
            self._tasks.pop(task.task_id, None)

    def remove_upload(self, upload_id: str) -> None:
        with self._lock:
            upload = self._uploads.pop(upload_id, None)
            if upload is None:
                return
            task = self._tasks.get(upload.task_id)
            if task and upload_id in task.upload_ids:
                task.upload_ids.remove(upload_id)

    def add_analysis_id(self, task_id: str, analysis_id: str, *, max_records: int = 20) -> None:
        task = self.require(task_id)
        if len(task.analysis_ids) >= max_records:
            raise AppError(
                "ANALYSIS_LIMIT_REACHED",
                "任务分析记录已达到上限",
                409,
                {"max_analysis_records": max_records},
            )
        task.analysis_ids.append(str(analysis_id))
        task.touch()

    def add_analysis(self, record: AnalysisRecord, *, max_records: int = 20) -> None:
        with self._lock:
            self.require(record.task_id)
            if record.analysis_id in self._analyses:
                raise AppError("ANALYSIS_ID_CONFLICT", "分析记录ID冲突", 500)
            self.add_analysis_id(record.task_id, record.analysis_id, max_records=max_records)
            self._analyses[record.analysis_id] = record

    def get_analysis(self, task_id: str, analysis_id: str) -> AnalysisRecord:
        self.require(task_id)
        record = self._analyses.get(str(analysis_id))
        if record is None or record.task_id != str(task_id):
            raise AppError("ANALYSIS_NOT_FOUND", "分析记录不存在", 404)
        return record

    def list_analyses(self, task_id: str) -> list[AnalysisRecord]:
        task = self.require(task_id)
        return [self._analyses[item] for item in task.analysis_ids if item in self._analyses]

    def active_analysis(self, task_id: str) -> AnalysisRecord | None:
        for record in self.list_analyses(task_id):
            if record.status in {
                AnalysisStatus.CREATED,
                AnalysisStatus.RUNNING,
                AnalysisStatus.AWAITING_CLARIFICATION,
                AnalysisStatus.CANCEL_REQUESTED,
            }:
                return record
        return None

    def remove_analysis(self, task_id: str, analysis_id: str) -> None:
        with self._lock:
            record = self.get_analysis(task_id, analysis_id)
            self._analyses.pop(record.analysis_id, None)
            task = self.require(task_id)
            if record.analysis_id in task.analysis_ids:
                task.analysis_ids.remove(record.analysis_id)
                task.touch()

    def clear(self) -> None:
        with self._lock:
            self._uploads.clear()
            self._analyses.clear()
            for task in self._tasks.values():
                task.status = task.status.DELETED
            self._tasks.clear()


class DatasetStore:
    """DataFrame唯一存放边界；对外总是返回副本，防止原始Dataset被原地修改。"""

    def __init__(self) -> None:
        self._records: dict[str, Dataset] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self._lock = threading.RLock()

    def add(self, dataset: Dataset, frame: pd.DataFrame) -> None:
        with self._lock:
            if dataset.dataset_id in self._records:
                raise AppError("DATASET_ID_CONFLICT", "Dataset ID冲突", 500)
            self._records[dataset.dataset_id] = dataset
            self._frames[dataset.dataset_id] = frame.copy(deep=True)

    def get(self, dataset_id: str) -> Dataset | None:
        with self._lock:
            return self._records.get(str(dataset_id))

    def require(self, task_id: str, dataset_id: str) -> Dataset:
        dataset = self.get(dataset_id)
        if dataset is None or dataset.task_id != str(task_id):
            raise AppError("DATASET_NOT_FOUND", "Dataset不存在", 404)
        return dataset

    def frame(self, task_id: str, dataset_id: str) -> pd.DataFrame:
        self.require(task_id, dataset_id)
        with self._lock:
            frame = self._frames.get(str(dataset_id))
            if frame is None:
                raise AppError("DATASET_NOT_READY", "Dataset数据尚未就绪", 409)
            return frame.copy(deep=True)

    def list_for_task(self, task_id: str) -> list[Dataset]:
        with self._lock:
            return [item for item in self._records.values() if item.task_id == str(task_id)]

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            dataset_ids = [
                dataset_id
                for dataset_id, dataset in self._records.items()
                if dataset.task_id == str(task_id)
            ]
            for dataset_id in dataset_ids:
                self._records.pop(dataset_id, None)
                self._frames.pop(dataset_id, None)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._frames.clear()
