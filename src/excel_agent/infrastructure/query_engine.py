"""QueryPlan校验、pandas编译执行和确定性图表规格。"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from ..domain.analysis import ChartSpec, QueryFilter, QueryPlan, QueryStep
from ..domain.task_dataset import Dataset, DatasetStatus, SemanticBinding
from ..errors import AppError
from .restricted_ast import FormulaError, parse_formula
from .stage1_semantic import SemanticCatalog

SUPPORTED_OPERATORS = {
    "==",
    "!=",
    ">",
    "<",
    ">=",
    "<=",
    "contains",
    "startswith",
    "endswith",
    "between",
}
SUPPORTED_OPERATIONS = {
    "detail",
    "filter",
    "aggregate",
    "group",
    "rank",
    "trend",
    "compare",
    "ratio",
}
SUPPORTED_GRAINS = {"day", "week", "month", "quarter", "year"}
SUPPORTED_AGGREGATIONS = {"sum", "count", "count_distinct", "mean", "min", "max"}
_UNSAFE_CHART_KEYS = {
    "column",
    "physical_field_id",
    "javascript",
    "formatter",
    "url",
    "callback",
    "script",
}
_PLAN_BINDING_KEYS = {
    "binding_id",
    "task_id",
    "dataset_id",
    "semantic_member_id",
    "semantic_member_kind",
    "status",
    "source",
    "type_compatible",
}
_CHART_INTENT_KEYS = {"dimension", "metrics", "type", "title", "unit"}


class QueryCancelled(RuntimeError):
    """查询步骤发现协作式取消标记。"""


class QueryTimeout(TimeoutError):
    """查询执行器的受控步骤超时。"""


@dataclass
class ExecutionOutput:
    result: dict[str, Any]
    intermediate_values: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    input_rows: int = 0
    output_rows: int = 0
    timings_ms: dict[str, float] = field(default_factory=dict)


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    return value if isinstance(value, (str, int, float, bool, list, dict)) else str(value)


def _member_map(catalog: SemanticCatalog) -> dict[str, Any]:
    return catalog.members


def _binding_map(bindings: list[SemanticBinding]) -> dict[str, SemanticBinding]:
    return {binding.semantic_member_id: binding for binding in bindings}


def _contains_unsafe_chart_value(value: Any) -> bool:
    if isinstance(value, dict):
        if any(
            str(key).casefold() in _UNSAFE_CHART_KEYS
            or str(key).casefold().startswith("on_")
            for key in value
        ):
            return True
        return any(_contains_unsafe_chart_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_unsafe_chart_value(item) for item in value)
    if isinstance(value, str) and re.search(r"https?://|javascript:", value, re.IGNORECASE):
        return True
    return False


def _contains_physical_binding_reference(value: Any) -> bool:
    if isinstance(value, dict):
        if any(
            str(key).casefold()
            in {"physical_field_id", "candidate_field_ids", "original_name", "normalized_name"}
            for key in value
        ):
            return True
        return any(_contains_physical_binding_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_physical_binding_reference(item) for item in value)
    return False


def _field_name(binding: SemanticBinding, fields: Any) -> str:
    if binding.physical_field_id is None:
        raise AppError("SEMANTIC_BINDING_REQUIRED", "语义成员尚未完成字段绑定", 409)
    for physical_field in fields:
        if physical_field.field_id == binding.physical_field_id:
            return physical_field.normalized_name
    raise AppError("PHYSICAL_FIELD_NOT_FOUND", "语义绑定引用的物理字段不存在", 409)


def _execution_column(frame: pd.DataFrame, semantic_id: str, field_map: dict[str, str]) -> str:
    """Resolve a semantic member to its physical column or a prior step result."""

    physical_name = field_map.get(semantic_id, semantic_id)
    if physical_name in frame.columns:
        return physical_name
    if semantic_id in frame.columns:
        return semantic_id
    return physical_name


class QueryPlanValidator:
    """只允许语义ID进入执行器的确定性校验器。"""

    def validate(
        self,
        plan: QueryPlan,
        *,
        dataset: Dataset,
        catalog: SemanticCatalog,
        bindings: list[SemanticBinding],
        extra_members: dict[str, Any] | None = None,
    ) -> None:
        if plan.task_id != dataset.task_id or plan.dataset_id != dataset.dataset_id:
            raise AppError("QUERY_PLAN_DATASET_MISMATCH", "QueryPlan与Dataset不匹配", 422)
        if dataset.status != DatasetStatus.READY:
            raise AppError("DATASET_NOT_READY", "Dataset尚未完成规范化或绑定", 409)
        if plan.semantic_model_version != catalog.model.version:
            raise AppError("SEMANTIC_MODEL_VERSION_MISMATCH", "语义模型版本不匹配", 409)
        if plan.intent not in SUPPORTED_OPERATIONS:
            raise AppError("QUERY_PLAN_OPERATION_INVALID", "QueryPlan意图不支持", 422)
        if not plan.queries or len(plan.queries) > 10:
            raise AppError("QUERY_PLAN_LIMIT_EXCEEDED", "查询步骤数量超出限制", 422)
        if plan.intent == "compare" and len(plan.queries) < 2:
            raise AppError("QUERY_PLAN_COMPARE_INVALID", "对比查询至少需要两个时间段", 422)
        member_map = {**_member_map(catalog), **(extra_members or {})}
        binding_map = _binding_map(bindings)
        for step in plan.queries:
            if step.operation not in SUPPORTED_OPERATIONS:
                raise AppError("QUERY_PLAN_OPERATION_INVALID", "QueryPlan操作不支持", 422)
            if not step.metric_ids:
                raise AppError("QUERY_PLAN_METRIC_REQUIRED", "QueryPlan requires a metric", 422)
            if not 1 <= step.limit <= 1000:
                raise AppError("QUERY_PLAN_LIMIT_INVALID", "QueryPlan返回数量超出限制", 422)
            if step.time_grain and step.time_grain not in SUPPORTED_GRAINS:
                raise AppError("QUERY_PLAN_GRAIN_INVALID", "时间粒度不支持", 422)
            if step.aggregation and step.aggregation not in SUPPORTED_AGGREGATIONS:
                raise AppError("QUERY_PLAN_AGGREGATION_INVALID", "聚合方式不支持", 422)
            all_ids = list(step.metric_ids) + list(step.dimension_ids)
            for item in step.filters:
                if _contains_physical_binding_reference(item.to_dict()):
                    raise AppError(
                        "QUERY_PLAN_PHYSICAL_FIELD_REFERENCE",
                        "QueryPlan过滤条件不得直接引用物理字段",
                        422,
                    )
                if item.operator not in SUPPORTED_OPERATORS:
                    raise AppError("QUERY_PLAN_FILTER_INVALID", "过滤操作不支持", 422)
                if item.operator == "between" and (
                    not isinstance(item.value, dict)
                    or item.value.get("start") is None
                    or item.value.get("end") is None
                ):
                    raise AppError("QUERY_PLAN_FILTER_INVALID", "between过滤值无效", 422)
                all_ids.append(item.semantic_id)
            if step.sort_metric_id:
                all_ids.append(step.sort_metric_id)
            for member_id in all_ids:
                member = member_map.get(member_id)
                if member is None or member.kind not in {"entity", "dimension", "metric"}:
                    raise AppError("SEMANTIC_MEMBER_NOT_FOUND", "QueryPlan引用了未知语义成员", 422)
                binding = binding_map.get(member_id)
                derived_metric = member.kind == "metric" and bool(member.extra.get("formula"))
                if not derived_metric and (binding is None or binding.status.value != "confirmed"):
                    raise AppError(
                        "SEMANTIC_BINDING_REQUIRED", "QueryPlan需要已确认的语义绑定", 409
                    )
            for metric_id in step.metric_ids:
                if member_map[metric_id].kind != "metric":
                    raise AppError("QUERY_PLAN_METRIC_INVALID", "查询指标必须是指标语义成员", 422)
            for dimension_id in step.dimension_ids:
                if member_map[dimension_id].kind not in {"entity", "dimension"}:
                    raise AppError(
                        "QUERY_PLAN_DIMENSION_INVALID",
                        "查询维度必须是实体或维度语义成员",
                        422,
                    )
            if step.time_grain:
                if not step.dimension_ids:
                    raise AppError(
                        "QUERY_PLAN_TIME_DIMENSION_REQUIRED",
                        "时间粒度查询必须提供时间维度",
                        422,
                    )
                fields_by_id = {
                    field.field_id: field for field in dataset.physical_schema.fields
                }
                has_time_dimension = False
                for dimension_id in step.dimension_ids:
                    binding = binding_map.get(dimension_id)
                    physical = (
                        fields_by_id.get(binding.physical_field_id)
                        if binding is not None
                        else None
                    )
                    if physical is not None and (
                        physical.is_time_candidate
                        or physical.physical_type in {"datetime", "date"}
                    ):
                        has_time_dimension = True
                if not has_time_dimension:
                    raise AppError(
                        "QUERY_PLAN_TIME_DIMENSION_INVALID",
                        "时间粒度只能应用于时间维度",
                        422,
                    )
            for metric_id in step.metric_ids:
                raw_allowed = member_map[metric_id].extra.get("allowed_dimensions", ())
                allowed_dimensions = (
                    (str(raw_allowed),)
                    if isinstance(raw_allowed, str)
                    else tuple(str(item) for item in raw_allowed)
                )
                if allowed_dimensions and any(
                    dimension_id not in allowed_dimensions for dimension_id in step.dimension_ids
                ):
                    raise AppError(
                        "QUERY_PLAN_DIMENSION_NOT_ALLOWED",
                        "指标不允许按当前维度分析",
                        422,
                    )
            if step.sort_metric_id and member_map[step.sort_metric_id].kind != "metric":
                raise AppError("QUERY_PLAN_SORT_INVALID", "排序字段必须是指标", 422)
            if step.operation == "trend" and not step.time_grain:
                raise AppError("QUERY_PLAN_GRAIN_REQUIRED", "趋势查询必须提供时间粒度", 422)
        if len(plan.calculations) > 5:
            raise AppError("QUERY_PLAN_CALCULATION_LIMIT", "后置计算数量超出限制", 422)
        for snapshot in plan.binding_snapshot:
            if not isinstance(snapshot, dict) or _contains_physical_binding_reference(snapshot):
                raise AppError(
                    "QUERY_PLAN_PHYSICAL_FIELD_REFERENCE",
                    "QueryPlan不得直接引用物理字段",
                    422,
                )
            if any(str(key) not in _PLAN_BINDING_KEYS for key in snapshot):
                raise AppError(
                    "QUERY_PLAN_PHYSICAL_FIELD_REFERENCE",
                    "QueryPlan绑定快照只能包含语义元数据",
                    422,
                )
            semantic_id = snapshot.get("semantic_member_id")
            if semantic_id is not None and semantic_id not in member_map:
                raise AppError("SEMANTIC_MEMBER_NOT_FOUND", "绑定快照引用了未知语义成员", 422)
        if plan.chart_intent is not None:
            if not isinstance(plan.chart_intent, dict):
                raise AppError("QUERY_PLAN_CHART_INVALID", "图表意图格式无效", 422)
            if any(str(key) not in _CHART_INTENT_KEYS for key in plan.chart_intent):
                raise AppError("QUERY_PLAN_CHART_INVALID", "图表意图包含未允许字段", 422)
            if _contains_unsafe_chart_value(plan.chart_intent):
                raise AppError(
                    "QUERY_PLAN_CHART_INVALID",
                    "图表意图不得引用物理字段或可执行脚本",
                    422,
                )
            chart_dimension = plan.chart_intent.get("dimension")
            chart_metrics = plan.chart_intent.get("metrics", [])
            if chart_dimension is not None and not isinstance(chart_dimension, str):
                raise AppError("QUERY_PLAN_CHART_INVALID", "图表维度格式无效", 422)
            if not isinstance(chart_metrics, (list, tuple)):
                raise AppError("QUERY_PLAN_CHART_INVALID", "图表指标格式无效", 422)
            if chart_dimension is not None and (
                chart_dimension not in member_map
                or member_map[chart_dimension].kind not in {"entity", "dimension"}
            ):
                raise AppError("QUERY_PLAN_CHART_INVALID", "图表维度不是语义成员", 422)
            if any(
                metric not in member_map or member_map[metric].kind != "metric"
                for metric in chart_metrics
            ):
                raise AppError("QUERY_PLAN_CHART_INVALID", "图表指标不是语义成员", 422)
            chart_type = plan.chart_intent.get("type")
            if chart_type is not None and chart_type not in {"bar", "line", "pie", "table"}:
                raise AppError("QUERY_PLAN_CHART_INVALID", "图表类型不支持", 422)
        # QueryPlan公开部分只能引用语义ID；物理字段快照仅保存在Evidence中。
        calculation_names = {
            member_id
            for step in plan.queries
            for member_id in (*step.metric_ids, *step.dimension_ids)
        }
        for calculation in plan.calculations:
            if not isinstance(calculation, dict) or not calculation.get("id"):
                raise AppError("QUERY_PLAN_CALCULATION_INVALID", "后置计算定义无效", 422)
            calculation_id = str(calculation["id"])
            if calculation_id in calculation_names:
                raise AppError("QUERY_PLAN_CALCULATION_INVALID", "后置计算ID重复", 422)
            formula = calculation.get("formula")
            if not isinstance(formula, str) or not formula.strip():
                raise AppError("QUERY_PLAN_CALCULATION_INVALID", "后置计算缺少公式", 422)
            try:
                parse_formula(formula, calculation_names)
            except FormulaError as exc:
                raise AppError(
                    "QUERY_PLAN_CALCULATION_INVALID", "后置计算只能引用语义结果", 422
                ) from exc
            calculation_names.add(calculation_id)


class PandasQueryExecutor:
    """将已校验QueryPlan执行为结构化结果。"""

    def __init__(self, *, step_timeout_seconds: float = 10.0) -> None:
        self.step_timeout_seconds = max(0.001, min(float(step_timeout_seconds), 10.0))

    def execute(
        self,
        plan: QueryPlan,
        *,
        dataset: Dataset,
        frame: pd.DataFrame,
        catalog: SemanticCatalog,
        bindings: list[SemanticBinding],
        extra_members: dict[str, Any] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> ExecutionOutput:
        started = time.perf_counter()
        if cancel_check and cancel_check():
            raise QueryCancelled()
        binding_map = _binding_map(bindings)
        member_map = {**_member_map(catalog), **(extra_members or {})}
        field_map = {
            member_id: _field_name(binding, dataset.physical_schema.fields)
            for member_id, binding in binding_map.items()
            if binding.status.value == "confirmed"
        }
        work = frame.copy(deep=True)
        result_frame = work
        intermediate: list[dict[str, Any]] = []
        warnings: list[str] = []
        if plan.intent == "compare" and len(plan.queries) >= 2:
            metric_id = plan.queries[0].metric_ids[0]
            values: list[Any] = []
            for index, step in enumerate(plan.queries[:2]):
                step_started = time.perf_counter()
                if cancel_check and cancel_check():
                    raise QueryCancelled()
                step_frame, step_intermediate, step_warnings = self._execute_step(
                    work,
                    step,
                    catalog=catalog,
                    member_map=member_map,
                    field_map=field_map,
                    physical_fields=dataset.physical_schema.fields,
                    cancel_check=cancel_check,
                )
                intermediate.extend(step_intermediate)
                warnings.extend(step_warnings)
                if len(step_frame) == 0:
                    values.append(None)
                else:
                    values.append(step_frame.iloc[0].get(metric_id))
                elapsed = time.perf_counter() - step_started
                if elapsed > self.step_timeout_seconds:
                    raise QueryTimeout("QueryPlan步骤超过时间限制")
                intermediate.append(
                    {
                        "step": index,
                        "operation": step.operation,
                        "rows": int(len(step_frame)),
                        "duration_ms": round(elapsed * 1000, 2),
                    }
                )
            current, previous = values[0], values[1]
            difference = None
            change_rate = None
            if current is not None and previous is not None:
                difference = current - previous
                if previous != 0:
                    change_rate = difference / previous * 100
                else:
                    warnings.append("对比基期为0，增幅无法计算")
            result_frame = pd.DataFrame(
                [{
                    "current": _json_value(current),
                    "previous": _json_value(previous),
                    "difference": _json_value(difference),
                    "change_rate": _json_value(change_rate),
                }]
            )
        else:
            for index, step in enumerate(plan.queries):
                step_started = time.perf_counter()
                if cancel_check and cancel_check():
                    raise QueryCancelled()
                result_frame, step_intermediate, step_warnings = self._execute_step(
                    result_frame,
                    step,
                    catalog=catalog,
                    member_map=member_map,
                    field_map=field_map,
                    physical_fields=dataset.physical_schema.fields,
                    cancel_check=cancel_check,
                )
                intermediate.extend(step_intermediate)
                warnings.extend(step_warnings)
                elapsed = time.perf_counter() - step_started
                if elapsed > self.step_timeout_seconds:
                    raise QueryTimeout("QueryPlan步骤超过时间限制")
                intermediate.append(
                    {
                        "step": index,
                        "operation": step.operation,
                        "rows": int(len(result_frame)),
                        "duration_ms": round(elapsed * 1000, 2),
                    }
                )
            if plan.intent == "ratio" and plan.queries:
                ratio_step = plan.queries[-1]
                if ratio_step.metric_ids and ratio_step.metric_ids[0] in result_frame.columns:
                    metric_id = ratio_step.metric_ids[0]
                    denominator = self._metric_value(
                        metric_id,
                        work,
                        catalog=catalog,
                        member_map=member_map,
                        field_map=field_map,
                        cache={},
                        stack=set(),
                    )
                    ratio_name = f"{metric_id}_ratio"
                    if denominator in (None, 0):
                        result_frame[ratio_name] = None
                        warnings.append("占比计算分母为0或空值")
                    else:
                        result_frame[ratio_name] = result_frame[metric_id] / denominator * 100
        if cancel_check and cancel_check():
            raise QueryCancelled()
        result_frame = self._apply_calculations(
            result_frame,
            plan.calculations,
            catalog=catalog,
            warnings=warnings,
        )
        limited = result_frame.head(plan.queries[-1].limit)
        result = {
            "columns": [str(item) for item in limited.columns],
            "data": [
                {str(key): _json_value(value) for key, value in row.items()}
                for row in limited.to_dict(orient="records")
            ],
            "total_rows": int(len(result_frame)),
            "returned_rows": int(len(limited)),
        }
        if len(result_frame) > len(limited):
            warnings.append("结果已按limit截断")
        return ExecutionOutput(
            result=result,
            intermediate_values=intermediate,
            warnings=warnings,
            input_rows=int(len(work)),
            output_rows=int(len(result_frame)),
            timings_ms={"query_execution": round((time.perf_counter() - started) * 1000, 2)},
        )

    def _apply_filters(
        self,
        frame: pd.DataFrame,
        filters: tuple[QueryFilter, ...],
        field_map: dict[str, str],
    ) -> pd.DataFrame:
        result = frame
        for item in filters:
            field = _execution_column(result, item.semantic_id, field_map)
            if field not in result.columns:
                raise AppError("PHYSICAL_FIELD_NOT_FOUND", "过滤字段不存在", 422)
            series = result[field]
            operator = item.operator
            value = item.value
            try:
                if operator == "between":
                    if (
                        not isinstance(value, dict)
                        or value.get("start") is None
                        or value.get("end") is None
                    ):
                        raise AppError("QUERY_PLAN_FILTER_INVALID", "between过滤值无效", 422)
                    if pd.api.types.is_numeric_dtype(series):
                        try:
                            start_value = float(value["start"])
                            end_value = float(value["end"])
                        except (TypeError, ValueError):
                            values = pd.to_datetime(series, errors="coerce")
                            mask = (values >= pd.to_datetime(value["start"])) & (
                                values <= pd.to_datetime(value["end"])
                            )
                        else:
                            mask = (series >= start_value) & (series <= end_value)
                    else:
                        values = pd.to_datetime(series, errors="coerce")
                        mask = (values >= pd.to_datetime(value["start"])) & (
                            values <= pd.to_datetime(value["end"])
                        )
                elif operator in {"contains", "startswith", "endswith"}:
                    text = series.astype("string")
                    if operator == "contains":
                        mask = text.str.contains(str(value), case=False, na=False)
                    elif operator == "startswith":
                        mask = text.str.startswith(str(value), na=False)
                    else:
                        mask = text.str.endswith(str(value), na=False)
                else:
                    compare = value
                    if pd.api.types.is_numeric_dtype(series):
                        compare = float(value)
                    if operator == "==":
                        mask = series == compare
                    elif operator == "!=":
                        mask = series != compare
                    elif operator == ">":
                        mask = series > compare
                    elif operator == "<":
                        mask = series < compare
                    elif operator == ">=":
                        mask = series >= compare
                    else:
                        mask = series <= compare
            except AppError:
                raise
            except (TypeError, ValueError, OverflowError) as exc:
                raise AppError("QUERY_PLAN_FILTER_INVALID", "过滤值与字段类型不兼容", 422) from exc
            result = result[mask.fillna(False)]
        return result

    def _time_bucket(self, series: pd.Series, grain: str) -> pd.Series:
        values = pd.to_datetime(series, errors="coerce")
        if grain == "day":
            return values.dt.strftime("%Y-%m-%d")
        if grain == "week":
            return values.dt.strftime("%G-W%V")
        if grain == "month":
            return values.dt.strftime("%Y-%m")
        if grain == "quarter":
            return values.dt.to_period("Q").astype("string")
        return values.dt.strftime("%Y")

    def _metric_value(
        self,
        metric_id: str,
        frame: pd.DataFrame,
        *,
        catalog: SemanticCatalog,
        member_map: dict[str, Any],
        field_map: dict[str, str],
        cache: dict[str, Any],
        stack: set[str],
        aggregation_override: str | None = None,
    ) -> Any:
        if metric_id in cache:
            return cache[metric_id]
        if metric_id in stack:
            raise AppError("METRIC_DEPENDENCY_CYCLE", "指标存在循环依赖", 422)
        member = member_map.get(metric_id)
        if member is None or member.kind != "metric":
            raise AppError("METRIC_NOT_FOUND", "指标不存在", 422)
        binding_field = _execution_column(frame, metric_id, field_map)
        if binding_field not in frame.columns:
            binding_field = None
        formula = member.extra.get("formula")
        if not formula:
            if binding_field is None:
                raise AppError("SEMANTIC_BINDING_REQUIRED", "指标缺少物理字段绑定", 409)
            aggregation = aggregation_override or str(member.extra.get("aggregation", "sum"))
            value = self._aggregate_series(frame[binding_field], aggregation)
            cache[metric_id] = value
            return value
        allowed = {
            member_id
            for member_id, semantic_member in member_map.items()
            if semantic_member.kind == "metric"
        }
        try:
            expression = parse_formula(str(formula), allowed)
        except FormulaError as exc:
            raise AppError("METRIC_FORMULA_INVALID", "指标公式无效", 422) from exc
        context: dict[str, Any] = {}
        new_stack = set(stack)
        new_stack.add(metric_id)
        for dependency in expression.dependencies:
            if dependency in member_map and member_map[dependency].kind == "metric":
                context[dependency] = self._metric_value(
                    dependency,
                    frame,
                    catalog=catalog,
                    member_map=member_map,
                    field_map=field_map,
                    cache=cache,
                    stack=new_stack,
                )
        try:
            value = expression.evaluate(context)
        except (FormulaError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise AppError("METRIC_FORMULA_EXECUTION_FAILED", "指标公式执行失败", 422) from exc
        cache[metric_id] = value
        return value

    def _aggregate_series(self, series: pd.Series, aggregation: str) -> Any:
        if aggregation == "sum":
            return series.sum(skipna=True)
        if aggregation == "count":
            return series.count()
        if aggregation == "count_distinct":
            return series.nunique(dropna=True)
        if aggregation == "mean":
            return series.mean(skipna=True)
        if aggregation == "min":
            return series.min(skipna=True)
        if aggregation == "max":
            return series.max(skipna=True)
        raise AppError("METRIC_AGGREGATION_INVALID", "指标聚合方式不支持", 422)

    def _execute_step(
        self,
        frame: pd.DataFrame,
        step: QueryStep,
        *,
        catalog: SemanticCatalog,
        member_map: dict[str, Any],
        field_map: dict[str, str],
        physical_fields: list[Any],
        cancel_check: Callable[[], bool] | None,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]], list[str]]:
        filtered = self._apply_filters(frame, step.filters, field_map)
        warnings: list[str] = []
        if cancel_check and cancel_check():
            raise QueryCancelled()
        if step.operation in {"detail", "filter"}:
            columns: dict[str, pd.Series] = {}
            for semantic_id in (*step.dimension_ids, *step.metric_ids):
                field = _execution_column(filtered, semantic_id, field_map)
                if field and field in filtered.columns:
                    columns[semantic_id] = filtered[field]
            result = pd.DataFrame(columns, index=filtered.index).reset_index(drop=True)
            if step.sort_metric_id and step.sort_metric_id in result:
                result = result.sort_values(step.sort_metric_id, ascending=step.ascending)
            intermediate = [
                {
                    "input_rows": int(len(frame)),
                    "filtered_rows": int(len(filtered)),
                    "null_rows": int(result.isna().all(axis=1).sum()) if len(result) else 0,
                }
            ]
            if filtered.empty:
                warnings.append("no data")
            return result, intermediate, warnings

        if filtered.empty:
            columns = [*step.dimension_ids, *step.metric_ids]
            return (
                pd.DataFrame(columns=columns),
                [{"input_rows": int(len(frame)), "filtered_rows": 0, "null_rows": 0}],
                ["no data"],
            )

        working = filtered.copy()
        grouping: list[str] = []
        output_names: dict[str, str] = {}
        for dimension_id in step.dimension_ids:
            field = _execution_column(working, dimension_id, field_map)
            output_name = dimension_id
            physical = next(
                (
                    item
                    for item in physical_fields
                    if item.normalized_name == field_map.get(dimension_id, field)
                ),
                None,
            )
            is_time_dimension = bool(
                physical is not None
                and (
                    physical.is_time_candidate
                    or physical.physical_type in {"datetime", "date"}
                )
            )
            if step.time_grain and is_time_dimension:
                output_name = dimension_id
                working[output_name] = self._time_bucket(working[field], step.time_grain)
            else:
                working[output_name] = working[field]
            grouping.append(output_name)
            output_names[dimension_id] = output_name
        metrics = list(step.metric_ids)
        if not metrics:
            raise AppError("QUERY_PLAN_METRIC_REQUIRED", "查询至少需要一个指标", 422)
        if grouping:
            rows: list[dict[str, Any]] = []
            grouped = working.groupby(grouping, dropna=False, sort=False)
            for keys, group in grouped:
                if not isinstance(keys, tuple):
                    keys = (keys,)
                row = {grouping[index]: _json_value(value) for index, value in enumerate(keys)}
                cache: dict[str, Any] = {}
                for metric_id in metrics:
                    value = self._metric_value(
                        metric_id,
                        group,
                        catalog=catalog,
                        member_map=member_map,
                        field_map=field_map,
                        cache=cache,
                        stack=set(),
                        aggregation_override=step.aggregation,
                    )
                    row[metric_id] = _json_value(value)
                    if value is None:
                        warnings.append(f"指标{metric_id}结果为空")
                rows.append(row)
            result = pd.DataFrame(rows)
        else:
            cache = {}
            row = {
                metric_id: _json_value(
                    self._metric_value(
                        metric_id,
                        working,
                        catalog=catalog,
                        member_map=member_map,
                        field_map=field_map,
                        cache=cache,
                        stack=set(),
                        aggregation_override=step.aggregation,
                    )
                )
                for metric_id in metrics
            }
            result = pd.DataFrame([row])
        if step.sort_metric_id and step.sort_metric_id in result.columns:
            result = result.sort_values(step.sort_metric_id, ascending=step.ascending)
        elif (
            step.operation in {"rank", "group"}
            and metrics
            and metrics[0] in result.columns
        ):
            result = result.sort_values(metrics[0], ascending=False)
        elif step.operation == "trend" and grouping:
            result = result.sort_values(grouping[0], ascending=True)
        result = result.reset_index(drop=True)
        return result, [
            {
                "input_rows": int(len(frame)),
                "filtered_rows": int(len(filtered)),
                "result_rows": int(len(result)),
                "null_rows": int(result.isna().sum().sum()),
            }
        ], warnings

    def _apply_calculations(
        self,
        frame: pd.DataFrame,
        calculations: tuple[dict[str, Any], ...],
        *,
        catalog: SemanticCatalog,
        warnings: list[str],
    ) -> pd.DataFrame:
        result = frame.copy()
        for calculation in calculations:
            calculation_id = str(calculation.get("id"))
            formula = calculation.get("formula")
            if not formula:
                raise AppError("QUERY_PLAN_CALCULATION_INVALID", "后置计算缺少公式", 422)
            try:
                expression = parse_formula(str(formula), set(result.columns))
                values: list[Any] = []
                for _, row in result.iterrows():
                    values.append(expression.evaluate(row.to_dict()))
                result[calculation_id] = values
                if any(value is None for value in values):
                    warnings.append(f"后置计算{calculation_id}包含空值")
            except FormulaError as exc:
                raise AppError("METRIC_FORMULA_INVALID", "后置计算公式无效", 422) from exc
        return result


def chart_from_evidence(
    *,
    evidence_result: dict[str, Any],
    dimension: str | None,
    metrics: list[str],
    title: str,
    unit: str | None = None,
) -> ChartSpec | None:
    """仅从已执行结果创建ChartSpec，不接受任意脚本或外部URL。"""

    rows = evidence_result.get("data") or []
    if not rows or not metrics:
        return None
    if not any(any(metric in row for metric in metrics) for row in rows):
        return None
    time_pattern = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?|Q\d|[- ]W\d{2})?$")
    chart_type = (
        "line"
        if dimension
        and any(time_pattern.match(str(row.get(dimension, ""))) for row in rows)
        else "bar"
    )
    if dimension is None:
        chart_type = "bar"
    safe_rows = []
    allowed = ([dimension] if dimension else []) + [
        metric for metric in metrics if metric != dimension
    ]
    for row in rows:
        safe_rows.append({key: row.get(key) for key in allowed if key in row})
    return ChartSpec(
        chart_type=chart_type,
        title=title[:200],
        dimension=dimension,
        metrics=tuple(metrics),
        data=tuple(safe_rows),
        unit=unit,
    )


__all__ = [
    "ExecutionOutput",
    "PandasQueryExecutor",
    "QueryCancelled",
    "QueryTimeout",
    "QueryPlanValidator",
    "chart_from_evidence",
]
