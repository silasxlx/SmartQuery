"""阶段2统一分析服务。

该服务是同步问数、SSE问数、澄清恢复、取消和分析查询的唯一业务入口。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

import pandas as pd
from langgraph.types import Command

from ..agent.stage2_graph import AgentState, build_analysis_graph
from ..domain.analysis import (
    AnalysisEvidence,
    AnalysisRecord,
    AnalysisStatus,
    ChartSpec,
    ClarificationStatus,
    PendingClarification,
    QueryFilter,
    QueryPlan,
    QueryStep,
    SemanticResolution,
)
from ..domain.semantic import SemanticMember
from ..domain.task_dataset import BindingStatus, DatasetStatus
from ..errors import AppError, safe_public_details, safe_public_message
from ..infrastructure.model_provider import (
    ModelProviderRegistry,
    SemanticResolutionPayload,
    structured_call,
)
from ..infrastructure.query_engine import (
    PandasQueryExecutor,
    QueryCancelled,
    QueryPlanValidator,
    QueryTimeout,
    chart_from_evidence,
)
from ..infrastructure.restricted_ast import (
    FormulaError,
    validate_metric_formulas,
)
from ..infrastructure.stage1_repositories import DatasetStore, TaskRepository
from ..infrastructure.stage1_semantic import SemanticCatalog
from ..infrastructure.task_knowledge import TaskKnowledgeStore

NON_TERMINAL = {
    AnalysisStatus.CREATED,
    AnalysisStatus.RUNNING,
    AnalysisStatus.AWAITING_CLARIFICATION,
    AnalysisStatus.CANCEL_REQUESTED,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, AppError):
        return {
            "code": exc.code,
            "message": safe_public_message(exc.message),
            "details": safe_public_details(exc.details),
        }
    return {"code": "INTERNAL_ERROR", "message": "分析失败", "details": {}}


class AnalysisService:
    def __init__(
        self,
        *,
        repository: TaskRepository,
        dataset_store: DatasetStore,
        semantic_catalog: SemanticCatalog,
        provider_registry: ModelProviderRegistry,
        embedding_registry: Any | None = None,
        executor: Any,
        knowledge_store: TaskKnowledgeStore | None = None,
        id_factory: Callable[[], str] | None = None,
        analysis_timeout_seconds: float = 120.0,
        query_executor: PandasQueryExecutor | None = None,
    ) -> None:
        self.repository = repository
        self.dataset_store = dataset_store
        self.semantic_catalog = semantic_catalog
        self.provider_registry = provider_registry
        self.embedding_registry = embedding_registry
        self.executor = executor
        self.knowledge_store = knowledge_store
        self._id_factory = id_factory or repository.new_id
        self.analysis_timeout_seconds = max(0.001, min(float(analysis_timeout_seconds), 120.0))
        self.query_executor = query_executor or PandasQueryExecutor(step_timeout_seconds=10.0)
        self.plan_validator = QueryPlanValidator()
        self._cancel_flags: dict[str, bool] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._graph = build_analysis_graph(self, checkpointer=None)

    def new_id(self) -> str:
        return str(self._id_factory())

    async def close(self) -> None:
        for record in (
            item
            for task in self.repository.list()
            for item in self.repository.list_analyses(task.task_id)
            if item.status in NON_TERMINAL
        ):
            record.cancel_requested = True
            self._cancel_flags[record.analysis_id] = True
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)
        # InMemorySaver is process-local, but explicitly drop every task
        # thread so completed and paused checkpoints do not survive the
        # application lifecycle in a long-lived container reference.
        checkpointer = getattr(self._graph, "checkpointer", None)
        if checkpointer is not None and hasattr(checkpointer, "delete_thread"):
            for task in self.repository.list():
                checkpointer.delete_thread(str(task.task_id))

    def clear_task(self, task_id: str) -> None:
        """Drop cancellation flags and the task thread checkpoint on deletion."""

        records = self.repository.list_analyses(task_id) if self.repository.get(task_id) else []
        for record in records:
            self._cancel_flags.pop(record.analysis_id, None)
        checkpointer = getattr(self._graph, "checkpointer", None)
        if checkpointer is not None and hasattr(checkpointer, "delete_thread"):
            checkpointer.delete_thread(str(task_id))

    def _task(self, task_id: str):
        return self.repository.require(task_id)

    def _check_cancelled(self, state: AgentState) -> None:
        if self._cancel_flags.get(state.get("analysis_id", ""), False):
            raise QueryCancelled()

    def _extra_members(self, task_id: str) -> dict[str, SemanticMember]:
        task = self.repository.require(task_id)
        return {
            str(item["metric_id"]): SemanticMember(
                member_id=str(item["metric_id"]),
                kind="metric",
                name=str(item.get("name", item["metric_id"])),
                aliases=tuple(item.get("aliases", [])),
                allowed_types=("number",),
                source_refs=(f"task:{task_id}",),
                description=item.get("description"),
                unit=item.get("unit"),
                extra={"formula": item["formula"], "temporary": True},
            )
            for item in task.semantic_extensions
            if item.get("metric_id") and item.get("formula")
        }

    def _metric_definition_snapshots(
        self, task_id: str, metric_ids: list[str] | tuple[str, ...]
    ) -> list[dict[str, Any]]:
        members = {**self.semantic_catalog.members, **self._extra_members(task_id)}
        return [
            members[metric_id].to_dict()
            for metric_id in metric_ids
            if metric_id in members and members[metric_id].kind == "metric"
        ]

    def confirm_temporary_metric(
        self,
        task_id: str,
        dataset_id: str,
        *,
        metric_id: str,
        name: str,
        formula: str,
        unit: str | None = None,
        confirmed_binding_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """只在澄清提交节点调用的任务级临时指标注册。"""

        task = self.repository.require(task_id)
        self.dataset_store.require(task_id, dataset_id)
        if not metric_id.startswith("task:"):
            metric_id = f"task:{task_id}:{metric_id}"
        official_metrics = {
            member_id: member
            for member_id, member in self.semantic_catalog.members.items()
            if member.kind == "metric"
        }
        existing_metrics = {
            str(item["metric_id"]): str(item["formula"])
            for item in task.semantic_extensions
            if item.get("metric_id") and item.get("formula")
        }
        formulas = {
            member_id: str(member.extra["formula"])
            for member_id, member in official_metrics.items()
            if member.extra.get("formula")
        }
        formulas.update(existing_metrics)
        formulas[metric_id] = formula
        all_metric_ids = set(official_metrics) | set(formulas)
        units = {
            member_id: member.unit for member_id, member in official_metrics.items()
        }
        units.update(
            {
                str(item["metric_id"]): item.get("unit")
                for item in task.semantic_extensions
                if item.get("metric_id")
            }
        )
        units[metric_id] = unit
        grains = {
            member_id: (
                member.extra.get("default_time_grain") or member.extra.get("time_grain")
            )
            for member_id, member in official_metrics.items()
        }
        try:
            compiled_formulas = validate_metric_formulas(
                formulas,
                field_names=all_metric_ids,
                units=units,
                grains=grains,
            )
        except FormulaError as exc:
            raise AppError("METRIC_FORMULA_INVALID", "临时指标公式无效", 422) from exc
        confirmed_bindings = {
            item.semantic_member_id
            for item in self.dataset_store.require(task_id, dataset_id).semantic_bindings
            if item.status == BindingStatus.CONFIRMED
        }
        confirmed_bindings.update(confirmed_binding_ids or set())
        for dependency in compiled_formulas[metric_id].dependencies:
            member = official_metrics.get(dependency)
            if (
                member is not None
                and not member.extra.get("formula")
                and dependency not in confirmed_bindings
            ):
                raise AppError("SEMANTIC_BINDING_REQUIRED", "临时指标依赖的基础指标尚未绑定", 409)
        task.semantic_extensions = [
            item for item in task.semantic_extensions if item.get("metric_id") != metric_id
        ]
        task.semantic_extensions.append(
            {
                "metric_id": metric_id,
                "name": name,
                "formula": formula,
                "unit": unit,
                "confirmed": True,
                "dataset_id": dataset_id,
            }
        )
        task.touch()
        return task.semantic_extensions[-1]

    def _record(self, task_id: str, analysis_id: str) -> AnalysisRecord:
        return self.repository.get_analysis(task_id, analysis_id)

    def _drop_checkpoint(self, task_id: str) -> None:
        checkpointer = getattr(self._graph, "checkpointer", None)
        if checkpointer is not None and hasattr(checkpointer, "delete_thread"):
            checkpointer.delete_thread(str(task_id))

    def _terminate_waiting_clarification(self, record: AnalysisRecord) -> None:
        if record.status != AnalysisStatus.AWAITING_CLARIFICATION:
            return
        record.cancel_requested = True
        record.status = AnalysisStatus.CANCELLED
        if record.clarification is not None:
            record.clarification.status = ClarificationStatus.EXPIRED
        record.ended_at = _now()
        record.resources_settled = True
        self._ensure_terminal_evidence(
            record,
            AnalysisStatus.CANCELLED,
            "new question superseded draft",
        )
        self._drop_checkpoint(record.task_id)
        self._release_task(record.task_id, record.analysis_id)

    def _ensure_terminal_evidence(
        self,
        record: AnalysisRecord,
        status: AnalysisStatus,
        warning: str,
    ) -> None:
        """Keep a small auditable evidence envelope for terminal failures."""

        if record.evidence is not None:
            record.evidence.status = status
            if warning in record.evidence.warnings:
                record.evidence.warnings.remove(warning)
            record.evidence.warnings.insert(0, warning)
            return
        task = self.repository.get(record.task_id)
        dataset = self.dataset_store.get(record.dataset_id)
        bindings = dataset.semantic_bindings if dataset is not None else []
        record.evidence = AnalysisEvidence(
            analysis_id=record.analysis_id,
            task_id=record.task_id,
            dataset_id=record.dataset_id,
            dataset_version=dataset.version if dataset is not None else None,
            question=record.question,
            semantic_model_version=(
                task.semantic_model_version if task and task.semantic_model_version else "unknown"
            ),
            semantic_resolution={},
            binding_snapshot=[item.to_dict() for item in bindings],
            query_plan={},
            result={},
            warnings=[warning],
            status=status,
            temporary_metrics=(list(task.semantic_extensions) if task else []),
        )

    async def _create_record(self, task_id: str, dataset_id: str, question: str) -> AnalysisRecord:
        task = self._task(task_id)
        dataset = self.dataset_store.require(task_id, dataset_id)
        if dataset.status != DatasetStatus.READY:
            raise AppError("DATASET_NOT_READY", "Dataset尚未就绪", 409)
        if task.semantic_model_version not in {None, self.semantic_catalog.model.version}:
            raise AppError("SEMANTIC_MODEL_VERSION_MISMATCH", "任务语义模型版本已失效", 409)
        async with task.lock:
            if task.status.value != "active":
                raise AppError("TASK_NOT_AVAILABLE", "任务当前不可用", 409)
            if task.busy or self.repository.active_analysis(task_id) is not None:
                raise AppError("TASK_BUSY", "当前任务已有分析正在执行", 409)
            analysis_id = self.new_id()
            record = AnalysisRecord(
                analysis_id=analysis_id,
                task_id=str(task_id),
                dataset_id=str(dataset_id),
                question=question,
                status=AnalysisStatus.CREATED,
            )
            self.repository.add_analysis(record)
            task.busy = True
            task.conversation.append(
                {"analysis_id": analysis_id, "role": "user", "content": question}
            )
            task.conversation = task.conversation[-20:]
            task.touch()
            self._cancel_flags[analysis_id] = False
            return record

    async def analyze(
        self,
        task_id: str,
        *,
        dataset_id: str,
        question: str,
        clarification_id: str | None = None,
        clarification_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(question).strip() and clarification_id is None:
            raise AppError("QUESTION_EMPTY", "问题不能为空", 422)
        return await self.submit(
            task_id,
            dataset_id=dataset_id,
            question=question,
            clarification_id=clarification_id,
            analysis_id=(clarification_response or {}).get("analysis_id"),
            response=clarification_response,
        )

    async def submit(
        self,
        task_id: str,
        *,
        dataset_id: str,
        question: str,
        clarification_id: str | None = None,
        analysis_id: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """API友好的入口；澄清恢复必须显式提供analysis_id。"""

        if not str(question).strip() and clarification_id is None:
            raise AppError("QUESTION_EMPTY", "question cannot be empty", 422)
        if clarification_id:
            if not analysis_id:
                matching = next(
                    (
                        item
                        for item in reversed(self.repository.list_analyses(task_id))
                        if item.clarification is not None
                        and item.clarification.clarification_id == clarification_id
                    ),
                    None,
                )
                active = self.repository.active_analysis(task_id)
                if matching is not None:
                    analysis_id = matching.analysis_id
                elif active is None:
                    raise AppError("ANALYSIS_NOT_FOUND", "待澄清分析不存在", 404)
                else:
                    analysis_id = active.analysis_id
            return await self.resume_clarification(
                task_id,
                analysis_id,
                clarification_id=clarification_id,
                response=response or {"confirm": True},
            )
        active = self.repository.active_analysis(task_id)
        if active is not None and active.status == AnalysisStatus.AWAITING_CLARIFICATION:
            self._terminate_waiting_clarification(active)
        record = await self._create_record(task_id, dataset_id, question)
        return await self._run_record(record)

    async def _run_record(
        self, record: AnalysisRecord, *, resume: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        record.status = AnalysisStatus.RUNNING
        record.current_stage = "running"
        record.started_at = record.started_at or _now()
        record.run_generation += 1
        config = {"configurable": {"thread_id": record.task_id}}
        initial: AgentState = {
            "task_id": record.task_id,
            "analysis_id": record.analysis_id,
            "dataset_id": record.dataset_id,
            "question": record.question,
            "status": AnalysisStatus.RUNNING.value,
        }
        graph_task: asyncio.Task[Any] | None = None

        def settle_late(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            try:
                done.result()
            except BaseException:
                pass
            record.resources_settled = record.pending_resources == 0
            if record.status not in NON_TERMINAL and record.resources_settled:
                self._release_task(record.task_id, record.analysis_id)

        try:
            if resume is None:
                graph_task = asyncio.create_task(self._graph.ainvoke(initial, config=config))
            else:
                graph_task = asyncio.create_task(
                    self._graph.ainvoke(Command(resume=resume), config=config)
                )
            self._background_tasks.add(graph_task)
            graph_task.add_done_callback(settle_late)
            result = await asyncio.wait_for(
                asyncio.shield(graph_task), timeout=self.analysis_timeout_seconds
            )
            if "__interrupt__" in result:
                self._set_clarification(record, result)
                return record.to_dict()
            self._apply_graph_result(record, result)
            if record.cancel_requested or self._cancel_flags.get(record.analysis_id):
                record.status = AnalysisStatus.CANCELLED
                record.error = {"code": "CANCELLED", "message": "分析已取消", "details": {}}
            elif result.get("status") == AnalysisStatus.CANCELLED.value:
                record.status = AnalysisStatus.CANCELLED
            else:
                record.status = AnalysisStatus.COMPLETED
            record.ended_at = _now()
            task = self.repository.require(record.task_id)
            if record.answer is not None and not any(
                item.get("analysis_id") == record.analysis_id and item.get("role") == "assistant"
                for item in task.conversation
            ):
                task.conversation.append(
                    {
                        "analysis_id": record.analysis_id,
                        "role": "assistant",
                        "content": record.answer,
                    }
                )
                task.conversation = task.conversation[-20:]
            if record.status == AnalysisStatus.CANCELLED:
                self._ensure_terminal_evidence(
                    record, AnalysisStatus.CANCELLED, "analysis cancelled"
                )
            return record.to_dict()
        except QueryTimeout as exc:
            record.status = AnalysisStatus.TIMED_OUT
            record.resources_settled = False
            record.error = {"code": "QUERY_TIMEOUT", "message": "查询超过时间限制", "details": {}}
            record.ended_at = _now()
            self._cancel_flags[record.analysis_id] = True
            self._ensure_terminal_evidence(record, AnalysisStatus.TIMED_OUT, "query timeout")
            raise AppError("QUERY_TIMEOUT", "查询超过时间限制", 408) from exc
        except asyncio.TimeoutError as exc:
            record.status = AnalysisStatus.TIMED_OUT
            record.resources_settled = False
            self._cancel_flags[record.analysis_id] = True
            record.error = {
                "code": "MODEL_OR_ANALYSIS_TIMEOUT",
                "message": "分析超过时间限制",
                "details": {},
            }
            record.ended_at = _now()
            self._ensure_terminal_evidence(record, AnalysisStatus.TIMED_OUT, "analysis timeout")
            raise AppError("MODEL_TIMEOUT", "完整分析超过时间限制", 504) from exc
        except QueryCancelled:
            record.status = AnalysisStatus.CANCELLED
            record.error = {"code": "CANCELLED", "message": "分析已取消", "details": {}}
            record.ended_at = _now()
            self._ensure_terminal_evidence(record, AnalysisStatus.CANCELLED, "analysis cancelled")
            return record.to_dict()
        except TimeoutError as exc:
            record.status = AnalysisStatus.TIMED_OUT
            record.error = {"code": "QUERY_TIMEOUT", "message": "查询超过时间限制", "details": {}}
            record.ended_at = _now()
            self._ensure_terminal_evidence(record, AnalysisStatus.TIMED_OUT, "query timeout")
            raise AppError("QUERY_TIMEOUT", "查询超过时间限制", 408) from exc
        except Exception as exc:
            if isinstance(exc, AppError) and exc.code in {"MODEL_TIMEOUT", "QUERY_TIMEOUT"}:
                record.status = AnalysisStatus.TIMED_OUT
                record.error = _safe_error(exc)
                record.ended_at = _now()
                record.resources_settled = True
                self._ensure_terminal_evidence(record, AnalysisStatus.TIMED_OUT, "analysis timeout")
                raise
            record.status = AnalysisStatus.FAILED
            record.error = _safe_error(exc)
            record.ended_at = _now()
            self._ensure_terminal_evidence(record, AnalysisStatus.FAILED, "analysis failed")
            if isinstance(exc, AppError):
                raise
            raise AppError("ANALYSIS_FAILED", "分析失败", 500) from exc
        finally:
            if record.status not in NON_TERMINAL and record.resources_settled:
                self._release_task(record.task_id, record.analysis_id)

    def _release_task(self, task_id: str, analysis_id: str) -> None:
        task = self.repository.get(task_id)
        if task is not None:
            task.busy = False
            task.touch()
        self._cancel_flags.pop(analysis_id, None)
        self._drop_checkpoint(task_id)

    def _set_clarification(self, record: AnalysisRecord, result: dict[str, Any]) -> None:
        interrupts = result.get("__interrupt__") or []
        value = getattr(interrupts[0], "value", {}) if interrupts else {}
        if not isinstance(value, dict):
            value = {"summary": str(value)}
        clarification_id = str(value.get("clarification_id") or self.new_id())
        pending = PendingClarification(
            clarification_id=clarification_id,
            task_id=record.task_id,
            analysis_id=record.analysis_id,
            draft_version=int(value.get("draft_version", 1)),
            kind=str(value.get("kind", "semantic_binding")),
            summary=dict(value.get("summary", value)),
            status=ClarificationStatus.PENDING,
        )
        record.clarification = pending
        record.status = AnalysisStatus.AWAITING_CLARIFICATION
        record.current_stage = "clarification_required"
        if record.evidence is None:
            task = self.repository.get(record.task_id)
            dataset = self.dataset_store.get(record.dataset_id)
            record.evidence = AnalysisEvidence(
                analysis_id=record.analysis_id,
                task_id=record.task_id,
                dataset_id=record.dataset_id,
                dataset_version=dataset.version if dataset is not None else None,
                question=record.question,
                semantic_model_version=(
                    task.semantic_model_version
                    if task and task.semantic_model_version
                    else "unknown"
                ),
                semantic_resolution=result.get("semantic_resolution", {}),
                metric_definitions=self._metric_definition_snapshots(
                    record.task_id,
                    result.get("semantic_resolution", {}).get("metric_ids", []),
                ),
                binding_snapshot=(
                    [item.to_dict() for item in dataset.semantic_bindings]
                    if dataset is not None
                    else []
                ),
                query_plan=result.get("query_plan", {}),
                warnings=["clarification required"],
                status=AnalysisStatus.AWAITING_CLARIFICATION,
                temporary_metrics=(list(task.semantic_extensions) if task else []),
            )
        else:
            record.evidence.status = AnalysisStatus.AWAITING_CLARIFICATION
            record.evidence.semantic_resolution = result.get("semantic_resolution", {})
            if "clarification required" not in record.evidence.warnings:
                record.evidence.warnings.append("clarification required")

    def _apply_graph_result(self, record: AnalysisRecord, result: dict[str, Any]) -> None:
        record.current_stage = str(result.get("current_stage", "finalize"))
        if result.get("evidence"):
            record.evidence = AnalysisEvidence(**self._evidence_kwargs(result["evidence"]))
        if record.evidence is not None:
            record.evidence.timings_ms.update(result.get("timings_ms", {}))
        if result.get("answer") is not None:
            record.answer = str(result["answer"])
        chart = result.get("chart")
        if chart:
            record.chart = ChartSpec(
                chart_type=chart["chart_type"],
                title=chart["title"],
                dimension=chart.get("dimension"),
                metrics=tuple(chart.get("metrics", [])),
                data=tuple(chart.get("data", [])),
                unit=chart.get("unit"),
            )
        task = self.repository.require(record.task_id)
        resolution = result.get("semantic_resolution") or {}
        task.conversation_summary = {
            "last_analysis_id": record.analysis_id,
            "intent": resolution.get("intent"),
            "metric_ids": list(resolution.get("metric_ids", [])),
            "dimension_ids": list(resolution.get("dimension_ids", [])),
            "result_rows": record.evidence.output_rows if record.evidence else 0,
        }
        task.touch()

    def _evidence_kwargs(self, value: dict[str, Any]) -> dict[str, Any]:
        return {
            "analysis_id": value["analysis_id"],
            "task_id": value["task_id"],
            "dataset_id": value["dataset_id"],
            "dataset_version": value.get("dataset_version"),
            "question": value["question"],
            "semantic_model_version": value["semantic_model_version"],
            "semantic_resolution": value.get("semantic_resolution", {}),
            "metric_definitions": value.get("metric_definitions", []),
            "binding_snapshot": value.get("binding_snapshot", []),
            "query_plan": value.get("query_plan", {}),
            "filters": value.get("filters", []),
            "intermediate_values": value.get("intermediate_values", []),
            "result": value.get("result", {}),
            "warnings": value.get("warnings", []),
            "input_rows": value.get("input_rows", 0),
            "output_rows": value.get("output_rows", 0),
            "timings_ms": value.get("timings_ms", {}),
            "status": (
                value.get("status")
                if isinstance(value.get("status"), AnalysisStatus)
                else AnalysisStatus(value.get("status", AnalysisStatus.COMPLETED.value))
            ),
            "join_info": value.get("join_info"),
            "temporary_metrics": value.get("temporary_metrics", []),
        }

    async def resume_clarification(
        self,
        task_id: str,
        analysis_id: str,
        *,
        clarification_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        record = self._record(task_id, analysis_id)
        if (
            record.status != AnalysisStatus.AWAITING_CLARIFICATION
            and record.clarification is not None
            and record.clarification.clarification_id == clarification_id
            and record.clarification.status
            in {ClarificationStatus.CONFIRMED, ClarificationStatus.REJECTED}
        ):
            return record.to_dict()
        if record.status != AnalysisStatus.AWAITING_CLARIFICATION or record.clarification is None:
            raise AppError("CLARIFICATION_MISMATCH", "当前分析不在等待澄清", 409)
        if record.clarification.clarification_id != clarification_id:
            raise AppError("CLARIFICATION_MISMATCH", "澄清ID不匹配", 409)
        draft_version = response.get("draft_version")
        if draft_version is not None:
            try:
                version_matches = int(draft_version) == record.clarification.draft_version
            except (TypeError, ValueError):
                version_matches = False
            if not version_matches:
                raise AppError("CLARIFICATION_MISMATCH", "澄清草案版本已失效", 409)
        record.status = AnalysisStatus.RUNNING
        record.clarification.status = (
            ClarificationStatus.CONFIRMED
            if self._is_confirmed(response)
            else ClarificationStatus.REJECTED
        )
        task = self.repository.require(task_id)
        task.conversation.append(
            {
                "analysis_id": analysis_id,
                "role": "user",
                "content": str(response.get("message", "确认")),
            }
        )
        task.conversation = task.conversation[-20:]
        task.touch()
        return await self._run_record(record, resume={**response, "analysis_id": analysis_id})

    def _is_confirmed(self, response: dict[str, Any]) -> bool:
        if response.get("confirm") is True:
            return True
        message = str(response.get("message", ""))
        return "确认" in message or message.casefold() in {"yes", "confirm", "ok"}

    async def cancel(self, task_id: str, analysis_id: str) -> dict[str, Any]:
        record = self._record(task_id, analysis_id)
        if record.status not in NON_TERMINAL:
            return record.to_dict()
        was_awaiting_clarification = record.status == AnalysisStatus.AWAITING_CLARIFICATION
        record.cancel_requested = True
        record.status = AnalysisStatus.CANCEL_REQUESTED
        self._cancel_flags[analysis_id] = True
        if was_awaiting_clarification and record.clarification is not None:
            record.status = AnalysisStatus.CANCELLED
            record.clarification.status = ClarificationStatus.REJECTED
            record.ended_at = _now()
            self._ensure_terminal_evidence(record, AnalysisStatus.CANCELLED, "analysis cancelled")
            self._release_task(task_id, analysis_id)
        return record.to_dict()

    def delete(self, task_id: str, analysis_id: str) -> None:
        record = self._record(task_id, analysis_id)
        if (
            record.status
            not in {
                AnalysisStatus.COMPLETED,
                AnalysisStatus.FAILED,
                AnalysisStatus.TIMED_OUT,
                AnalysisStatus.CANCELLED,
            }
            or not record.resources_settled
        ):
            raise AppError("CANCEL_PENDING", "分析资源尚未释放", 409)
        self.repository.remove_analysis(task_id, analysis_id)

    def snapshot(self, task_id: str, analysis_id: str) -> dict[str, Any]:
        return self._record(task_id, analysis_id).to_dict()

    # ---------- GraphDependencies ----------
    def prepare_context(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        return {"status": AnalysisStatus.RUNNING.value, "dataset_version": dataset.version}

    def retrieve_task_knowledge(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        if self.knowledge_store is None:
            return {"knowledge": [], "knowledge_warnings": []}
        knowledge = self.knowledge_store.search(state["task_id"], state["question"])
        self._check_cancelled(state)
        accepted: list[dict[str, Any]] = []
        warnings: list[str] = []
        for item in knowledge:
            conflict_members = self._knowledge_conflicts(item)
            if conflict_members:
                warnings.append(
                    "knowledge conflict for "
                    + ", ".join(conflict_members)
                    + "; supplemental content ignored"
                )
                continue
            accepted.append(
                {**item, "authority": "supplemental", "may_override_semantics": False}
            )
        return {
            "knowledge": accepted,
            "knowledge_warnings": warnings,
        }

    def _knowledge_conflicts(self, item: dict[str, Any]) -> list[str]:
        """Reject a supplemental rule when it states a conflicting metric unit."""

        text = f"{item.get('title', '')}\n{item.get('content', '')}".casefold()
        declared = re.search(
            r"(?:单位|unit|currency)\s*(?:为|是|=|:|is)?\s*([a-z%]+|[\u4e00-\u9fff]{1,6})",
            text,
        )
        if declared is None:
            return []
        declared_unit = self._normalise_unit(declared.group(1))
        conflicts: list[str] = []
        for member in self.semantic_catalog.metric_definitions():
            labels = (member.metric_id, member.name, *member.aliases)
            if not any(str(label).casefold() in text for label in labels):
                continue
            formal_unit = self._normalise_unit(member.unit)
            if formal_unit and declared_unit and formal_unit != declared_unit:
                conflicts.append(member.metric_id)
        return conflicts

    @staticmethod
    def _normalise_unit(value: str | None) -> str:
        token = str(value or "").casefold().strip()
        return {"cny": "元", "rmb": "元", "yuan": "元", "元": "元"}.get(token, token)

    def _augment_resolution(
        self, question: str, resolution: SemanticResolution
    ) -> SemanticResolution:
        text = question.strip()
        if "客单价" in text:
            resolution.metric_ids = ["average_order_value"]
            resolution.clarification_draft = {
                "metric_id": "average_order_value",
                "name": "客单价",
                "formula": "safe_divide(amount, count)",
                "unit": "元/笔",
            }
        if re.search(r"记录|列出|明细|查询", text):
            resolution.intent = "detail"
        branch_match = re.search(r"([A-Za-z][A-Za-z0-9_-]*)\s*(?:网点|机构|支行|门店)", text)
        if branch_match:
            resolution.filters.append(
                {"semantic_id": "branch", "operator": "==", "value": branch_match.group(1)}
            )
        compare_match = re.search(r"金额大于\s*([0-9]+(?:\.[0-9]+)?)", text)
        if compare_match:
            resolution.filters.append(
                {"semantic_id": "amount", "operator": ">", "value": float(compare_match.group(1))}
            )
        month_match = re.search(r"(20\d{2})年\s*(\d{1,2})月", text)
        if month_match and "比" not in text:
            year, month = int(month_match.group(1)), int(month_match.group(2))
            start = pd.Timestamp(year=year, month=month, day=1)
            end = start + pd.offsets.MonthEnd(1)
            resolution.filters.append(
                {
                    "semantic_id": "date",
                    "operator": "between",
                    "value": {"start": start.isoformat(), "end": end.isoformat()},
                }
            )
        comparison_match = re.search(r"(\d{1,2})月\s*比\s*(\d{1,2})月", text)
        if comparison_match:
            resolution.intent = "compare"
            resolution.comparison = {
                "current_month": int(comparison_match.group(1)),
                "previous_month": int(comparison_match.group(2)),
            }
        if "按月" in text:
            resolution.time_grain = "month"
            resolution.intent = "trend"
            if not resolution.dimension_ids:
                resolution.dimension_ids = ["date"]
        if "按日" in text:
            resolution.time_grain = "day"
            resolution.intent = "trend"
            if not resolution.dimension_ids:
                resolution.dimension_ids = ["date"]
        if re.search(r"最高|最多|排名|排行", text):
            resolution.intent = "rank"
            if not resolution.order_by and resolution.metric_ids:
                resolution.order_by = resolution.metric_ids[0]
        if "占比" in text:
            resolution.intent = "ratio"
        if resolution.aggregation is None:
            if "平均" in text or re.search(r"\b(?:average|mean)\b", text, re.IGNORECASE):
                resolution.aggregation = "mean"
            elif not resolution.dimension_ids and (
                "最小" in text
                or "最低" in text
                or re.search(r"\b(?:minimum|min)\b", text, re.IGNORECASE)
            ):
                resolution.aggregation = "min"
            elif not resolution.dimension_ids and (
                "最大" in text
                or re.search(r"\b(?:maximum|max)\b", text, re.IGNORECASE)
            ):
                resolution.aggregation = "max"
        return resolution

    async def resolve_semantics(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        provider = self.provider_registry.active_provider()
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        frame = self.dataset_store.frame(state["task_id"], state["dataset_id"])
        representative_values: dict[str, list[Any]] = {}
        representative_count = 0
        for field in dataset.physical_schema.fields:
            if representative_count >= 100:
                break
            if field.normalized_name not in frame.columns:
                continue
            values = frame[field.normalized_name].dropna().head(5).tolist()
            values = values[: 100 - representative_count]
            representative_values[field.normalized_name] = [
                value.isoformat() if isinstance(value, pd.Timestamp) else value
                for value in values
            ]
            representative_count += len(values)
        context = {
            "semantic_model": self.semantic_catalog.summary(),
            "dataset_id": state["dataset_id"],
            "physical_schema": {
                "fields": [
                    {
                        key: value
                        for key, value in field.to_dict().items()
                        if key != "representative_values"
                    }
                    for field in dataset.physical_schema.fields
                ]
            },
            "confirmed_bindings": [
                item.to_dict()
                for item in dataset.semantic_bindings
                if item.status == BindingStatus.CONFIRMED
            ],
            "representative_values": representative_values,
            "knowledge": state.get("knowledge", [])[:3],
            "conversation": [
                dict(item) for item in self.repository.require(state["task_id"]).conversation[-20:]
            ],
            "conversation_summary": dict(
                self.repository.require(state["task_id"]).conversation_summary
            ),
        }
        payload = await structured_call(
            provider,
            schema=SemanticResolutionPayload,
            system="只输出SemanticResolution，不输出SQL、Python或物理字段执行代码。",
            user=state["question"],
            context=context,
            timeout_seconds=getattr(provider.capabilities, "timeout_seconds", 60.0),
        )
        self._check_cancelled(state)
        resolution = self._augment_resolution(state["question"], payload.to_domain())
        return {"semantic_resolution": resolution.to_dict()}

    def validate_semantics(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        resolution = SemanticResolution(**state["semantic_resolution"])
        if not 1 <= resolution.limit <= 1000:
            raise AppError("SEMANTIC_RESOLUTION_INVALID", "返回数量超出限制", 422)
        binding_map = {item.semantic_member_id: item for item in dataset.semantic_bindings}
        member_map = {**self.semantic_catalog.members, **self._extra_members(state["task_id"])}

        def contains_physical_reference(value: Any) -> bool:
            if isinstance(value, dict):
                if any(
                    key in value
                    for key in (
                        "column",
                        "physical_field_id",
                        "physical_column",
                        "field_name",
                        "original_name",
                        "normalized_name",
                        "candidate_field_ids",
                    )
                ):
                    return True
                return any(contains_physical_reference(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return any(contains_physical_reference(item) for item in value)
            return False

        # Candidate physical fields are a controlled exception: they are
        # generated by the system and checked below.  All other model output
        # must remain in the semantic vocabulary.
        resolution_without_candidates = resolution.to_dict()
        resolution_without_candidates.pop("binding_candidates", None)
        if contains_physical_reference(resolution_without_candidates):
            raise AppError(
                "SEMANTIC_RESOLUTION_INVALID",
                "模型输出了物理字段引用",
                422,
            )
        missing = []
        ambiguities = []
        for candidate_member_id, candidates in resolution.binding_candidates.items():
            binding = binding_map.get(str(candidate_member_id))
            if binding is None or any(
                str(candidate) not in set(binding.candidate_field_ids)
                for candidate in candidates
            ):
                raise AppError(
                    "SEMANTIC_RESOLUTION_INVALID",
                    "模型输出了未经系统确认的物理字段候选",
                    422,
                )
        if not resolution.metric_ids:
            missing.append("metric")
        for member_id in (*resolution.metric_ids, *resolution.dimension_ids):
            member = member_map.get(member_id)
            if member is None:
                draft_id = str((resolution.clarification_draft or {}).get("metric_id", ""))
                if member_id == draft_id:
                    missing.append(member_id)
                    continue
                raise AppError("SEMANTIC_RESOLUTION_INVALID", "模型输出了未知语义ID", 422)
            binding = binding_map.get(member_id)
            if binding is None or binding.status != BindingStatus.CONFIRMED:
                missing.append(member_id)
                if binding and binding.candidate_field_ids:
                    resolution.binding_candidates[member_id] = list(binding.candidate_field_ids)
        for item in resolution.filters:
            semantic_id = item.get("semantic_id")
            if not semantic_id or contains_physical_reference(item):
                raise AppError("SEMANTIC_RESOLUTION_INVALID", "过滤条件不得直接引用物理字段", 422)
            member = member_map.get(str(semantic_id))
            if member is None:
                raise AppError("SEMANTIC_RESOLUTION_INVALID", "模型输出了未知语义ID", 422)
            binding = binding_map.get(str(semantic_id))
            if binding is None or binding.status != BindingStatus.CONFIRMED:
                missing.append(str(semantic_id))
                if binding and binding.candidate_field_ids:
                    resolution.binding_candidates[str(semantic_id)] = list(
                        binding.candidate_field_ids
                    )
        if resolution.order_by:
            order_member = member_map.get(resolution.order_by)
            if order_member is None or order_member.kind != "metric":
                raise AppError("SEMANTIC_RESOLUTION_INVALID", "排序指标不是有效语义成员", 422)
        if resolution.time_range and resolution.time_range.get("semantic_id"):
            time_member = member_map.get(str(resolution.time_range["semantic_id"]))
            if time_member is None or time_member.kind not in {"dimension", "entity"}:
                raise AppError("SEMANTIC_RESOLUTION_INVALID", "时间字段不是有效语义成员", 422)
        resolution.missing_concepts = sorted(set(missing))
        resolution.ambiguities = sorted(set(ambiguities))
        if resolution.missing_concepts or resolution.ambiguities:
            clarification_id = self.new_id()
            summary = {
                "missing_concepts": resolution.missing_concepts,
                "binding_candidates": resolution.binding_candidates,
                "temporary_metric_draft": resolution.clarification_draft,
                "message": "请确认缺失或歧义的业务概念与字段绑定",
            }
            return {
                "semantic_resolution": resolution.to_dict(),
                "needs_clarification": True,
                "clarification_payload": {
                    "clarification_id": clarification_id,
                    "draft_version": 1,
                    "kind": "semantic_binding",
                    "summary": summary,
                },
            }
        return {"semantic_resolution": resolution.to_dict(), "needs_clarification": False}

    def clarification_payload(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        return dict(state.get("clarification_payload", {}))

    def commit_clarification(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        response = state.get("clarification_response", {})
        confirmed = self._is_confirmed(response)
        if not confirmed:
            if self._is_revision(response):
                resolution = SemanticResolution(**state["semantic_resolution"])
                draft = dict(resolution.clarification_draft or {})
                for key in ("metric_id", "name", "formula", "unit"):
                    if response.get(key) is not None:
                        draft[key] = response[key]
                selected = response.get("physical_field_id")
                if selected:
                    for member_id, candidates in resolution.binding_candidates.items():
                        if selected in candidates:
                            resolution.binding_candidates[member_id] = [selected]
                draft_version = int(
                    state.get("clarification_payload", {}).get("draft_version", 1)
                ) + 1
                clarification_id = self.new_id()
                payload = {
                    "clarification_id": clarification_id,
                    "draft_version": draft_version,
                    "kind": "semantic_binding",
                    "summary": {
                        "missing_concepts": resolution.missing_concepts,
                        "binding_candidates": resolution.binding_candidates,
                        "temporary_metric_draft": draft or None,
                        "message": "请确认修订后的字段绑定或临时指标定义",
                    },
                }
                return {
                    "clarification_done": False,
                    "clarification_revision": True,
                    "needs_clarification": True,
                    "clarification_payload": payload,
                    "semantic_resolution": resolution.to_dict(),
                }
            return {"clarification_done": False, "status": AnalysisStatus.CANCELLED.value}
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        resolution = SemanticResolution(**state["semantic_resolution"])

        # Validate every user-selected binding before mutating either the
        # task metric extensions or Dataset bindings.  A failed confirmation
        # must not leave a partially committed semantic draft behind.
        selected = response.get("physical_field_id")
        if selected and not any(
            selected in candidates for candidates in resolution.binding_candidates.values()
        ):
            raise AppError("CLARIFICATION_REQUIRED", "请选择系统提供的物理字段", 409)
        binding_choices: list[tuple[str, str]] = []
        for member_id, candidates in resolution.binding_candidates.items():
            if not candidates:
                continue
            field_id = (
                selected
                if selected in candidates
                else (candidates[0] if len(candidates) == 1 else None)
            )
            if field_id is None:
                raise AppError("CLARIFICATION_REQUIRED", "请明确选择物理字段", 409)
            if not any(
                binding.semantic_member_id == member_id
                for binding in dataset.semantic_bindings
            ):
                raise AppError("SEMANTIC_BINDING_REQUIRED", "语义绑定不存在", 409)
            member = self.semantic_catalog.get_member(member_id)
            field = next(
                (
                    item
                    for item in dataset.physical_schema.fields
                    if item.field_id == field_id
                ),
                None,
            )
            if field is None:
                raise AppError("PHYSICAL_FIELD_NOT_FOUND", "物理字段不存在", 404)
            if member is not None and not self.semantic_catalog._type_compatible(
                field.physical_type, member.allowed_types
            ):
                raise AppError("SEMANTIC_BINDING_TYPE_MISMATCH", "物理字段类型不兼容", 422)
            binding_choices.append((member_id, field_id))

        if resolution.clarification_draft:
            draft = dict(resolution.clarification_draft)
            original_metric_id = str(draft.get("metric_id", ""))
            for key in ("metric_id", "name", "formula", "unit"):
                if response.get(key) is not None:
                    draft[key] = response[key]
            metric = self.confirm_temporary_metric(
                state["task_id"],
                state["dataset_id"],
                metric_id=str(draft["metric_id"]),
                name=str(draft.get("name", draft["metric_id"])),
                formula=str(draft["formula"]),
                unit=draft.get("unit"),
                confirmed_binding_ids={member_id for member_id, _ in binding_choices},
            )
            resolution.metric_ids = [
                metric["metric_id"] if item == original_metric_id else item
                for item in resolution.metric_ids
            ]
            resolution.clarification_draft = None
        # Only the explicit confirmation node writes the selected bindings.
        for member_id, field_id in binding_choices:
            for binding in dataset.semantic_bindings:
                if binding.semantic_member_id == member_id:
                    binding.physical_field_id = field_id
                    binding.status = BindingStatus.CONFIRMED
                    binding.source = "user"
                    break
        return {
            "clarification_done": True,
            "needs_clarification": False,
            "semantic_resolution": resolution.to_dict(),
        }

    def _is_revision(self, response: dict[str, Any]) -> bool:
        if any(
            response.get(key) is not None
            for key in ("physical_field_id", "formula", "metric_id", "unit")
        ):
            return True
        message = str(response.get("message", ""))
        revision_words = ("修改", "改成", "修订", "revise", "change")
        return any(word in message.casefold() for word in revision_words)

    def build_query_plan(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        resolution = SemanticResolution(**state["semantic_resolution"])
        filters = tuple(
            QueryFilter(
                semantic_id=str(item["semantic_id"]),
                operator=str(item["operator"]),
                value=item.get("value"),
            )
            for item in resolution.filters
        )
        if resolution.time_range:
            range_id = str(resolution.time_range.get("semantic_id", "date"))
            if not any(item.semantic_id == range_id for item in filters):
                filters += (
                    QueryFilter(
                        semantic_id=range_id,
                        operator="between",
                        value={
                            "start": resolution.time_range.get(
                                "start", resolution.time_range.get("from")
                            ),
                            "end": resolution.time_range.get(
                                "end", resolution.time_range.get("to")
                            ),
                        },
                    ),
                )
        operation = resolution.intent
        if operation not in {
            "detail",
            "filter",
            "aggregate",
            "group",
            "rank",
            "trend",
            "compare",
            "ratio",
        }:
            operation = "aggregate"
        if operation == "aggregate" and resolution.dimension_ids:
            operation = "group"
        common = {
            "metric_ids": tuple(resolution.metric_ids),
            "dimension_ids": tuple(resolution.dimension_ids),
            "time_grain": resolution.time_grain,
            "aggregation": resolution.aggregation,
            "sort_metric_id": resolution.order_by,
            "ascending": resolution.ascending,
            "limit": max(1, min(resolution.limit, 1000)),
        }
        if operation == "compare" and resolution.comparison:
            year_match = re.search(r"20\d{2}", state["question"])
            year = int(year_match.group(0)) if year_match else 2024
            steps = []
            for month in (
                resolution.comparison["current_month"],
                resolution.comparison["previous_month"],
            ):
                start = pd.Timestamp(year=year, month=month, day=1)
                end = start + pd.offsets.MonthEnd(1)
                period_filters = tuple(filters) + (
                    QueryFilter(
                        semantic_id="date",
                        operator="between",
                        value={"start": start.isoformat(), "end": end.isoformat()},
                    ),
                )
                steps.append(
                    QueryStep(operation="aggregate", filters=period_filters, **common)
                )
            plan_queries = tuple(steps)
        else:
            plan_queries = (
                QueryStep(operation=operation, filters=filters, **common),
            )
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        bindings = [
            {
                key: value
                for key, value in binding.to_dict().items()
                if key not in {"physical_field_id", "candidate_field_ids", "confirmed_at"}
            }
            for binding in dataset.semantic_bindings
            if binding.semantic_member_id
            in set(
                resolution.metric_ids
                + resolution.dimension_ids
                + [item.semantic_id for item in filters]
            )
        ]
        plan = QueryPlan(
            plan_id=self.new_id(),
            task_id=state["task_id"],
            dataset_id=state["dataset_id"],
            semantic_model_version=self.semantic_catalog.model.version,
            intent=operation,
            queries=plan_queries,
            calculations=(),
            chart_intent={
                "dimension": resolution.dimension_ids[0] if resolution.dimension_ids else None,
                "metrics": resolution.metric_ids,
            },
            binding_snapshot=tuple(bindings),
        )
        return {"query_plan": plan.to_dict()}

    def _plan_from_dict(self, value: dict[str, Any]) -> QueryPlan:
        queries = []
        for item in value.get("queries", []):
            queries.append(
                QueryStep(
                    operation=item.get("operation", "aggregate"),
                    metric_ids=tuple(item.get("metric_ids", [])),
                    dimension_ids=tuple(item.get("dimension_ids", [])),
                    filters=tuple(
                        QueryFilter(**filter_item) for filter_item in item.get("filters", [])
                    ),
                    time_grain=item.get("time_grain"),
                    aggregation=item.get("aggregation"),
                    sort_metric_id=item.get("sort_metric_id"),
                    ascending=item.get("ascending", False),
                    limit=item.get("limit", 20),
                )
            )
        return QueryPlan(
            plan_id=value["plan_id"],
            task_id=value["task_id"],
            dataset_id=value["dataset_id"],
            semantic_model_version=value["semantic_model_version"],
            intent=value["intent"],
            queries=tuple(queries),
            calculations=tuple(value.get("calculations", [])),
            chart_intent=value.get("chart_intent"),
            binding_snapshot=tuple(value.get("binding_snapshot", [])),
        )

    def validate_query_plan(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        plan = self._plan_from_dict(state["query_plan"])
        self.plan_validator.validate(
            plan,
            dataset=dataset,
            catalog=self.semantic_catalog,
            bindings=dataset.semantic_bindings,
            extra_members=self._extra_members(state["task_id"]),
        )
        return {"query_plan": plan.to_dict()}

    async def execute_query_plan(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        frame = self.dataset_store.frame(state["task_id"], state["dataset_id"])
        plan = self._plan_from_dict(state["query_plan"])
        loop = asyncio.get_running_loop()
        record = self._record(state["task_id"], state["analysis_id"])
        record.pending_resources += 1
        record.resources_settled = False
        query_future = loop.run_in_executor(
            self.executor,
            lambda: self.query_executor.execute(
                plan,
                dataset=dataset,
                frame=frame,
                catalog=self.semantic_catalog,
                bindings=dataset.semantic_bindings,
                extra_members=self._extra_members(state["task_id"]),
                cancel_check=lambda: self._cancel_flags.get(state["analysis_id"], False),
            ),
        )
        self._background_tasks.add(query_future)

        def settle_query(done: asyncio.Future[Any]) -> None:
            self._background_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass
            record.pending_resources = max(0, record.pending_resources - 1)
            record.resources_settled = record.pending_resources == 0
            if record.status not in NON_TERMINAL and record.resources_settled:
                self._release_task(record.task_id, record.analysis_id)

        query_future.add_done_callback(settle_query)
        try:
            output = await asyncio.wait_for(
                asyncio.shield(query_future),
                timeout=self.query_executor.step_timeout_seconds * max(1, len(plan.queries)),
            )
        except asyncio.TimeoutError as exc:
            self._cancel_flags[state["analysis_id"]] = True
            raise QueryTimeout("QueryPlan步骤超过时间限制") from exc
        self._check_cancelled(state)
        return {"execution": output.__dict__}

    def build_evidence(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        dataset = self.dataset_store.require(state["task_id"], state["dataset_id"])
        task = self.repository.require(state["task_id"])
        execution = state.get("execution", {})
        plan = state["query_plan"]
        join_info = next(
            (
                dict(item)
                for item in dataset.normalization_records
                if item.get("rule") == "safe_join"
            ),
            None,
        )
        plan_filters = [
            filter_item
            for query in plan.get("queries", [])
            for filter_item in query.get("filters", [])
        ]
        evidence = AnalysisEvidence(
            analysis_id=state["analysis_id"],
            task_id=state["task_id"],
            dataset_id=state["dataset_id"],
            dataset_version=dataset.version,
            question=state["question"],
            semantic_model_version=self.semantic_catalog.model.version,
            semantic_resolution=state["semantic_resolution"],
            metric_definitions=self._metric_definition_snapshots(
                state["task_id"], state["semantic_resolution"].get("metric_ids", [])
            ),
            binding_snapshot=[item.to_dict() for item in dataset.semantic_bindings],
            query_plan=plan,
            filters=plan_filters,
            intermediate_values=execution.get("intermediate_values", []),
            result=execution.get("result", {}),
            warnings=list(state.get("knowledge_warnings", []))
            + list(execution.get("warnings", [])),
            input_rows=execution.get("input_rows", 0),
            output_rows=execution.get("output_rows", 0),
            timings_ms=execution.get("timings_ms", {}),
            join_info=join_info,
            temporary_metrics=[dict(item) for item in task.semantic_extensions],
        )
        return {"evidence": evidence.to_dict()}

    async def generate_answer(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        provider = self.provider_registry.active_provider()
        evidence = state.get("evidence", {})
        if hasattr(provider, "answer") and provider.__class__.__name__ == "MockModelProvider":
            answer_call = provider.answer(
                system="只根据Evidence回答",
                user=state["question"],
                context={"result": evidence.get("result", {})},
            )
        else:
            answer_call = provider.answer(
                system="只根据Evidence回答，不编造数字",
                user=state["question"],
                context={"evidence": evidence},
            )
        try:
            answer = await asyncio.wait_for(
                answer_call,
                timeout=max(
                    0.001,
                    min(
                        float(getattr(provider.capabilities, "timeout_seconds", 60.0)),
                        60.0,
                    ),
                ),
            )
        except asyncio.TimeoutError as exc:
            raise AppError("MODEL_TIMEOUT", "模型回答生成超时", 504) from exc
        self._check_cancelled(state)
        return {"answer": answer}

    def build_chart_spec(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        resolution = SemanticResolution(**state["semantic_resolution"])
        evidence = state.get("evidence", {})
        member = (
            self.semantic_catalog.get_member(resolution.metric_ids[0])
            if resolution.metric_ids
            else None
        )
        if member is None and resolution.metric_ids:
            member = self._extra_members(state["task_id"]).get(resolution.metric_ids[0])
        chart_metrics = list(resolution.metric_ids)
        if resolution.intent == "ratio":
            result_columns = set((evidence.get("result") or {}).get("columns", []))
            ratio_metrics = [
                f"{metric_id}_ratio"
                for metric_id in resolution.metric_ids
                if f"{metric_id}_ratio" in result_columns
            ]
            if ratio_metrics:
                chart_metrics = ratio_metrics
        elif resolution.intent == "compare":
            result_columns = set((evidence.get("result") or {}).get("columns", []))
            if "difference" in result_columns:
                chart_metrics = ["difference"]
        chart = chart_from_evidence(
            evidence_result=evidence.get("result", {}),
            dimension=resolution.dimension_ids[0] if resolution.dimension_ids else None,
            metrics=chart_metrics,
            title=state["question"],
            unit="%" if resolution.intent == "ratio" else (member.unit if member else None),
        )
        return {"chart": chart.to_dict() if chart else None}

    def finalize(self, state: AgentState) -> dict[str, Any]:
        self._check_cancelled(state)
        return {"status": state.get("status", AnalysisStatus.COMPLETED.value)}

    async def stream_events(
        self,
        task_id: str,
        *,
        dataset_id: str,
        question: str,
        clarification_id: str | None = None,
        analysis_id: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        record: AnalysisRecord | None = None
        if clarification_id is None:
            if not str(question).strip():
                raise AppError("QUESTION_EMPTY", "问题不能为空", 422)
            active = self.repository.active_analysis(task_id)
            if active is not None and active.status == AnalysisStatus.AWAITING_CLARIFICATION:
                self._terminate_waiting_clarification(active)
            record = await self._create_record(task_id, dataset_id, question)
            analysis_id = record.analysis_id
        elif analysis_id is None:
            active = self.repository.active_analysis(task_id)
            analysis_id = active.analysis_id if active is not None else None
        yield {"event": "started", "data": {"analysis_id": analysis_id}}
        yield {"event": "semantic_resolving", "data": {"question": question}}
        background = asyncio.create_task(
            self._run_record(record)
            if record is not None
            else self.submit(
                task_id,
                dataset_id=dataset_id,
                question=question,
                clarification_id=clarification_id,
                analysis_id=analysis_id,
                response=response,
            )
        )
        self._background_tasks.add(background)

        def consume_background(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        background.add_done_callback(consume_background)
        try:
            snapshot = await asyncio.shield(background)
        except asyncio.CancelledError:
            # SSE断开只取消订阅；后台分析继续执行，结果由analysis_id查询。
            return
        if snapshot["status"] == AnalysisStatus.AWAITING_CLARIFICATION.value:
            yield {"event": "clarification_required", "data": snapshot["clarification"]}
            yield {
                "event": "done",
                "data": {"analysis_id": snapshot["analysis_id"], "status": snapshot["status"]},
            }
            return
        if snapshot["status"] != AnalysisStatus.COMPLETED.value:
            yield {
                "event": "error",
                "data": snapshot.get("error") or {"code": "ANALYSIS_FAILED", "message": "分析失败"},
            }
            yield {
                "event": "done",
                "data": {"analysis_id": snapshot["analysis_id"], "status": snapshot["status"]},
            }
            return
        evidence = snapshot.get("evidence") or {}
        yield {
            "event": "plan_validated",
            "data": {
                "analysis_id": snapshot["analysis_id"],
                "query_plan": evidence.get("query_plan"),
            },
        }
        yield {
            "event": "query_executed",
            "data": {"analysis_id": snapshot["analysis_id"], "result": evidence.get("result")},
        }
        yield {"event": "evidence", "data": evidence}
        answer = snapshot.get("answer") or ""
        for sequence, start in enumerate(range(0, len(answer), 80)):
            yield {
                "event": "answer_delta",
                "data": {
                    "analysis_id": snapshot["analysis_id"],
                    "sequence": sequence,
                    "text": answer[start : start + 80],
                },
            }
        yield {
            "event": "answer",
            "data": {"analysis_id": snapshot["analysis_id"], "answer": answer},
        }
        if snapshot.get("chart"):
            yield {"event": "chart", "data": snapshot["chart"]}
        yield {
            "event": "done",
            "data": {"analysis_id": snapshot["analysis_id"], "status": snapshot["status"]},
        }


__all__ = ["AnalysisService"]
