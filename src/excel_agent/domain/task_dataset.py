"""阶段1任务与Dataset领域对象。

领域对象只保存可序列化的元数据和状态，不持有DataFrame、文件句柄或模型客户端。
DataFrame由DatasetStore隔离保存，通过dataset_id访问。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


class UploadStatus(StrEnum):
    INSPECTING = "inspecting"
    INSPECTED = "inspected"
    IMPORTED = "imported"
    REJECTED = "rejected"


class DatasetKind(StrEnum):
    RAW = "raw"
    NORMALIZED = "normalized"
    JOIN = "join"


class DatasetStatus(StrEnum):
    LOADING = "loading"
    PROFILING = "profiling"
    NORMALIZING = "normalizing"
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"


class BindingStatus(StrEnum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    SUGGESTED = "suggested"
    REJECTED = "rejected"


@dataclass
class AnalysisTask:
    task_id: str
    name: str
    resource_dir: str
    semantic_model_version: str | None = None
    status: TaskStatus = TaskStatus.ACTIVE
    upload_ids: list[str] = field(default_factory=list)
    dataset_ids: list[str] = field(default_factory=list)
    active_dataset_id: str | None = None
    analysis_ids: list[str] = field(default_factory=list)
    semantic_extensions: list[dict[str, Any]] = field(default_factory=list)
    conversation: list[dict[str, Any]] = field(default_factory=list, repr=False)
    conversation_summary: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    busy: bool = field(default=False, repr=False, compare=False)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status.value,
            "semantic_model_version": self.semantic_model_version,
            "dataset_ids": list(self.dataset_ids),
            "active_dataset_id": self.active_dataset_id,
            "upload_ids": list(self.upload_ids),
            "analysis_ids": list(self.analysis_ids),
            "conversation_summary": dict(self.conversation_summary),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class UploadInspection:
    upload_id: str
    task_id: str
    display_filename: str
    format: str
    size_bytes: int
    objects: list[dict[str, Any]]
    encoding_candidates: list[str] = field(default_factory=list)
    delimiter_candidates: list[str] = field(default_factory=list)
    validation_errors: list[dict[str, Any]] = field(default_factory=list)
    expires_with_task: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "task_id": self.task_id,
            "display_filename": self.display_filename,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "objects": self.objects,
            "encoding_candidates": self.encoding_candidates,
            "delimiter_candidates": self.delimiter_candidates,
            "validation_errors": self.validation_errors,
            "expires_with_task": self.expires_with_task,
        }


@dataclass
class PhysicalField:
    field_id: str
    original_name: str
    normalized_name: str
    physical_type: str
    nullable: bool
    non_null_count: int
    null_ratio: float
    unique_ratio: float
    representative_values: list[Any] = field(default_factory=list)
    is_primary_key_candidate: bool = False
    is_time_candidate: bool = False
    is_dimension_candidate: bool = False
    is_metric_candidate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "original_name": self.original_name,
            "normalized_name": self.normalized_name,
            "physical_type": self.physical_type,
            "nullable": self.nullable,
            "non_null_count": self.non_null_count,
            "null_ratio": self.null_ratio,
            "unique_ratio": self.unique_ratio,
            "representative_values": self.representative_values,
            "is_primary_key_candidate": self.is_primary_key_candidate,
            "is_time_candidate": self.is_time_candidate,
            "is_dimension_candidate": self.is_dimension_candidate,
            "is_metric_candidate": self.is_metric_candidate,
        }


@dataclass
class PhysicalSchema:
    fields: list[PhysicalField] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"fields": [field.to_dict() for field in self.fields]}


@dataclass
class DataProfile:
    row_count: int
    column_count: int
    schema: PhysicalSchema
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "column_count": self.column_count,
            "schema": self.schema.to_dict(),
            "warnings": list(self.warnings),
        }


@dataclass
class NormalizationDecision:
    decision_id: str
    field_id: str
    field_name: str
    kind: str
    message: str
    options: list[str]
    status: str = "pending"
    selected: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "field_id": self.field_id,
            "field_name": self.field_name,
            "kind": self.kind,
            "message": self.message,
            "options": list(self.options),
            "status": self.status,
            "selected": self.selected,
        }


@dataclass
class SemanticBinding:
    binding_id: str
    task_id: str
    dataset_id: str
    semantic_member_id: str
    semantic_member_kind: str
    physical_field_id: str | None
    status: BindingStatus
    source: str
    type_compatible: bool
    candidate_field_ids: list[str] = field(default_factory=list)
    confirmed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "task_id": self.task_id,
            "dataset_id": self.dataset_id,
            "semantic_member_id": self.semantic_member_id,
            "semantic_member_kind": self.semantic_member_kind,
            "physical_field_id": self.physical_field_id,
            "status": self.status.value,
            "source": self.source,
            "type_compatible": self.type_compatible,
            "candidate_field_ids": list(self.candidate_field_ids),
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
        }


@dataclass
class Dataset:
    dataset_id: str
    task_id: str
    kind: DatasetKind
    display_name: str
    source_type: str
    source_object: str | None
    parent_dataset_id: str | None
    version: int
    status: DatasetStatus
    physical_schema: PhysicalSchema
    profile: DataProfile
    normalization_records: list[dict[str, Any]] = field(default_factory=list)
    pending_decisions: list[NormalizationDecision] = field(default_factory=list)
    semantic_bindings: list[SemanticBinding] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self, *, include_profile: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "task_id": self.task_id,
            "kind": self.kind.value,
            "display_name": self.display_name,
            "source_type": self.source_type,
            "source_object": self.source_object,
            "parent_dataset_id": self.parent_dataset_id,
            "version": self.version,
            "status": self.status.value,
            "physical_schema": self.physical_schema.to_dict(),
            "normalization_records": list(self.normalization_records),
            "pending_decisions": [item.to_dict() for item in self.pending_decisions],
            "semantic_bindings": [item.to_dict() for item in self.semantic_bindings],
            "created_at": self.created_at.isoformat(),
        }
        if include_profile:
            result["profile"] = self.profile.to_dict()
        return result


@dataclass
class UploadRecord:
    upload_id: str
    task_id: str
    display_filename: str
    suffix: str
    path: str
    inspection: UploadInspection
    status: UploadStatus = UploadStatus.INSPECTED
    imported: bool = False
