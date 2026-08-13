"""阶段2唯一LangGraph StateGraph。

图本身只负责节点顺序、澄清中断和可序列化状态；业务依赖由
``GraphDependencies`` 注入，避免节点直接持有DataFrame或模型客户端。
"""

from __future__ import annotations

import time
from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt


class AgentState(TypedDict, total=False):
    task_id: str
    analysis_id: str
    dataset_id: str
    question: str
    current_stage: str
    knowledge: list[dict[str, Any]]
    knowledge_warnings: list[str]
    semantic_resolution: dict[str, Any]
    needs_clarification: bool
    clarification_payload: dict[str, Any]
    clarification_response: dict[str, Any]
    clarification_done: bool
    query_plan: dict[str, Any]
    execution: dict[str, Any]
    evidence: dict[str, Any]
    answer: str
    chart: dict[str, Any] | None
    status: str
    error: dict[str, Any] | None
    timings_ms: dict[str, float]


class GraphDependencies(Protocol):
    def prepare_context(self, state: AgentState) -> dict[str, Any]: ...

    def retrieve_task_knowledge(self, state: AgentState) -> dict[str, Any]: ...

    async def resolve_semantics(self, state: AgentState) -> dict[str, Any]: ...

    def validate_semantics(self, state: AgentState) -> dict[str, Any]: ...

    def clarification_payload(self, state: AgentState) -> dict[str, Any]: ...

    def commit_clarification(self, state: AgentState) -> dict[str, Any]: ...

    def build_query_plan(self, state: AgentState) -> dict[str, Any]: ...

    def validate_query_plan(self, state: AgentState) -> dict[str, Any]: ...

    async def execute_query_plan(self, state: AgentState) -> dict[str, Any]: ...

    def build_evidence(self, state: AgentState) -> dict[str, Any]: ...

    async def generate_answer(self, state: AgentState) -> dict[str, Any]: ...

    def build_chart_spec(self, state: AgentState) -> dict[str, Any]: ...

    def finalize(self, state: AgentState) -> dict[str, Any]: ...


def _with_timing(
    state: AgentState,
    result: dict[str, Any],
    node: str,
    started: float,
) -> dict[str, Any]:
    timings = dict(state.get("timings_ms", {}))
    timings[node] = round((time.perf_counter() - started) * 1000, 2)
    result["timings_ms"] = timings
    return result


def build_analysis_graph(
    dependencies: GraphDependencies,
    *,
    checkpointer: Any | None = None,
):
    """编译唯一分析图；同一个Container内只应调用一次。"""

    def prepare_context(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {"current_stage": "prepare_context", **dependencies.prepare_context(state)}
        return _with_timing(state, result, "prepare_context", started)

    def retrieve_task_knowledge(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {
            "current_stage": "retrieve_task_knowledge",
            **dependencies.retrieve_task_knowledge(state),
        }
        return _with_timing(state, result, "retrieve_task_knowledge", started)

    async def resolve_semantics(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {
            "current_stage": "resolve_semantics",
            **await dependencies.resolve_semantics(state),
        }
        return _with_timing(state, result, "resolve_semantics", started)

    def validate_semantics(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = dependencies.validate_semantics(state)
        return _with_timing(
            state,
            {"current_stage": "validate_semantics", **result},
            "validate_semantics",
            started,
        )

    def clarify_if_required(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        if not state.get("needs_clarification"):
            return _with_timing(
                state,
                {"current_stage": "clarify_if_required", "clarification_done": True},
                "clarify_if_required",
                started,
            )
        payload = dependencies.clarification_payload(state)
        response = interrupt(payload)
        return _with_timing(
            state,
            {
                "current_stage": "clarify_if_required",
                "clarification_payload": payload,
                "clarification_response": response
                if isinstance(response, dict)
                else {"value": response},
            },
            "clarify_if_required",
            started,
        )

    def commit_clarification(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = dependencies.commit_clarification(state)
        return _with_timing(
            state,
            {"current_stage": "commit_clarification", **result},
            "commit_clarification",
            started,
        )

    def build_query_plan(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {"current_stage": "build_query_plan", **dependencies.build_query_plan(state)}
        return _with_timing(state, result, "build_query_plan", started)

    def validate_query_plan(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {
            "current_stage": "validate_query_plan",
            **dependencies.validate_query_plan(state),
        }
        return _with_timing(state, result, "validate_query_plan", started)

    async def execute_query_plan(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {
            "current_stage": "execute_query_plan",
            **await dependencies.execute_query_plan(state),
        }
        return _with_timing(state, result, "execute_query_plan", started)

    def build_evidence(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {"current_stage": "build_evidence", **dependencies.build_evidence(state)}
        return _with_timing(state, result, "build_evidence", started)

    async def generate_answer(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {
            "current_stage": "generate_answer",
            **await dependencies.generate_answer(state),
        }
        return _with_timing(state, result, "generate_answer", started)

    def build_chart_spec(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {"current_stage": "build_chart_spec", **dependencies.build_chart_spec(state)}
        return _with_timing(state, result, "build_chart_spec", started)

    def finalize(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        result = {"current_stage": "finalize", **dependencies.finalize(state)}
        return _with_timing(state, result, "finalize", started)

    graph = StateGraph(AgentState)
    for name, node in (
        ("prepare_context", prepare_context),
        ("retrieve_task_knowledge", retrieve_task_knowledge),
        ("resolve_semantics", resolve_semantics),
        ("validate_semantics", validate_semantics),
        ("clarify_if_required", clarify_if_required),
        ("commit_clarification", commit_clarification),
        ("build_query_plan", build_query_plan),
        ("validate_query_plan", validate_query_plan),
        ("execute_query_plan", execute_query_plan),
        ("build_evidence", build_evidence),
        ("generate_answer", generate_answer),
        ("build_chart_spec", build_chart_spec),
        ("finalize", finalize),
    ):
        graph.add_node(name, node)
    graph.set_entry_point("prepare_context")
    graph.add_edge("prepare_context", "retrieve_task_knowledge")
    graph.add_edge("retrieve_task_knowledge", "resolve_semantics")
    graph.add_edge("resolve_semantics", "validate_semantics")
    graph.add_conditional_edges(
        "validate_semantics",
        lambda state: "clarify" if state.get("needs_clarification") else "plan",
        {"clarify": "clarify_if_required", "plan": "build_query_plan"},
    )
    graph.add_edge("clarify_if_required", "commit_clarification")
    graph.add_conditional_edges(
        "commit_clarification",
        lambda state: (
            "plan"
            if state.get("clarification_done")
            else ("clarify" if state.get("clarification_revision") else "finish")
        ),
        {"plan": "build_query_plan", "clarify": "clarify_if_required", "finish": "finalize"},
    )
    graph.add_edge("build_query_plan", "validate_query_plan")
    graph.add_edge("validate_query_plan", "execute_query_plan")
    graph.add_edge("execute_query_plan", "build_evidence")
    graph.add_edge("build_evidence", "generate_answer")
    graph.add_edge("generate_answer", "build_chart_spec")
    graph.add_edge("build_chart_spec", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


__all__ = ["AgentState", "GraphDependencies", "build_analysis_graph"]
