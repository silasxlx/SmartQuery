from __future__ import annotations

import pandas as pd
import pytest

from excel_agent.application.stage1_services import ProfilingService
from excel_agent.domain.analysis import QueryPlan, QueryStep
from excel_agent.domain.task_dataset import Dataset, DatasetKind, DatasetStatus
from excel_agent.infrastructure.query_engine import PandasQueryExecutor, QueryPlanValidator
from excel_agent.infrastructure.restricted_ast import (
    FormulaError,
    parse_formula,
    validate_metric_formulas,
)
from excel_agent.infrastructure.stage1_semantic import SemanticCatalog


def _dataset_and_bindings():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-02-01", "2024-02-02"]),
            "branch": ["A", "A", "B", "B"],
            "orders": [10, 12, 8, 15],
            "amount": [100.0, 120.0, 80.0, 150.0],
        }
    )
    dataset_id = "dataset-1"
    profile = ProfilingService().profile(frame, dataset_id)
    catalog = SemanticCatalog.from_file("semantic_model/v1.yaml")
    bindings = catalog.bindings_for(
        task_id="task-1", dataset_id=dataset_id, fields=profile.schema.fields
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        task_id="task-1",
        kind=DatasetKind.NORMALIZED,
        display_name="sample",
        source_type="csv",
        source_object="sample.csv",
        parent_dataset_id=None,
        version=1,
        status=DatasetStatus.READY,
        physical_schema=profile.schema,
        profile=profile,
        semantic_bindings=bindings,
    )
    return frame, dataset, catalog, bindings


def test_restricted_formula_evaluates_without_python_execution():
    expression = parse_formula("safe_divide(sum(amount), count(orders))", {"amount", "orders"})
    value = expression.evaluate(
        {
            "amount": pd.Series([100.0, 120.0]),
            "orders": pd.Series([10, 12]),
        }
    )
    assert value == 110.0


@pytest.mark.parametrize(
    "formula",
    [
        "__import__('os').system('whoami')",
        "amount.__class__",
        "amount[0]",
        "lambda x: x",
        "[amount for amount in values]",
        "open('secret.txt')",
        "sum(sum(amount))",
    ],
)
def test_restricted_formula_rejects_unsafe_syntax(formula):
    with pytest.raises(FormulaError):
        parse_formula(formula, {"amount", "values"})


def test_metric_dependency_cycle_is_rejected():
    with pytest.raises(FormulaError, match="循环"):
        validate_metric_formulas({"a": "b + 1", "b": "a + 1"})


def test_metric_formula_unit_and_grain_conflicts_are_rejected():
    with pytest.raises(FormulaError, match="单位"):
        validate_metric_formulas(
            {"derived": "amount + count"},
            field_names=("amount", "count"),
            units={"amount": "元", "count": "笔"},
        )
    with pytest.raises(FormulaError, match="时间粒度"):
        validate_metric_formulas(
            {"derived": "daily + monthly"},
            field_names=("daily", "monthly"),
            grains={"daily": "day", "monthly": "month"},
        )


def test_query_plan_uses_semantic_ids_and_executes_grouped_result():
    frame, dataset, catalog, bindings = _dataset_and_bindings()
    plan = QueryPlan(
        plan_id="plan-1",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="group",
        queries=(
            QueryStep(
                operation="group",
                metric_ids=("amount",),
                dimension_ids=("branch",),
                limit=20,
            ),
        ),
    )
    QueryPlanValidator().validate(
        plan, dataset=dataset, catalog=catalog, bindings=bindings
    )
    output = PandasQueryExecutor().execute(
        plan,
        dataset=dataset,
        frame=frame,
        catalog=catalog,
        bindings=bindings,
    )
    assert output.result["data"][0] == {"branch": "B", "amount": 230.0}
    assert output.result["data"][1] == {"branch": "A", "amount": 220.0}
    assert all("physical" not in str(item).lower() for item in plan.to_dict()["queries"])


def test_query_plan_rejects_unknown_or_unbound_member():
    _, dataset, catalog, bindings = _dataset_and_bindings()
    plan = QueryPlan(
        plan_id="plan-2",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="aggregate",
        queries=(QueryStep(metric_ids=("raw_amount_column",)),),
    )
    with pytest.raises(Exception):
        QueryPlanValidator().validate(
            plan, dataset=dataset, catalog=catalog, bindings=bindings
        )
