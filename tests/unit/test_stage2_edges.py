from __future__ import annotations

import ast
import asyncio
import subprocess
import sys

import pandas as pd
import pytest

from excel_agent.application.stage1_services import ProfilingService
from excel_agent.domain.analysis import QueryFilter, QueryPlan, QueryStep
from excel_agent.domain.task_dataset import Dataset, DatasetKind, DatasetStatus
from excel_agent.errors import AppError
from excel_agent.infrastructure.embedding import LocalHashEmbeddingProvider
from excel_agent.infrastructure.join_engine import execute_safe_join, suggest_join
from excel_agent.infrastructure.model_provider import (
    MockModelProvider,
    ModelProviderCapabilities,
    ModelProviderRegistry,
    SemanticResolutionPayload,
    structured_call,
)
from excel_agent.infrastructure.query_engine import (
    PandasQueryExecutor,
    QueryPlanValidator,
    chart_from_evidence,
)
from excel_agent.infrastructure.restricted_ast import FormulaError, parse_formula
from excel_agent.infrastructure.stage1_semantic import SemanticCatalog
from excel_agent.infrastructure.task_knowledge import TaskKnowledgeStore


def _objects():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-02-01", "2024-02-02"]),
            "branch": ["A", "A", "B", "B"],
            "orders": [10, 12, 8, 15],
            "amount": [100.0, 120.0, 80.0, 150.0],
        }
    )
    dataset_id = "d-edge"
    profile = ProfilingService().profile(frame, dataset_id)
    catalog = SemanticCatalog.from_file("semantic_model/v1.yaml")
    bindings = catalog.bindings_for(
        task_id="t-edge", dataset_id=dataset_id, fields=profile.schema.fields
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        task_id="t-edge",
        kind=DatasetKind.NORMALIZED,
        display_name="edge",
        source_type="csv",
        source_object=None,
        parent_dataset_id=None,
        version=1,
        status=DatasetStatus.READY,
        physical_schema=profile.schema,
        profile=profile,
        semantic_bindings=bindings,
    )
    return frame, dataset, catalog, bindings


def test_restricted_ast_operations_and_safe_divide():
    context = {"x": 10, "y": 2, "series": pd.Series([1, 2, 3])}
    assert parse_formula("x + y * 2", context).evaluate(context) == 14
    assert parse_formula("x > y", context).evaluate(context) is True
    assert parse_formula("x if x > y else y", context).evaluate(context) == 10
    assert parse_formula("-x", context).evaluate(context) == -10
    assert parse_formula("min(series)", context).evaluate(context) == 1
    assert parse_formula("max(series)", context).evaluate(context) == 3
    assert parse_formula("mean(series)", context).evaluate(context) == 2
    assert parse_formula("count_distinct(series)", context).evaluate(context) == 3
    assert parse_formula("safe_divide(x, 0)", context).evaluate(context) is None
    with pytest.raises(FormulaError):
        parse_formula("sum(x, y)", context)


@pytest.mark.parametrize("formula", ["'text'", "unknown", "[x]", "x and y", "x ** y"])
def test_restricted_ast_rejects_non_whitelisted_nodes(formula):
    with pytest.raises(FormulaError):
        parse_formula(formula, {"x", "y"})


def test_restricted_ast_comparisons_and_scalar_aggregates():
    context = {"x": 10, "y": 2, "empty": None}
    assert parse_formula("x == y", context).evaluate(context) is False
    assert parse_formula("x != y", context).evaluate(context) is True
    assert parse_formula("x < y", context).evaluate(context) is False
    assert parse_formula("x <= y", context).evaluate(context) is False
    assert parse_formula("x >= y", context).evaluate(context) is True
    assert parse_formula("count(empty)", context).evaluate(context) == 0
    assert parse_formula("count_distinct(x)", context).evaluate(context) == 1
    with pytest.raises(FormulaError):
        parse_formula("sum(x, y)", context)
    with pytest.raises(FormulaError):
        parse_formula("sum(x, key=1)", context)


def test_restricted_ast_series_division_and_scalar_validation_errors():
    series_context = {"left": pd.Series([2.0, 4.0]), "right": pd.Series([1.0, 0.0])}
    divided = parse_formula("safe_divide(left, right)", series_context).evaluate(series_context)
    assert divided.iloc[0] == 2.0
    assert pd.isna(divided.iloc[1])
    assert parse_formula("count(3)", {"x": 1}).evaluate({"x": 1}) == 1
    assert parse_formula("count_distinct(3)", {"x": 1}).evaluate({"x": 1}) == 1
    with pytest.raises(FormulaError):
        parse_formula("'bad'", set()).evaluate({})
    with pytest.raises(FormulaError):
        parse_formula("x + y", {"x", "y"}).evaluate({"x": "bad", "y": 1})
    with pytest.raises(FormulaError):
        parse_formula("unknown(x)", {"x"})


