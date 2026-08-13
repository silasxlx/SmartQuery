from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from excel_agent.application.stage1_services import (
    NormalizationService,
    ProfilingService,
    Stage1Limits,
    Stage1Service,
    _json_value,
)
from excel_agent.domain.task_dataset import BindingStatus
from excel_agent.errors import AppError
from excel_agent.infrastructure.stage1_repositories import DatasetStore, TaskRepository
from excel_agent.infrastructure.stage1_semantic import SemanticCatalog
from excel_agent.security import TempResourceManager


def test_task_repository_enforces_five_task_limit(tmp_path):
    repository = TaskRepository(
        root_for_task=lambda task_id: str(tmp_path / f"task-{task_id}"),
        max_tasks=5,
    )
    for index in range(5):
        repository.create(f"task-{index}")
    with pytest.raises(AppError) as error:
        repository.create("overflow")
    assert error.value.code == "TASK_LIMIT_REACHED"


def test_profile_and_normalization_are_deterministic():
    frame = pd.DataFrame(
        {
            " 日期 ": ["2024-01-01", "2024-01-02"],
            "金额": ["1,000", "2,000"],
            "网点": [" A ", "B"],
        }
    )
    normalized, records, decisions = NormalizationService().normalize(frame, "dataset-1")
    profile = ProfilingService().profile(normalized, "dataset-1")
    assert list(normalized.columns) == ["日期", "金额", "网点"]
    assert normalized["金额"].tolist() == [1000, 2000]
    assert not decisions
    assert {item["rule"] for item in records} == {"date_parse", "numeric_parse"}
    assert profile.schema.fields[0].is_time_candidate is True


def test_profile_handles_boolean_empty_and_safe_json_values(monkeypatch):
    frame = pd.DataFrame({"flag": [True, False], "empty": [None, None]})
    profile = ProfilingService().profile(frame, "dataset-1")
    assert profile.schema.fields[0].physical_type == "boolean"
    assert profile.schema.fields[1].non_null_count == 0
    assert _json_value(None) is None
    assert _json_value(pd.NA) is None
    assert _json_value(pd.Timestamp("2024-01-01")) == "2024-01-01T00:00:00"
    assert _json_value(float("nan")) is None
    assert _json_value(pd.Series([1, 2])) == "0    1\n1    2\ndtype: int64"

    original = pd.to_datetime
    calls = {"count": 0}

    def fallback_to_datetime(*args, **kwargs):
        if kwargs.get("format") == "mixed" and calls["count"] == 0:
            calls["count"] += 1
            raise TypeError("format unsupported")
        return original(*args, **kwargs)

    monkeypatch.setattr(pd, "to_datetime", fallback_to_datetime)
    profile = ProfilingService().profile(pd.DataFrame({"date": ["2024-01-01"]}), "dataset-2")
    assert profile.schema.fields[0].is_time_candidate is True


def test_ambiguous_percent_requires_confirmation():
    frame = pd.DataFrame({"转化率": ["10%", "20%"]})
    _, _, decisions = NormalizationService().normalize(frame, "dataset-1")
    assert len(decisions) == 1
    assert decisions[0].kind == "percent_scale"
    normalized, _, confirmed = NormalizationService().normalize(
        frame,
        "dataset-1",
        confirmations={decisions[0].decision_id: "0-100"},
    )
    assert not confirmed
    assert normalized["转化率"].tolist() == [10, 20]


def test_money_unit_requires_confirmation_then_converts():
    frame = pd.DataFrame({"金额万元": ["1", "2"]})
    normalizer = NormalizationService()
    _, _, decisions = normalizer.normalize(frame, "dataset-1")
    assert decisions[0].kind == "money_unit"
    normalized, records, confirmed = normalizer.normalize(
        frame,
        "dataset-1",
        confirmations={decisions[0].decision_id: "万元"},
    )
    assert not confirmed
    assert normalized["金额万元"].tolist() == [10000, 20000]
    assert records[0]["rule"] == "money_unit"


def test_ambiguous_date_confirmation_supports_day_first():
    frame = pd.DataFrame({"日期": ["01/02/2024", "03/04/2024"]})
    normalizer = NormalizationService()
    _, _, decisions = normalizer.normalize(frame, "dataset-1")
    assert decisions[0].kind == "date_format"
    normalized, _, pending = normalizer.normalize(
        frame,
        "dataset-1",
        confirmations={decisions[0].decision_id: "dayfirst"},
    )
    assert not pending
    assert str(normalized["日期"].iloc[0].date()) == "2024-02-01"


def test_semantic_catalog_binds_only_unique_compatible_fields(tmp_path):
    path = tmp_path / "model.yaml"
    path.write_text(
        """
version: test
dimensions:
  - id: branch
    name: 网点
    aliases: [branch]
    allowed_types: [string]
metrics:
  - id: amount
    name: 金额
    aliases: [amount]
    allowed_types: [number]
    source_refs: [test]
""",
        encoding="utf-8",
    )
    catalog = SemanticCatalog.from_file(path)
    profile = ProfilingService().profile(
        pd.DataFrame({"branch": ["A"], "amount": [1]}),
        "dataset-1",
    )
    bindings = catalog.bindings_for(
        task_id="task-1",
        dataset_id="dataset-1",
        fields=profile.schema.fields,
    )
    assert {item.semantic_member_id for item in bindings} == {"branch", "amount"}
    assert all(item.status == BindingStatus.CONFIRMED for item in bindings)


def test_semantic_model_conflict_fails(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text(
        """
version: test
metrics:
  - id: one
    name: 金额
    aliases: [amount]
    source_refs: [test]
  - id: two
    name: 收入
    aliases: [amount]
    source_refs: [test]
""",
        encoding="utf-8",
    )
    with pytest.raises(AppError) as error:
        SemanticCatalog.from_file(path)
    assert error.value.code == "SEMANTIC_MODEL_INVALID"


@pytest.mark.parametrize(
    "content",
    [
        "entities: []\n",
        "version: v1\nentities: {}\n",
        "version: v1\nentities: [bad]\n",
        "version: v1\nentities: [{id: x}]\n",
        "version: v1\nmetrics: [{id: x, name: x}]\n",
        "version: v1\nmetrics: [{id: x, name: x, source_refs: []}]\n",
        "version: v1\nmetrics: [{id: x, name: x, aliases: {x: y}, source_refs: [x]}]\n",
    ],
)
def test_semantic_model_invalid_shapes_fail(tmp_path, content):
    path = tmp_path / "invalid-shape.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AppError) as error:
        SemanticCatalog.from_file(path)
    assert error.value.code == "SEMANTIC_MODEL_INVALID"


def _service(tmp_path, limits: Stage1Limits | None = None):
    resources = TempResourceManager(tmp_path / "runtime")
    repository = TaskRepository(
        root_for_task=lambda task_id: str(resources.task_directory(task_id)),
    )
    executor = ThreadPoolExecutor(max_workers=2)
    service = Stage1Service(
        repository=repository,
        dataset_store=DatasetStore(),
        semantic_catalog=SemanticCatalog.from_file("semantic_model/v1.yaml"),
        temp_resources=resources,
        executor=executor,
        limits=limits,
    )
    return service, executor, resources


def test_stage1_service_timeout_and_dataset_limits(tmp_path):
    service, executor, resources = _service(
        tmp_path,
        Stage1Limits(operation_timeout_seconds=0.001, max_rows=1),
    )
    try:
        with pytest.raises(AppError) as timeout:
            asyncio.run(service._run_blocking(lambda: (time.sleep(0.05), 1)[1]))
        assert timeout.value.code == "QUERY_TIMEOUT"
        time.sleep(0.1)
        with pytest.raises(AppError) as row_limit:
            service._prepare(
                frame=pd.DataFrame({"value": [1, 2]}),
                task_id="task-1",
                raw_id="raw-1",
                normalized_id="normalized-1",
                source_type="csv",
                source_object=None,
            )
        assert row_limit.value.code == "DATASET_ROW_LIMIT_EXCEEDED"
        service.limits = Stage1Limits(max_columns=1)
        with pytest.raises(AppError) as column_limit:
            service._prepare(
                frame=pd.DataFrame({"one": [1], "two": [2]}),
                task_id="task-1",
                raw_id="raw-1",
                normalized_id="normalized-1",
                source_type="csv",
                source_object=None,
            )
        assert column_limit.value.code == "DATASET_COLUMN_LIMIT_EXCEEDED"
    finally:
        executor.shutdown(wait=True)
        resources.cleanup()


def test_stage1_service_delete_failure_restores_task(tmp_path, monkeypatch):
    service, executor, resources = _service(tmp_path)
    try:
        task = service.repository.create("delete-me")
        resources.task_directory(task.task_id)

        def fail_delete(_: str) -> None:
            raise RuntimeError("storage failure")

        monkeypatch.setattr(service.dataset_store, "delete_task", fail_delete)
        with pytest.raises(RuntimeError):
            asyncio.run(service.delete_task(task.task_id))
        assert service.repository.get(task.task_id).status.value == "active"
    finally:
        executor.shutdown(wait=True)
        resources.cleanup()
