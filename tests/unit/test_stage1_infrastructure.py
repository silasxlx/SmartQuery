from __future__ import annotations

import pandas as pd
import pytest

from excel_agent.domain.task_dataset import UploadInspection, UploadRecord
from excel_agent.errors import AppError
from excel_agent.infrastructure.stage1_data_sources import (
    CsvDataSource,
    XlsxDataSource,
    data_source_for_suffix,
)
from excel_agent.infrastructure.stage1_repositories import DatasetStore, TaskRepository


def test_csv_delimiter_ambiguity_requires_explicit_choice(tmp_path):
    path = tmp_path / "ambiguous.csv"
    path.write_text("name;value\nA,west;1\n", encoding="utf-8")
    source = CsvDataSource()
    inspection = source.inspect(path)
    assert ";" in inspection.delimiter_candidates
    assert len(inspection.delimiter_candidates) > 1
    with pytest.raises(AppError) as error:
        source.load(path)
    assert error.value.code == "CSV_DELIMITER_CONFIRMATION_REQUIRED"
    frame = source.load(path, delimiter=";")
    assert list(frame.columns) == ["name", "value"]


def test_xlsx_sheet_selection_and_sheet_limit(tmp_path):
    path = tmp_path / "book.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame({"value": [1]}).to_excel(writer, sheet_name="one", index=False)
        pd.DataFrame({"value": [2]}).to_excel(writer, sheet_name="two", index=False)
    with pytest.raises(AppError) as error:
        XlsxDataSource().load(path)
    assert error.value.code == "SHEET_SELECTION_REQUIRED"
    assert XlsxDataSource().load(path, object_name="two")["value"].tolist() == [2]
    with pytest.raises(AppError) as limit_error:
        XlsxDataSource(max_sheets=1).inspect(path)
    assert limit_error.value.code == "DATASET_SHEET_LIMIT_EXCEEDED"


def test_unknown_data_source_and_invalid_csv_encoding(tmp_path):
    with pytest.raises(AppError) as error:
        data_source_for_suffix(".xls")
    assert error.value.code == "UPLOAD_FORMAT_UNSUPPORTED"
    path = tmp_path / "broken.csv"
    path.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(AppError) as encoding_error:
        CsvDataSource().inspect(path)
    assert encoding_error.value.code == "CSV_ENCODING_UNSUPPORTED"


def test_task_repository_upload_ownership_and_cleanup(tmp_path):
    repository = TaskRepository(
        root_for_task=lambda task_id: str(tmp_path / f"task-{task_id}"),
    )
    first = repository.create("one")
    second = repository.create("two")
    inspection = UploadInspection(
        upload_id="upload-1",
        task_id=first.task_id,
        display_filename="sample.csv",
        format="csv",
        size_bytes=1,
        objects=[],
    )
    repository.add_upload(
        UploadRecord(
            upload_id="upload-1",
            task_id=first.task_id,
            display_filename="sample.csv",
            suffix=".csv",
            path=str(tmp_path / "upload.csv"),
            inspection=inspection,
        )
    )
    assert repository.get_upload(first.task_id, "upload-1").inspection.upload_id == "upload-1"
    with pytest.raises(AppError) as foreign:
        repository.get_upload(second.task_id, "upload-1")
    assert foreign.value.code == "UPLOAD_NOT_FOUND"
    repository.mark_upload_imported(first.task_id, "upload-1")
    repository.remove_upload("upload-1")
    for index in range(20):
        repository.add_analysis_id(first.task_id, f"analysis-{index}")
    with pytest.raises(AppError) as analysis_limit:
        repository.add_analysis_id(first.task_id, "analysis-overflow")
    assert analysis_limit.value.code == "ANALYSIS_LIMIT_REACHED"
    repository.clear()
    assert repository.list() == []


def test_dataset_store_returns_copy_and_deletes_task_data():
    from excel_agent.domain.task_dataset import (
        DataProfile,
        Dataset,
        DatasetKind,
        DatasetStatus,
        PhysicalSchema,
    )

    frame = pd.DataFrame({"value": [1, 2]})
    profile = DataProfile(2, 1, PhysicalSchema([]))
    dataset = Dataset(
        dataset_id="dataset-1",
        task_id="task-1",
        kind=DatasetKind.RAW,
        display_name="raw",
        source_type="csv",
        source_object=None,
        parent_dataset_id=None,
        version=1,
        status=DatasetStatus.READY,
        physical_schema=PhysicalSchema([]),
        profile=profile,
    )
    store = DatasetStore()
    store.add(dataset, frame)
    returned = store.frame("task-1", "dataset-1")
    returned.loc[0, "value"] = 999
    assert store.frame("task-1", "dataset-1").loc[0, "value"] == 1
    store.delete_task("task-1")
    assert store.get("dataset-1") is None