def test_query_executor_filters_details_trend_ratio_and_calculation():
    frame, dataset, catalog, bindings = _objects()
    executor = PandasQueryExecutor()
    for operator, value in (
        ("==", "A"),
        ("!=", "B"),
        (">", 100),
        ("<", 200),
        (">=", 100),
        ("<=", 100),
        ("contains", "A"),
        ("startswith", "A"),
        ("endswith", "A"),
        ("between", {"start": "2024-01-01", "end": "2024-01-31"}),
    ):
        metric = (
            "amount"
            if operator not in {"contains", "startswith", "endswith", "==", "!="}
            else "branch"
        )
        plan = QueryPlan(
            plan_id=f"filter-{operator}",
            task_id=dataset.task_id,
            dataset_id=dataset.dataset_id,
            semantic_model_version=catalog.model.version,
            intent="aggregate",
            queries=(
                QueryStep(
                    metric_ids=("amount",),
                    filters=(QueryFilter(metric, operator, value),),
                ),
            ),
        )
        QueryPlanValidator().validate(plan, dataset=dataset, catalog=catalog, bindings=bindings)
        output = executor.execute(
            plan, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
        )
        assert "data" in output.result

    detail = QueryPlan(
        plan_id="detail",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="detail",
        queries=(
            QueryStep(
                operation="detail",
                metric_ids=("amount",),
                dimension_ids=("branch",),
                limit=2,
            ),
        ),
    )
    detail_out = executor.execute(
        detail, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
    )
    assert detail_out.result["returned_rows"] == 2

    trend = QueryPlan(
        plan_id="trend",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="trend",
        queries=(
            QueryStep(
                operation="trend",
                metric_ids=("amount",),
                dimension_ids=("date",),
                time_grain="month",
            ),
        ),
    )
    trend_out = executor.execute(
        trend, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
    )
    assert {row["date"] for row in trend_out.result["data"]} == {"2024-01", "2024-02"}

    trend_by_branch = QueryPlan(
        plan_id="trend-by-branch",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="trend",
        queries=(
            QueryStep(
                operation="trend",
                metric_ids=("amount",),
                dimension_ids=("date", "branch"),
                time_grain="month",
            ),
        ),
    )
    QueryPlanValidator().validate(
        trend_by_branch, dataset=dataset, catalog=catalog, bindings=bindings
    )
    trend_by_branch_out = executor.execute(
        trend_by_branch,
        dataset=dataset,
        frame=frame,
        catalog=catalog,
        bindings=bindings,
    )
    assert trend_by_branch_out.result["data"][0]["date"] == "2024-01"
    assert trend_by_branch_out.result["data"][0]["branch"] == "A"

    pipeline = QueryPlan(
        plan_id="pipeline",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="aggregate",
        queries=(
            QueryStep(operation="filter", metric_ids=("amount",), dimension_ids=("branch",)),
            QueryStep(operation="aggregate", metric_ids=("amount",), dimension_ids=("branch",)),
        ),
    )
    QueryPlanValidator().validate(pipeline, dataset=dataset, catalog=catalog, bindings=bindings)
    pipeline_out = executor.execute(
        pipeline, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
    )
    assert pipeline_out.result["data"][0] == {"branch": "A", "amount": 220.0}

    ratio = QueryPlan(
        plan_id="ratio",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="ratio",
        queries=(
            QueryStep(
                operation="ratio",
                metric_ids=("amount",),
                dimension_ids=("branch",),
            ),
        ),
    )
    ratio_out = executor.execute(
        ratio, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
    )
    ratios = {row["branch"]: row["amount_ratio"] for row in ratio_out.result["data"]}
    assert ratios["A"] == pytest.approx(48.888889, rel=1e-5)
    assert ratios["B"] == pytest.approx(51.111111, rel=1e-5)

    calc = QueryPlan(
        plan_id="calc",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="aggregate",
        queries=(QueryStep(metric_ids=("amount",)),),
        calculations=({"id": "double_amount", "formula": "amount * 2"},),
    )
    calc_out = executor.execute(
        calc, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
    )
    assert calc_out.result["data"][0]["double_amount"] == 900.0


