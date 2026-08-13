"""阶段2分析领域对象。

这些对象只保存可序列化的分析元数据。DataFrame、文件对象、模型客户端和
LangGraph运行时状态均不进入领域对象；DataFrame仍由 ``DatasetStore`` 管理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AnalysisStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ClarificationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REVISED = "revised"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class QueryFilter:
    """只允许出现在QueryPlan中的结构化过滤条件。"""

    semantic_id: str
    operator: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_id": self.semantic_id,
            "operator": self.operator,
            "value": self.value,
        }


@dataclass(frozen=True)
class QueryStep:
    """一个确定性查询步骤，不含物理列名。"""

    operation: str = "aggregate"
    metric_ids: tuple[str, ...] = ()
    dimension_ids: tuple[str, ...] = ()
    filters: tuple[QueryFilter, ...] = ()
    time_grain: str | None = None
    aggregation: str | None = None
    sort_metric_id: str | None = None
    ascending: bool = False
    limit: int = 20

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "metric_ids": list(self.metric_ids),
            "dimension_ids": list(self.dimension_ids),
            "filters": [item.to_dict() for item in self.filters],
            "time_grain": self.time_grain,
            "aggregation": self.aggregation,
            "sort_metric_id": self.sort_metric_id,
            "ascending": self.ascending,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class QueryPlan:
    plan_id: str
    task_id: str
    dataset_id: str
    semantic_model_version: str
    intent: str
    queries: tuple[QueryStep, ...]
    calculations: tuple[dict[str, Any], ...] = ()
    chart_intent: dict[str, Any] | None = None
    binding_snapshot: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "dataset_id": self.dataset_id,
            "semantic_model_version": self.semantic_model_version,
            "intent": self.intent,
            "queries": [item.to_dict() for item in self.queries],
            "calculations": [dict(item) for item in self.calculations],
            "chart_intent": dict(self.chart_intent) if self.chart_intent else None,
            "binding_snapshot": [dict(item) for item in self.binding_snapshot],
        }


@dataclass
class SemanticResolution:
    intent: str
    metric_ids: list[str] = field(default_factory=list)
    dimension_ids: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    time_range: dict[str, Any] | None = None
    time_grain: str | None = None
    aggregation: str | None = None
    order_by: str | None = None
    ascending: bool = False
    limit: int = 20
    binding_candidates: dict[str, list[str]] = field(default_factory=dict)
    missing_concepts: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    clarification_draft: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "metric_ids": list(self.metric_ids),
            "dimension_ids": list(self.dimension_ids),
            "filters": [dict(item) for item in self.filters],
            "time_range": dict(self.time_range) if self.time_range else None,
            "time_grain": self.time_grain,
            "aggregation": self.aggregation,
            "order_by": self.order_by,
            "ascending": self.ascending,
            "limit": self.limit,
            "binding_candidates": {
                key: list(value) for key, value in self.binding_candidates.items()
            },
            "missing_concepts": list(self.missing_concepts),
            "ambiguities": list(self.ambiguities),
            "clarification_draft": (
                dict(self.clarification_draft) if self.clarification_draft else None
            ),
            "comparison": dict(self.comparison) if self.comparison else None,
        }


@dataclass
class PendingClarification:
    clarification_id: str
    task_id: str
    analysis_id: str
    draft_version: int
    kind: str
    summary: dict[str, Any]
    status: ClarificationStatus = ClarificationStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "clarification_id": self.clarification_id,
            "task_id": self.task_id,
            "analysis_id": self.analysis_id,
            "draft_version": self.draft_version,
            "kind": self.kind,
            "summary": dict(self.summary),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class AnalysisEvidence:
    analysis_id: str
    task_id: str
    dataset_id: str
    question: str
    semantic_model_version: str
    semantic_resolution: dict[str, Any]
    binding_snapshot: list[dict[str, Any]]
    query_plan: dict[str, Any]
    filters: list[dict[str, Any]] = field(default_factory=list)
    intermediate_values: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    input_rows: int = 0
    output_rows: int = 0
    timings_ms: dict[str, float] = field(default_factory=dict)
    status: AnalysisStatus = AnalysisStatus.COMPLETED
    join_info: dict[str, Any] | None = None
    temporary_metrics: list[dict[str, Any]] = field(default_factory=list)
    metric_definitions: list[dict[str, Any]] = field(default_factory=list)
    dataset_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "task_id": self.task_id,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "question": self.question,
            "semantic_model_version": self.semantic_model_version,
            "semantic_resolution": dict(self.semantic_resolution),
            "metric_definitions": [dict(item) for item in self.metric_definitions],
            "binding_snapshot": [dict(item) for item in self.binding_snapshot],
            "query_plan": dict(self.query_plan),
            "filters": [dict(item) for item in self.filters],
            "intermediate_values": [dict(item) for item in self.intermediate_values],
            "result": dict(self.result),
            "warnings": list(self.warnings),
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "timings_ms": dict(self.timings_ms),
            "status": self.status.value,
            "join_info": dict(self.join_info) if self.join_info else None,
            "temporary_metrics": [dict(item) for item in self.temporary_metrics],
        }


@dataclass(frozen=True)
class ChartSpec:
    chart_type: str
    title: str
    dimension: str | None
    metrics: tuple[str, ...]
    data: tuple[dict[str, Any], ...]
    unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chart_type": self.chart_type,
            "title": self.title,
            "dimension": self.dimension,
            "metrics": list(self.metrics),
            "data": [dict(item) for item in self.data],
            "unit": self.unit,
        }


@dataclass
class AnalysisRecord:
    analysis_id: str
    task_id: str
    dataset_id: str
    question: str
    status: AnalysisStatus = AnalysisStatus.CREATED
    current_stage: str = "created"
    clarification: PendingClarification | None = None
    evidence: AnalysisEvidence | None = None
    chart: ChartSpec | None = None
    answer: str | None = None
    error: dict[str, Any] | None = None
    resources_settled: bool = True
    pending_resources: int = 0
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cancel_requested: bool = False
    run_generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "task_id": self.task_id,
            "dataset_id": self.dataset_id,
            "question": self.question,
            "status": self.status.value,
            "current_stage": self.current_stage,
            "clarification": self.clarification.to_dict() if self.clarification else None,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "chart": self.chart.to_dict() if self.chart else None,
            "answer": self.answer,
            "error": dict(self.error) if self.error else None,
            "resources_settled": self.resources_settled,
            "pending_resources": self.pending_resources,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }
