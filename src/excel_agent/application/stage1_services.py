"""阶段1应用服务：任务、上传、Dataset、Profile、规范化和语义绑定。"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..domain.task_dataset import (
    BindingStatus,
    DataProfile,
    Dataset,
    DatasetKind,
    DatasetStatus,
    NormalizationDecision,
    PhysicalField,
    PhysicalSchema,
    SemanticBinding,
    UploadInspection,
    UploadRecord,
    UploadStatus,
)
from ..errors import AppError
from ..infrastructure.join_engine import execute_safe_join
from ..infrastructure.stage1_data_sources import DataSource, data_source_for_suffix
from ..infrastructure.stage1_repositories import DatasetStore, TaskRepository
from ..infrastructure.stage1_semantic import SemanticCatalog
from ..security import (
    TempResourceManager,
    cleanup_path,
    save_upload_stream,
    validate_file_signature,
    validate_upload_filename,
    validate_xlsx_zip,
)

MAX_REPRESENTATIVE_VALUES = 5


@dataclass(frozen=True)
class Stage1Limits:
    max_upload_bytes: int = 10 * 1024 * 1024
    max_rows: int = 50_000
    max_columns: int = 200
    max_sheets: int = 10
    operation_timeout_seconds: float = 30.0


@dataclass
class PreparedDataset:
    raw_frame: pd.DataFrame
    normalized_frame: pd.DataFrame
    raw_profile: DataProfile
    normalized_profile: DataProfile
    normalization_records: list[dict[str, Any]]
    pending_decisions: list[NormalizationDecision]
    semantic_bindings: list[SemanticBinding]


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    return value if isinstance(value, (str, int, float, bool, list, dict)) else str(value)


def _clean_header(value: Any, index: int) -> str:
    text = re.sub(r"[\u0000-\u001f\u007f\u00a0]", " ", str(value or ""))
    text = " ".join(text.strip().split())
    return text or f"column_{index + 1}"


def normalize_headers(columns: list[Any]) -> list[str]:
    counts: dict[str, int] = {}
    normalized: list[str] = []
    for index, column in enumerate(columns):
        base = _clean_header(column, index)
        counts[base] = counts.get(base, 0) + 1
        normalized.append(base if counts[base] == 1 else f"{base}__{counts[base]}")
    return normalized


def _parse_numeric(series: pd.Series) -> tuple[pd.Series, float]:
    text = series.astype("string").str.strip()
    text = text.str.replace(",", "", regex=False)
    text = text.str.replace(r"^[￥$€£]", "", regex=True)
    parsed = pd.to_numeric(text, errors="coerce")
    non_null = int(series.notna().sum())
    success = float(parsed.notna().sum() / non_null) if non_null else 0.0
    return parsed, success


def _date_score(series: pd.Series) -> tuple[pd.Series, float]:
    text = series.astype("string").str.strip()
    try:
        parsed = pd.to_datetime(text, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(text, errors="coerce")
    non_null = int(series.notna().sum())
    success = float(parsed.notna().sum() / non_null) if non_null else 0.0
    return parsed, success


def _physical_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "decimal"
    parsed, score = _parse_numeric(series)
    if score >= 0.99 and parsed.notna().any():
        return "decimal"
    return "string"


class ProfilingService:
    def profile(self, frame: pd.DataFrame, dataset_id: str) -> DataProfile:
        fields: list[PhysicalField] = []
        columns = [str(column) for column in frame.columns]
        normalized_columns = normalize_headers(columns)
        for index, (original_name, normalized_name) in enumerate(
            zip(columns, normalized_columns, strict=True)
        ):
            series = frame.iloc[:, index]
            non_null_count = int(series.notna().sum())
            row_count = len(series)
            null_ratio = 1.0 if row_count == 0 else float(1 - non_null_count / row_count)
            unique_ratio = (
                0.0
                if non_null_count == 0
                else float(series.dropna().nunique(dropna=True) / non_null_count)
            )
            values = [_json_value(item) for item in series.dropna().head(MAX_REPRESENTATIVE_VALUES)]
            parsed_dates, date_score = _date_score(series)
            physical_type = _physical_type(series)
            fields.append(
                PhysicalField(
                    field_id=f"{dataset_id}:field:{index}",
                    original_name=original_name,
                    normalized_name=normalized_name,
                    physical_type=physical_type,
                    nullable=bool(series.isna().any()),
                    non_null_count=non_null_count,
                    null_ratio=round(null_ratio, 6),
                    unique_ratio=round(unique_ratio, 6),
                    representative_values=values,
                    is_primary_key_candidate=(non_null_count > 0 and unique_ratio == 1.0),
                    is_time_candidate=(
                        date_score >= 0.8 and physical_type in {"string", "datetime"}
                    ),
                    is_dimension_candidate=(physical_type in {"string", "boolean"}),
                    is_metric_candidate=(physical_type in {"integer", "decimal"}),
                )
            )
        return DataProfile(
            row_count=int(frame.shape[0]),
            column_count=int(frame.shape[1]),
            schema=PhysicalSchema(fields),
        )


class NormalizationService:
    _ambiguous_date = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")

    def normalize(
        self,
        frame: pd.DataFrame,
        dataset_id: str,
        *,
        confirmations: dict[str, str] | None = None,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]], list[NormalizationDecision]]:
        confirmations = confirmations or {}
        normalized = frame.copy(deep=True)
        normalized.columns = normalize_headers([str(column) for column in frame.columns])
        records: list[dict[str, Any]] = []
        pending: list[NormalizationDecision] = []
        for index, field_name in enumerate(normalized.columns):
            field_id = f"{dataset_id}:field:{index}"
            series = normalized.iloc[:, index]
            if series.dtype == object or pd.api.types.is_string_dtype(series):
                normalized.iloc[:, index] = series.map(
                    lambda item: item.strip() if isinstance(item, str) else item
                )
            current = normalized.iloc[:, index]
            non_null = current.dropna()
            if non_null.empty:
                continue

            field_token = str(field_name).casefold()
            numeric, numeric_score = _parse_numeric(current)
            percent_values = current.astype("string").str.strip()
            has_percent = percent_values.str.endswith("%", na=False).any()
            if has_percent:
                percent_numeric = pd.to_numeric(
                    percent_values.str.rstrip("%").str.replace(",", "", regex=False),
                    errors="coerce",
                )
                percent_score = float(percent_numeric.notna().sum() / len(non_null))
                if percent_score >= 0.99:
                    decision_id = f"{dataset_id}:normalization:{index}:percent_scale"
                    choice = confirmations.get(decision_id)
                    if choice in {"0-1", "0-100"}:
                        values = percent_numeric if choice == "0-100" else percent_numeric / 100
                        normalized[normalized.columns[index]] = values
                        records.append(
                            {
                                "field_id": field_id,
                                "field_name": field_name,
                                "rule": (
                                    "percent_to_decimal" if choice == "0-100" else "percent_numeric"
                                ),
                                "choice": choice,
                                "affected_rows": int(values.notna().sum()),
                                "failed_rows": int(values.isna().sum()),
                            }
                        )
                    else:
                        pending.append(
                            NormalizationDecision(
                                decision_id=decision_id,
                                field_id=field_id,
                                field_name=field_name,
                                kind="percent_scale",
                                message="请确认百分比字段采用0-1还是0-100表示",
                                options=["0-1", "0-100"],
                            )
                        )
                    continue

            if "万元" in field_token or "万" in field_token:
                decision_id = f"{dataset_id}:normalization:{index}:money_unit"
                choice = confirmations.get(decision_id)
                if choice not in {"元", "万元"}:
                    pending.append(
                        NormalizationDecision(
                            decision_id=decision_id,
                            field_id=field_id,
                            field_name=field_name,
                            kind="money_unit",
                            message="请确认金额字段单位",
                            options=["元", "万元"],
                        )
                    )
                    continue
                if numeric_score >= 0.99:
                    values = numeric * (10000 if choice == "万元" else 1)
                    normalized[normalized.columns[index]] = values
                    records.append(
                        {
                            "field_id": field_id,
                            "field_name": field_name,
                            "rule": "money_unit",
                            "choice": choice,
                            "affected_rows": int(values.notna().sum()),
                            "failed_rows": int(values.isna().sum()),
                        }
                    )
                    continue

            date_values, date_score = _date_score(current)
            date_like = "date" in field_token or "time" in field_token or "日期" in field_token
            ambiguous = any(
                isinstance(value, str)
                and self._ambiguous_date.match(value.strip())
                and re.split(r"[/-]", value.strip())[0] != re.split(r"[/-]", value.strip())[1]
                for value in non_null.head(100).tolist()
            )
            if ambiguous and date_score >= 0.8:
                decision_id = f"{dataset_id}:normalization:{index}:date_format"
                choice = confirmations.get(decision_id)
                if choice not in {"dayfirst", "monthfirst"}:
                    pending.append(
                        NormalizationDecision(
                            decision_id=decision_id,
                            field_id=field_id,
                            field_name=field_name,
                            kind="date_format",
                            message="请确认日期格式中的日/月顺序",
                            options=["dayfirst", "monthfirst"],
                        )
                    )
                    continue
                try:
                    date_values = pd.to_datetime(
                        current,
                        errors="coerce",
                        dayfirst=choice == "dayfirst",
                        format="mixed",
                    )
                except (TypeError, ValueError):
                    date_values = pd.to_datetime(
                        current,
                        errors="coerce",
                        dayfirst=choice == "dayfirst",
                    )
                normalized[normalized.columns[index]] = date_values
                records.append(
                    {
                        "field_id": field_id,
                        "field_name": field_name,
                        "rule": "date_parse",
                        "choice": choice,
                        "affected_rows": int(date_values.notna().sum()),
                        "failed_rows": int(date_values.isna().sum()),
                    }
                )
                continue
            if date_score >= 0.99 and (date_like or current.dtype == object):
                normalized[normalized.columns[index]] = date_values
                records.append(
                    {
                        "field_id": field_id,
                        "field_name": field_name,
                        "rule": "date_parse",
                        "affected_rows": int(date_values.notna().sum()),
                        "failed_rows": int(date_values.isna().sum()),
                    }
                )
                continue

            if numeric_score >= 0.99 and current.dtype == object:
                normalized[normalized.columns[index]] = numeric
                records.append(
                    {
                        "field_id": field_id,
                        "field_name": field_name,
                        "rule": "numeric_parse",
                        "affected_rows": int(numeric.notna().sum()),
                        "failed_rows": int(numeric.isna().sum()),
                    }
                )
        return normalized, records, pending


class Stage1Service:
    def __init__(
        self,
        *,
        repository: TaskRepository,
        dataset_store: DatasetStore,
        semantic_catalog: SemanticCatalog,
        temp_resources: TempResourceManager,
        executor: Any,
        limits: Stage1Limits | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.dataset_store = dataset_store
        self.semantic_catalog = semantic_catalog
        self.temp_resources = temp_resources
        self.executor = executor
        self.limits = limits or Stage1Limits()
        self._id_factory = id_factory or repository.new_id
        self.profiler = ProfilingService()
        self.normalizer = NormalizationService()

    def new_id(self) -> str:
        return str(self._id_factory())

    async def _run_blocking(self, func: Callable[[], Any]) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self.executor, func)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self.limits.operation_timeout_seconds
            )
        except asyncio.TimeoutError as exc:

            def consume(done: asyncio.Future[Any]) -> None:
                try:
                    done.result()
                except BaseException:
                    pass

            future.add_done_callback(consume)
            raise AppError("QUERY_TIMEOUT", "Dataset处理超过时间限制", 408) from exc

    async def create_task(self, name: str | None = None) -> dict[str, Any]:
        task = self.repository.create(name)
        task.resource_dir = str(self.temp_resources.task_directory(task.task_id))
        return task.summary()

    def list_tasks(self) -> list[dict[str, Any]]:
        return [task.summary() for task in self.repository.list()]

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self.repository.require(task_id).summary()

    async def delete_task(self, task_id: str) -> None:
        task = await self.repository.begin_delete(task_id)
        try:
            self.dataset_store.delete_task(task.task_id)
            self.temp_resources.cleanup_task(task.task_id)
            self.repository.finalize_delete(task.task_id)
        except Exception:
            task.status = task.status.ACTIVE
            raise

    async def inspect_upload(self, task_id: str, upload: Any) -> dict[str, Any]:
        async with self.repository.task_lock(task_id):
            suffix = validate_upload_filename(upload.filename, allowed_extensions={".xlsx", ".csv"})
            upload_id = self.new_id()
            path = self.temp_resources.create_task_path(task_id, suffix)
            try:
                size = await save_upload_stream(
                    upload,
                    path,
                    max_bytes=self.limits.max_upload_bytes,
                )
                if suffix == ".xlsx":
                    validate_file_signature(path, suffix)
                    validate_xlsx_zip(path)
                elif size == 0:
                    raise AppError("UPLOAD_EMPTY", "上传文件为空", 422)
                source = data_source_for_suffix(suffix, max_sheets=self.limits.max_sheets)
                raw_inspection = await self._run_blocking(lambda: source.inspect(path))
                objects = raw_inspection.objects
                if suffix == ".csv":
                    objects = [
                        {**item, "name": str(upload.filename)} for item in raw_inspection.objects
                    ]
                inspection = UploadInspection(
                    upload_id=upload_id,
                    task_id=str(task_id),
                    display_filename=str(upload.filename),
                    format=raw_inspection.format,
                    size_bytes=size,
                    objects=objects,
                    encoding_candidates=raw_inspection.encoding_candidates,
                    delimiter_candidates=raw_inspection.delimiter_candidates,
                    validation_errors=raw_inspection.validation_errors,
                )
                self.repository.add_upload(
                    UploadRecord(
                        upload_id=upload_id,
                        task_id=str(task_id),
                        display_filename=str(upload.filename),
                        suffix=suffix,
                        path=str(path),
                        inspection=inspection,
                    )
                )
                return inspection.to_dict()
            except Exception:
                cleanup_path(path)
                raise

    def _prepare(
        self,
        *,
        frame: pd.DataFrame,
        task_id: str,
        raw_id: str,
        normalized_id: str,
        source_type: str,
        source_object: str | None,
        confirmations: dict[str, str] | None = None,
    ) -> PreparedDataset:
        if frame.shape[0] > self.limits.max_rows:
            raise AppError(
                "DATASET_ROW_LIMIT_EXCEEDED",
                "Dataset行数超过限制",
                413,
                {"actual": int(frame.shape[0]), "max": self.limits.max_rows},
            )
        if frame.shape[1] > self.limits.max_columns:
            raise AppError(
                "DATASET_COLUMN_LIMIT_EXCEEDED",
                "Dataset列数超过限制",
                413,
                {"actual": int(frame.shape[1]), "max": self.limits.max_columns},
            )
        raw_frame = frame.copy(deep=True)
        raw_profile = self.profiler.profile(raw_frame, raw_id)
        normalized_frame, records, pending = self.normalizer.normalize(
            raw_frame,
            normalized_id,
            confirmations=confirmations,
        )
        normalized_profile = self.profiler.profile(normalized_frame, normalized_id)
        bindings = self.semantic_catalog.bindings_for(
            task_id=task_id,
            dataset_id=normalized_id,
            fields=normalized_profile.schema.fields,
        )
        return PreparedDataset(
            raw_frame=raw_frame,
            normalized_frame=normalized_frame,
            raw_profile=raw_profile,
            normalized_profile=normalized_profile,
            normalization_records=records,
            pending_decisions=pending,
            semantic_bindings=bindings,
        )

    async def create_dataset(
        self,
        task_id: str,
        *,
        upload_id: str,
        object_name: str | None = None,
        encoding: str | None = None,
        delimiter: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        async with self.repository.task_lock(task_id):
            upload_record = self.repository.get_upload(task_id, upload_id)
            if upload_record.imported:
                raise AppError("UPLOAD_ALREADY_IMPORTED", "上传记录已经导入Dataset", 409)
            source = data_source_for_suffix(
                upload_record.suffix,
                max_sheets=self.limits.max_sheets,
            )
            raw_id = self.new_id()
            normalized_id = self.new_id()
            try:
                prepared = await self._run_blocking(
                    lambda: self._prepare_loaded(
                        source,
                        upload_record,
                        object_name,
                        encoding,
                        delimiter,
                        task_id,
                        raw_id,
                        normalized_id,
                    )
                )
                raw_dataset = Dataset(
                    dataset_id=raw_id,
                    task_id=str(task_id),
                    kind=DatasetKind.RAW,
                    display_name=display_name or upload_record.display_filename,
                    source_type=prepared["source_type"],
                    source_object=prepared["source_object"],
                    parent_dataset_id=None,
                    version=1,
                    status=DatasetStatus.READY,
                    physical_schema=prepared["prepared"].raw_profile.schema,
                    profile=prepared["prepared"].raw_profile,
                )
                result: PreparedDataset = prepared["prepared"]
                normalized_dataset = Dataset(
                    dataset_id=normalized_id,
                    task_id=str(task_id),
                    kind=DatasetKind.NORMALIZED,
                    display_name=f"{display_name or upload_record.display_filename}（规范化）",
                    source_type=prepared["source_type"],
                    source_object=prepared["source_object"],
                    parent_dataset_id=raw_id,
                    version=1,
                    status=(
                        DatasetStatus.BLOCKED if result.pending_decisions else DatasetStatus.READY
                    ),
                    physical_schema=result.normalized_profile.schema,
                    profile=result.normalized_profile,
                    normalization_records=result.normalization_records,
                    pending_decisions=result.pending_decisions,
                    semantic_bindings=result.semantic_bindings,
                )
                self.dataset_store.add(raw_dataset, result.raw_frame)
                self.dataset_store.add(normalized_dataset, result.normalized_frame)
                self.repository.add_dataset_id(task_id, raw_id)
                self.repository.add_dataset_id(task_id, normalized_id)
                if normalized_dataset.status == DatasetStatus.READY:
                    self.repository.set_active_dataset(task_id, normalized_id)
                self.repository.mark_upload_imported(task_id, upload_id)
                cleanup_path(Path(upload_record.path))
                return normalized_dataset.to_dict()
            except AppError as exc:
                # Ambiguous CSV/SHEET choices must keep the upload for a retry;
                # parse, limit and signature failures are terminal for upload_id.
                retryable = {
                    "CSV_ENCODING_CONFIRMATION_REQUIRED",
                    "CSV_DELIMITER_CONFIRMATION_REQUIRED",
                    "SHEET_SELECTION_REQUIRED",
                }
                if exc.code not in retryable:
                    cleanup_path(Path(upload_record.path))
                    upload_record.status = UploadStatus.REJECTED
                raise
            except Exception:
                cleanup_path(Path(upload_record.path))
                upload_record.status = UploadStatus.REJECTED
                raise

    def _prepare_loaded(
        self,
        source: DataSource,
        upload_record: UploadRecord,
        object_name: str | None,
        encoding: str | None,
        delimiter: str | None,
        task_id: str,
        raw_id: str,
        normalized_id: str,
    ) -> dict[str, Any]:
        frame = source.load(
            Path(upload_record.path),
            object_name=object_name,
            encoding=encoding,
            delimiter=delimiter,
        )
        prepared = self._prepare(
            frame=frame,
            task_id=task_id,
            raw_id=raw_id,
            normalized_id=normalized_id,
            source_type=upload_record.suffix.lstrip("."),
            source_object=object_name,
        )
        return {
            "prepared": prepared,
            "source_type": upload_record.suffix.lstrip("."),
            "source_object": object_name,
        }

    def list_datasets(self, task_id: str) -> list[dict[str, Any]]:
        self.repository.require(task_id)
        return [
            dataset.to_dict(include_profile=False)
            for dataset in self.dataset_store.list_for_task(task_id)
        ]

    def get_dataset(self, task_id: str, dataset_id: str) -> dict[str, Any]:
        return self.dataset_store.require(task_id, dataset_id).to_dict()

    def preview(self, task_id: str, dataset_id: str, limit: int = 20) -> dict[str, Any]:
        dataset = self.dataset_store.require(task_id, dataset_id)
        if dataset.status != DatasetStatus.READY and dataset.status != DatasetStatus.BLOCKED:
            raise AppError("DATASET_NOT_READY", "Dataset尚未就绪", 409)
        limit = max(1, min(limit, 100))
        frame = self.dataset_store.frame(task_id, dataset_id).head(limit)
        rows = []
        for row in frame.to_dict(orient="records"):
            rows.append({str(key): _json_value(value) for key, value in row.items()})
        return {
            "dataset_id": dataset_id,
            "columns": [str(column) for column in frame.columns],
            "rows": rows,
            "row_count": int(dataset.profile.row_count),
        }

    def profile(self, task_id: str, dataset_id: str) -> dict[str, Any]:
        return self.dataset_store.require(task_id, dataset_id).profile.to_dict()

    async def confirm_normalization(
        self,
        task_id: str,
        dataset_id: str,
        *,
        decision_id: str,
        choice: str,
    ) -> dict[str, Any]:
        async with self.repository.task_lock(task_id):
            blocked = self.dataset_store.require(task_id, dataset_id)
            decision = next(
                (item for item in blocked.pending_decisions if item.decision_id == decision_id),
                None,
            )
            if decision is None:
                raise AppError("NORMALIZATION_DECISION_NOT_FOUND", "规范化决策不存在", 404)
            if choice not in decision.options:
                raise AppError("NORMALIZATION_CHOICE_INVALID", "规范化决策选项无效", 422)
            is_join_dataset = blocked.kind == DatasetKind.JOIN and any(
                item.get("rule") == "safe_join" for item in blocked.normalization_records
            )
            raw_id = blocked.dataset_id if is_join_dataset else blocked.parent_dataset_id
            if raw_id is None:
                raise AppError("DATASET_PARENT_MISSING", "规范化Dataset缺少原始父Dataset", 500)
            raw_frame = self.dataset_store.frame(task_id, raw_id)
            confirmations = {
                item.decision_id: item.selected
                for item in blocked.pending_decisions
                if item.selected
            }
            confirmations[decision_id] = choice
            new_id = self.new_id()
            prepared = await self._run_blocking(
                lambda: self._prepare(
                    frame=raw_frame,
                    task_id=task_id,
                    raw_id=raw_id,
                    normalized_id=new_id,
                    source_type=blocked.source_type,
                    source_object=blocked.source_object,
                    confirmations=confirmations,
                )
            )
            normalization_records = list(prepared.normalization_records)
            if is_join_dataset:
                normalization_records = [
                    item
                    for item in blocked.normalization_records
                    if item.get("rule") == "safe_join"
                ] + normalization_records
            new_dataset = Dataset(
                dataset_id=new_id,
                task_id=str(task_id),
                kind=DatasetKind.JOIN if is_join_dataset else DatasetKind.NORMALIZED,
                display_name=blocked.display_name,
                source_type=blocked.source_type,
                source_object=blocked.source_object,
                parent_dataset_id=raw_id,
                version=blocked.version + 1,
                status=(
                    DatasetStatus.BLOCKED if prepared.pending_decisions else DatasetStatus.READY
                ),
                physical_schema=prepared.normalized_profile.schema,
                profile=prepared.normalized_profile,
                normalization_records=normalization_records,
                pending_decisions=prepared.pending_decisions,
                semantic_bindings=prepared.semantic_bindings,
            )
            self.dataset_store.add(new_dataset, prepared.normalized_frame)
            self.repository.add_dataset_id(task_id, new_id)
            if new_dataset.status == DatasetStatus.READY:
                self.repository.set_active_dataset(task_id, new_id)
            return new_dataset.to_dict()

    async def confirm_binding(
        self,
        task_id: str,
        dataset_id: str,
        *,
        binding_id: str,
        physical_field_id: str | None,
        confirm: bool,
    ) -> dict[str, Any]:
        async with self.repository.task_lock(task_id):
            dataset = self.dataset_store.require(task_id, dataset_id)
            binding = next(
                (item for item in dataset.semantic_bindings if item.binding_id == binding_id),
                None,
            )
            if binding is None:
                raise AppError("SEMANTIC_BINDING_NOT_FOUND", "语义绑定不存在", 404)
            if confirm:
                if physical_field_id is None:
                    raise AppError("PHYSICAL_FIELD_REQUIRED", "确认绑定必须提供物理字段", 422)
                field_ids = {field.field_id for field in dataset.physical_schema.fields}
                if physical_field_id not in field_ids:
                    raise AppError("PHYSICAL_FIELD_NOT_FOUND", "物理字段不存在", 404)
                field = next(
                    field
                    for field in dataset.physical_schema.fields
                    if field.field_id == physical_field_id
                )
                member = self.semantic_catalog.get_member(binding.semantic_member_id)
                if member is not None and not self.semantic_catalog._type_compatible(
                    field.physical_type, member.allowed_types
                ):
                    raise AppError("SEMANTIC_BINDING_TYPE_MISMATCH", "物理字段类型不兼容", 422)
                binding.physical_field_id = physical_field_id
                binding.status = BindingStatus.CONFIRMED
                binding.source = "user"
                binding.type_compatible = True
                binding.confirmed_at = datetime.now().astimezone()
            else:
                binding.status = BindingStatus.REJECTED
                binding.source = "user"
            return binding.to_dict()

    def bindings(self, task_id: str, dataset_id: str | None = None) -> list[dict[str, Any]]:
        self.repository.require(task_id)
        datasets = self.dataset_store.list_for_task(task_id)
        if dataset_id is not None:
            dataset = self.dataset_store.require(task_id, dataset_id)
            datasets = [dataset]
        return [binding.to_dict() for dataset in datasets for binding in dataset.semantic_bindings]

    def semantic_model(self) -> dict[str, Any]:
        return self.semantic_catalog.summary()

    async def create_joined_dataset(
        self,
        task_id: str,
        *,
        left_dataset_id: str,
        right_dataset_id: str,
        left_keys: list[str],
        right_keys: list[str],
        join_type: str = "inner",
        display_name: str = "联表Dataset",
    ) -> dict[str, Any]:
        """用户确认后执行安全联表，并把结果回流为新的不可变Dataset。"""

        async with self.repository.task_lock(task_id):
            self.dataset_store.require(task_id, left_dataset_id)
            self.dataset_store.require(task_id, right_dataset_id)
            left_frame = self.dataset_store.frame(task_id, left_dataset_id)
            right_frame = self.dataset_store.frame(task_id, right_dataset_id)
            joined_frame, join_info = await self._run_blocking(
                lambda: execute_safe_join(
                    left_frame,
                    right_frame,
                    left_keys=left_keys,
                    right_keys=right_keys,
                    join_type=join_type,
                )
            )
            join_info = {
                **join_info,
                "left_dataset_id": left_dataset_id,
                "right_dataset_id": right_dataset_id,
            }
            dataset_id = self.new_id()
            normalized_frame, normalization_records, pending_decisions = (
                self.normalizer.normalize(joined_frame, dataset_id)
            )
            profile = self.profiler.profile(normalized_frame, dataset_id)
            bindings = self.semantic_catalog.bindings_for(
                task_id=task_id,
                dataset_id=dataset_id,
                fields=profile.schema.fields,
            )
            dataset = Dataset(
                dataset_id=dataset_id,
                task_id=str(task_id),
                kind=DatasetKind.JOIN,
                display_name=display_name,
                source_type="joined",
                source_object=None,
                parent_dataset_id=left_dataset_id,
                version=1,
                status=(DatasetStatus.BLOCKED if pending_decisions else DatasetStatus.READY),
                physical_schema=profile.schema,
                profile=profile,
                normalization_records=[
                    {"rule": "safe_join", **join_info},
                    *normalization_records,
                ],
                pending_decisions=pending_decisions,
                semantic_bindings=bindings,
            )
            self.dataset_store.add(dataset, normalized_frame)
            self.repository.add_dataset_id(task_id, dataset_id)
            if dataset.status == DatasetStatus.READY:
                self.repository.set_active_dataset(task_id, dataset_id)
            return {**dataset.to_dict(), "join_info": join_info}