def test_query_executor_supports_metric_aggregation_overrides():
    frame, dataset, catalog, bindings = _objects()
    executor = PandasQueryExecutor()
    expected = {"mean": 112.5, "min": 80.0, "max": 150.0}
    for aggregation, value in expected.items():
        plan = QueryPlan(
            plan_id=f"aggregation-{aggregation}",
            task_id=dataset.task_id,
            dataset_id=dataset.dataset_id,
            semantic_model_version=catalog.model.version,
            intent="aggregate",
            queries=(QueryStep(metric_ids=("amount",), aggregation=aggregation),),
        )
        QueryPlanValidator().validate(
            plan, dataset=dataset, catalog=catalog, bindings=bindings
        )
        output = executor.execute(
            plan, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
        )
        assert output.result["data"][0]["amount"] == value


def test_query_validator_rejects_limits_grains_filters_and_mismatch():
    _, dataset, catalog, bindings = _objects()
    base = dict(
        plan_id="invalid",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="aggregate",
    )
    with pytest.raises(AppError):
        QueryPlanValidator().validate(
            QueryPlan(**base, queries=(QueryStep(metric_ids=("amount",), limit=1001),)),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )

    with pytest.raises(AppError, match="物理字段"):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=(QueryStep(metric_ids=("amount",)),),
                binding_snapshot=(
                    {"semantic_member_id": "amount", "physical_field_id": "raw_amount"},
                ),
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    with pytest.raises(AppError, match="语义元数据"):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=(QueryStep(metric_ids=("amount",)),),
                binding_snapshot=(
                    {"semantic_member_id": "amount", "physical_fields": ["raw_amount"]},
                ),
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    with pytest.raises(AppError, match="图表"):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=(QueryStep(metric_ids=("amount",)),),
                chart_intent={"physical_field_id": "raw_amount"},
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    with pytest.raises(AppError, match="未允许字段"):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=(QueryStep(metric_ids=("amount",)),),
                chart_intent={"column": "raw_amount"},
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )

    constrained = catalog.members["amount"]
    constrained.extra["allowed_dimensions"] = ["date"]
    with pytest.raises(AppError, match="维度"):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=(
                    QueryStep(metric_ids=("amount",), dimension_ids=("branch",)),
                ),
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    constrained.extra.pop("allowed_dimensions")
    with pytest.raises(AppError):
        QueryPlanValidator().validate(
            QueryPlan(**base, queries=(QueryStep(metric_ids=("amount",), time_grain="hour"),)),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    with pytest.raises(AppError):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=(
                    QueryStep(
                        metric_ids=("amount",),
                        filters=(QueryFilter("amount", "in", 1),),
                    ),
                ),
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )


def test_query_validator_rejects_invalid_operations_plans_and_calculations():
    _, dataset, catalog, bindings = _objects()
    base = dict(
        plan_id="invalid-extra",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="aggregate",
    )
    invalid_steps = [
        QueryStep(operation="unknown", metric_ids=("amount",)),
        QueryStep(operation="trend", metric_ids=("amount",)),
        QueryStep(metric_ids=()),
    ]
    for step in invalid_steps:
        with pytest.raises(AppError):
            QueryPlanValidator().validate(
                QueryPlan(**base, queries=(step,)),
                dataset=dataset,
                catalog=catalog,
                bindings=bindings,
            )
    with pytest.raises(AppError):
        QueryPlanValidator().validate(
            QueryPlan(**base, queries=(QueryStep(metric_ids=("amount",)),), calculations=( {},)),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    with pytest.raises(AppError, match="语义结果"):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=(QueryStep(metric_ids=("amount",)),),
                calculations=({"id": "x", "formula": "raw_amount * 2"},),
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    with pytest.raises(AppError):
        QueryPlanValidator().validate(
            QueryPlan(
                **base,
                queries=tuple(QueryStep(metric_ids=("amount",)) for _ in range(11)),
            ),
            dataset=dataset,
            catalog=catalog,
            bindings=bindings,
        )
    mismatch = QueryPlan(
        **{**base, "task_id": "other"},
        queries=(QueryStep(metric_ids=("amount",)),),
    )
    with pytest.raises(AppError):
        QueryPlanValidator().validate(
            mismatch, dataset=dataset, catalog=catalog, bindings=bindings
        )


def test_query_executor_empty_cancel_compare_zero_and_chart_guards():
    frame, dataset, catalog, bindings = _objects()
    executor = PandasQueryExecutor(step_timeout_seconds=10)
    plan = QueryPlan(
        plan_id="empty",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="aggregate",
        queries=(
            QueryStep(
                metric_ids=("amount",),
                filters=(QueryFilter("branch", "==", "missing"),),
            ),
        ),
    )
    output = executor.execute(
        plan, dataset=dataset, frame=frame, catalog=catalog, bindings=bindings
    )
    assert output.result["data"] == []
    assert "no data" in output.warnings
    with pytest.raises(Exception):
        executor.execute(
            plan,
            dataset=dataset,
            frame=frame,
            catalog=catalog,
            bindings=bindings,
            cancel_check=lambda: True,
        )
    assert executor._time_bucket(frame["date"], "day").iloc[0] == "2024-01-01"
    assert executor._time_bucket(frame["date"], "week").iloc[0] == "2024-W01"
    assert executor._time_bucket(frame["date"], "quarter").iloc[0] == "2024Q1"
    assert executor._time_bucket(frame["date"], "year").iloc[0] == "2024"

    zero_frame = frame.copy()
    zero_frame.loc[zero_frame["date"].dt.month == 1, "amount"] = 0
    comparison = QueryPlan(
        plan_id="compare-zero",
        task_id=dataset.task_id,
        dataset_id=dataset.dataset_id,
        semantic_model_version=catalog.model.version,
        intent="compare",
        queries=(
            QueryStep(
                metric_ids=("amount",),
                filters=(
                    QueryFilter(
                        "date",
                        "between",
                        {"start": "2024-02-01", "end": "2024-02-28"},
                    ),
                ),
            ),
            QueryStep(
                metric_ids=("amount",),
                filters=(
                    QueryFilter(
                        "date",
                        "between",
                        {"start": "2024-01-01", "end": "2024-01-31"},
                    ),
                ),
            ),
        ),
    )
    comparison_out = executor.execute(
        comparison,
        dataset=dataset,
        frame=zero_frame,
        catalog=catalog,
        bindings=bindings,
    )
    assert comparison_out.result["data"][0]["change_rate"] is None
    assert comparison_out.warnings
    chart = chart_from_evidence(
        evidence_result={"data": [{"branch": "A", "amount": 1, "ignored": "x"}]},
        dimension="branch",
        metrics=["amount"],
        title="x" * 300,
    )
    assert chart is not None
    assert len(chart.title) == 200
    assert "ignored" not in chart.data[0]
    assert chart_from_evidence(evidence_result={}, dimension=None, metrics=[], title="none") is None


def test_join_stats_and_safe_join_guards():
    left = pd.DataFrame({"id": [1, 2], "amount": [10, 20]})
    right = pd.DataFrame({"id": [1, 2], "name": ["A", "B"]})
    suggestion = suggest_join(left, right, left_id="l", right_id="r")
    assert suggestion.candidates[0]["match_ratio"] == 1.0
    assert suggestion.candidates[0]["type_compatible"] is True
    assert suggestion.candidates[0]["expected_output_rows"] == 2
    renamed = suggest_join(
        left,
        right.rename(columns={"id": "account_id"}),
        left_id="l",
        right_id="r2",
    )
    assert any(
        item["left_key"] == "id" and item["right_key"] == "account_id"
        for item in renamed.candidates
    )
    joined, info = execute_safe_join(left, right, left_keys=["id"], right_keys=["id"])
    assert len(joined) == 2
    assert info["relation"] == "one_to_one"
    with pytest.raises(AppError, match="many-to-many"):
        execute_safe_join(
            pd.DataFrame({"id": [1, 1]}),
            pd.DataFrame({"id": [1, 1]}),
            left_keys=["id"],
            right_keys=["id"],
        )
    with pytest.raises(AppError, match="字段"):
        execute_safe_join(left, right, left_keys=["missing"], right_keys=["id"])
    with pytest.raises(AppError, match="类型"):
        execute_safe_join(
            pd.DataFrame({"id": [1]}),
            pd.DataFrame({"id": ["1"]}),
            left_keys=["id"],
            right_keys=["id"],
        )
    composite_left = pd.DataFrame({"a": [1, 1], "b": ["x", "y"]})
    composite_right = pd.DataFrame({"a": [1, 1], "b": ["x", "z"]})
    _, composite_info = execute_safe_join(
        composite_left,
        composite_right,
        left_keys=["a", "b"],
        right_keys=["a", "b"],
    )
    assert composite_info["match_ratio"] == 0.5


def test_task_knowledge_chunking_and_isolation(tmp_path):
    store = TaskKnowledgeStore(
        root=tmp_path,
        short_document_tokens=2,
        chunk_tokens=2,
        chunk_overlap_tokens=1,
        min_similarity=0.1,
        max_results=3,
    )
    document = store.add(
        "task-a",
        source_name="rule.md",
        content="# 金额\n金额单位为元。\n更多说明。",
    )
    assert len(document.chunks) >= 1
    assert store.search("task-a", "金额")
    assert store.search("task-b", "金额") == []
    assert store.get("task-a", document.document_id).content
    store.delete("task-a", document.document_id)
    with pytest.raises(AppError):
        store.get("task-a", document.document_id)
    with pytest.raises(AppError, match="任务不存在"):
        store.delete_task("..\\outside")


def test_local_embedding_is_stable_across_processes():
    provider = LocalHashEmbeddingProvider(16)
    current = provider.embed(["金额 unit"])[0]
    code = (
        "from excel_agent.infrastructure.embedding import LocalHashEmbeddingProvider; "
        "print(LocalHashEmbeddingProvider(16).embed(['金额 unit'])[0])"
    )
    other = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert current == pytest.approx(ast.literal_eval(other), rel=0, abs=1e-12)


def test_semantic_relationship_and_verified_question_validation(tmp_path):
    valid = tmp_path / "valid-model.yaml"
    valid.write_text(
        """
version: test
entities:
  - {id: customer, name: 客户, allowed_types: [string]}
  - {id: order, name: 订单, allowed_types: [string]}
relationships:
  - id: customer_orders
    name: 客户订单
    left_entity: customer
    right_entity: order
    left_key: customer_id
    right_key: customer_id
    cardinality: one_to_many
    source_refs: [verified:customer-orders]
verified_questions:
  - {id: q1, question: 客户订单数, expected_intent: aggregate}
""",
        encoding="utf-8",
    )
    catalog = SemanticCatalog.from_file(valid)
    assert catalog.relationship_definitions()[0].extra["cardinality"] == "one_to_many"

    invalid = tmp_path / "cycle-model.yaml"
    invalid.write_text(
        """
version: test
entities:
  - {id: a, name: A, allowed_types: [string]}
  - {id: b, name: B, allowed_types: [string]}
relationships:
  - id: ab
    name: AB
    left_entity: a
    right_entity: b
    left_key: id
    right_key: id
    cardinality: one_to_one
    source_refs: [test]
  - id: ba
    name: BA
    left_entity: b
    right_entity: a
    left_key: id
    right_key: id
    cardinality: one_to_one
    source_refs: [test]
""",
        encoding="utf-8",
    )
    with pytest.raises(AppError, match="循环"):
        SemanticCatalog.from_file(invalid)

    unknown_verified = tmp_path / "unknown-verified.yaml"
    unknown_verified.write_text(
        """
version: test
metrics:
  - id: amount
    name: 金额
    source_refs: [test]
verified_questions:
  - id: q1
    question: unknown
    expected_resolution: {metric_ids: [not_a_metric]}
""",
        encoding="utf-8",
    )
    with pytest.raises(AppError, match="未知语义"):
        SemanticCatalog.from_file(unknown_verified)


class _RetryProvider:
    name = "retry"
    capabilities = ModelProviderCapabilities("native", False, 1)

    def __init__(self):
        self.calls = 0

    async def structured(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("temporary")
        return {"intent": "aggregate", "metric_ids": ["amount"]}

    async def answer(self, **kwargs):
        return "ok"


class _ServerError(Exception):
    status_code = 503


class _ServerRetryProvider(_RetryProvider):
    async def structured(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise _ServerError("temporary 5xx")
        return {"intent": "aggregate", "metric_ids": ["amount"]}


def test_provider_retry_and_registry():
    provider = _RetryProvider()
    result = asyncio.run(
        structured_call(
            provider,
            schema=SemanticResolutionPayload,
            system="system",
            user="question",
            context={},
        )
    )
    assert result.metric_ids == ["amount"]
    assert provider.calls == 2
    server_provider = _ServerRetryProvider()
    server_result = asyncio.run(
        structured_call(
            server_provider,
            schema=SemanticResolutionPayload,
            system="system",
            user="question",
            context={},
        )
    )
    assert server_result.metric_ids == ["amount"]
    assert server_provider.calls == 2
    registry = ModelProviderRegistry({"mock": MockModelProvider()}, "mock")
    assert registry.active_provider().name == "mock"


def test_structured_resolution_rejects_physical_field_output():
    class PhysicalFieldProvider:
        name = "physical-field"
        capabilities = ModelProviderCapabilities("native", False)

        async def structured(self, **kwargs):
            return {"intent": "aggregate", "metric_ids": ["amount"], "physical_field_id": "raw"}

        async def answer(self, **kwargs):
            return ""

    with pytest.raises(AppError, match="structured output invalid"):
        asyncio.run(
            structured_call(
                PhysicalFieldProvider(),
                schema=SemanticResolutionPayload,
                system="system",
                user="question",
                context={},
            )
        )
