from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from excel_agent.api import app
from excel_agent.config import (
    AppConfig,
    ModelConfig,
    ProviderConfig,
    get_config,
    set_config,
)
from excel_agent.errors import AppError
from excel_agent.infrastructure.model_provider import ModelProviderCapabilities

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def mock_model():
    original = get_config()
    set_config(
        AppConfig(
            model=ModelConfig(
                providers={"default": ProviderConfig(provider="mock")},
            )
        )
    )
    try:
        yield
    finally:
        set_config(original)


def _dataset(client: TestClient) -> tuple[str, str]:
    task = client.post("/api/v2/tasks", json={"name": "stage2"}).json()
    task_id = task["task_id"]
    upload = client.post(
        f"/api/v2/tasks/{task_id}/uploads",
        files={"file": ("sample.csv", (FIXTURES / "sample.csv").read_bytes(), "text/csv")},
    ).json()
    dataset = client.post(
        f"/api/v2/tasks/{task_id}/datasets",
        json={"upload_id": upload["upload_id"]},
    ).json()
    return task_id, dataset["dataset_id"]


def test_stage2_chat_evidence_and_chart(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "按网点汇总金额"},
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["status"] == "completed"
        assert snapshot["evidence"]["semantic_model_version"] == "v1"
        assert snapshot["evidence"]["result"]["data"][0] == {
            "branch": "B",
            "amount": 230.0,
        }
        assert snapshot["chart"]["chart_type"] == "bar"
        assert snapshot["chart"]["data"][0]["amount"] == 230.0
        analysis = client.get(
            f"/api/v2/tasks/{task_id}/analyses/{snapshot['analysis_id']}"
        )
        assert analysis.status_code == 200
        assert analysis.json()["answer"]


def test_stage2_model_context_is_schema_bound_and_bounded(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "group amount by branch"},
        )
        assert response.status_code == 200
        provider = client.app.state.container.provider_registry.active_provider()
        context = provider.calls[-1]["context"]
        assert context["physical_schema"]["fields"]
        assert all(
            "representative_values" not in field
            for field in context["physical_schema"]["fields"]
        )
        assert context["confirmed_bindings"]
        assert sum(len(values) for values in context["representative_values"].values()) <= 100
        assert "frame" not in context
        container_task = client.app.state.container.task_repository.require(task_id)
        assert len(container_task.conversation) <= 20


def test_stage2_stream_has_monotonic_answer_delta(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat/stream",
            json={"dataset_id": dataset_id, "message": "按网点汇总金额"},
        )
        assert response.status_code == 200
        events = [line for line in response.text.splitlines() if line.startswith("event: ")]
        names = [line.split(": ", 1)[1] for line in events]
        assert names[0:2] == ["started", "semantic_resolving"]
        started_data = __import__("json").loads(response.text.splitlines()[1][6:])
        assert started_data["analysis_id"]
        assert "evidence" in names
        assert names[-1] == "done"
        sequences = []
        lines = response.text.splitlines()
        for index, line in enumerate(lines):
            if line == "event: answer_delta":
                sequences.append(int(__import__("json").loads(lines[index + 1][6:])["sequence"]))
        assert sequences == sorted(sequences)


def test_stage2_compare_ratio_and_temp_metric_confirmation(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        ratio = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "A网点金额占比"},
        ).json()
        assert ratio["status"] == "completed"
        assert ratio["evidence"]["result"]["data"][0]["amount_ratio"] == pytest.approx(
            48.888888, rel=1e-5
        )
        assert ratio["chart"]["metrics"] == ["amount_ratio"]
        assert ratio["chart"]["data"][0]["amount_ratio"] == pytest.approx(
            48.888888, rel=1e-5
        )

        comparison = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "2024年2月比1月金额变化"},
        ).json()
        assert comparison["evidence"]["result"]["data"][0]["difference"] == 10.0

        pending = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "客单价是多少"},
        ).json()
        assert pending["status"] == "awaiting_clarification"
        assert pending["evidence"]["status"] == "awaiting_clarification"
        assert "clarification required" in pending["evidence"]["warnings"]
        confirmed = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={
                "dataset_id": dataset_id,
                "message": "确认",
                "analysis_id": pending["analysis_id"],
                "clarification_id": pending["clarification"]["clarification_id"],
                "confirm": True,
            },
        ).json()
        assert confirmed["status"] == "completed"
        assert confirmed["evidence"]["result"]["data"][0][
            f"task:{task_id}:average_order_value"
        ] == 10.0
        assert confirmed["evidence"]["temporary_metrics"][0]["confirmed"] is True


def test_stage2_join_and_task_knowledge_are_isolated(mock_model):
    with TestClient(app) as client:
        task_id, left_id = _dataset(client)
        content = "# 金额口径\n订单金额单位为元，按订单金额求和。".encode("utf-8")
        uploaded = client.post(
            f"/api/v2/tasks/{task_id}/knowledge/documents",
            files={"file": ("rule.md", content, "text/markdown")},
        )
        assert uploaded.status_code == 200
        document_id = uploaded.json()["document_id"]
        listed = client.get(f"/api/v2/tasks/{task_id}/knowledge/documents")
        assert listed.json()["documents"][0]["document_id"] == document_id
        assert client.get(
            f"/api/v2/tasks/{task_id}/knowledge/documents/{document_id}"
        ).json()["content"].startswith("# 金额")

        conflict = client.post(
            f"/api/v2/tasks/{task_id}/knowledge/documents",
            files={
                "file": (
                    "conflict.md",
                    "# 金额\n金额单位为美元。".encode("utf-8"),
                    "text/markdown",
                )
            },
        )
        assert conflict.status_code == 200
        conflict_analysis = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": left_id, "message": "汇总金额"},
        )
        assert conflict_analysis.status_code == 200
        assert any(
            "knowledge conflict for amount" in warning
            for warning in conflict_analysis.json()["evidence"]["warnings"]
        )

        foreign = client.post("/api/v2/tasks", json={"name": "foreign"}).json()["task_id"]
        assert client.get(
            f"/api/v2/tasks/{foreign}/knowledge/documents/{document_id}"
        ).status_code == 404

        lookup_upload = client.post(
            f"/api/v2/tasks/{task_id}/uploads",
            files={
                "file": (
                    "lookup.csv",
                    b"branch,label\nA,alpha\nB,beta\n",
                    "text/csv",
                )
            },
        ).json()
        lookup = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": lookup_upload["upload_id"]},
        ).json()
        joined_ok = client.post(
            f"/api/v2/tasks/{task_id}/joins",
            json={
                "left_dataset_id": left_id,
                "right_dataset_id": lookup["dataset_id"],
                "left_keys": ["branch"],
                "right_keys": ["branch"],
                "display_name": "joined-ok",
            },
        )
        assert joined_ok.status_code == 200
        assert joined_ok.json()["status"] == "ready"
        assert any(
            item["rule"] == "safe_join"
            for item in joined_ok.json()["normalization_records"]
        )

        joined = client.post(
            f"/api/v2/tasks/{task_id}/joins",
            json={
                "left_dataset_id": left_id,
                "right_dataset_id": left_id,
                "left_keys": ["branch"],
                "right_keys": ["branch"],
                "display_name": "joined",
            },
        )
        assert joined.status_code == 409
        assert joined.json()["code"] == "JOIN_MANY_TO_MANY_BLOCKED"


def test_stage2_failure_keeps_terminal_evidence(mock_model):
    class FailingProvider:
        name = "failing"
        capabilities = ModelProviderCapabilities("native", False)

        async def structured(self, **kwargs):
            raise AppError("MODEL_ERROR", "provider failure", 502)

        async def answer(self, **kwargs):
            return ""

    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        container = client.app.state.container
        container.provider_registry.register("default", FailingProvider())
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "汇总金额"},
        )
        assert response.status_code == 502
        record = container.task_repository.list_analyses(task_id)[0]
        assert record.status.value == "failed"
        assert record.evidence is not None
        assert record.evidence.status.value == "failed"
        assert "analysis failed" in record.evidence.warnings


def test_stage2_answer_timeout_is_terminal_and_queryable(mock_model):
    class SlowAnswerProvider:
        name = "slow-answer"
        capabilities = ModelProviderCapabilities("native", False, 0.01)

        async def structured(self, **kwargs):
            return {"intent": "aggregate", "metric_ids": ["amount"]}

        async def answer(self, **kwargs):
            await asyncio.sleep(0.05)
            return "late"

    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        container = client.app.state.container
        container.provider_registry.register("default", SlowAnswerProvider())
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "汇总金额"},
        )
        assert response.status_code == 504
        analysis_id = container.task_repository.list_analyses(task_id)[0].analysis_id
        snapshot = client.get(
            f"/api/v2/tasks/{task_id}/analyses/{analysis_id}"
        ).json()
        assert snapshot["status"] == "timed_out"
        assert snapshot["evidence"]["status"] == "timed_out"


def test_stage2_stream_failure_done_keeps_analysis_id(mock_model):
    class FailingProvider:
        name = "stream-failing"
        capabilities = ModelProviderCapabilities("native", False)

        async def structured(self, **kwargs):
            raise AppError("MODEL_ERROR", "provider failure", 502)

        async def answer(self, **kwargs):
            return ""

    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        container = client.app.state.container
        container.provider_registry.register("default", FailingProvider())
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat/stream",
            json={"dataset_id": dataset_id, "message": "汇总金额"},
        )
        assert response.status_code == 200
        lines = response.text.splitlines()
        started = __import__("json").loads(lines[1][6:])["analysis_id"]
        done_index = max(index for index, line in enumerate(lines) if line == "event: done")
        done = __import__("json").loads(lines[done_index + 1][6:])
        assert started and done["analysis_id"] == started
        assert done["status"] == "failed"


def test_stage2_clarification_revision_and_new_question_supersede(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        pending = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "客单价是多少"},
        ).json()
        first_id = pending["clarification"]["clarification_id"]
        revised = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={
                "dataset_id": dataset_id,
                "message": "修改公式",
                "analysis_id": pending["analysis_id"],
                "clarification_id": first_id,
                "draft_version": pending["clarification"]["draft_version"],
                "confirm": False,
                "formula": "safe_divide(amount, count) * 2",
            },
        ).json()
        assert revised["status"] == "awaiting_clarification"
        assert revised["clarification"]["clarification_id"] != first_id
        old_analysis_id = revised["analysis_id"]

        completed = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "按网点汇总金额"},
        ).json()
        assert completed["status"] == "completed"
        old = client.get(
            f"/api/v2/tasks/{task_id}/analyses/{old_analysis_id}"
        ).json()
        assert old["status"] == "cancelled"
        assert "superseded" in old["evidence"]["warnings"][0]
        stale = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={
                "dataset_id": dataset_id,
                "message": "确认",
                "analysis_id": pending["analysis_id"],
                "clarification_id": first_id,
                "confirm": True,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "CLARIFICATION_MISMATCH"


def test_stage2_query_timeout_keeps_task_busy_until_worker_finishes(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        service = client.app.state.container.analysis_service
        original = service.query_executor

        class SlowExecutor:
            step_timeout_seconds = 0.01

            def execute(self, *args, **kwargs):
                time.sleep(0.08)
                return original.execute(*args, **kwargs)

        service.query_executor = SlowExecutor()
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "汇总金额"},
        )
        assert response.status_code == 408
        record = client.app.state.container.task_repository.list_analyses(task_id)[0]
        assert record.status.value == "timed_out"
        assert record.resources_settled is False
        time.sleep(0.12)
        assert client.app.state.container.task_repository.require(task_id).busy is False


def test_stage2_delete_busy_task_preserves_analysis_resources(mock_model):
    with TestClient(app) as client:
        task_id, _ = _dataset(client)
        task = client.app.state.container.task_repository.require(task_id)
        task.busy = True
        response = client.delete(f"/api/v2/tasks/{task_id}")
        assert response.status_code == 409
        assert response.json()["code"] == "TASK_MUTATION_BLOCKED"
        preserved = client.app.state.container.task_repository.require(task_id)
        assert preserved.busy is True
        assert preserved.status.value == "active"
        preserved.busy = False


def test_stage2_invalid_temporary_metric_does_not_partially_commit(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        pending = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "客单价是多少"},
        ).json()
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={
                "dataset_id": dataset_id,
                "message": "确认",
                "analysis_id": pending["analysis_id"],
                "clarification_id": pending["clarification"]["clarification_id"],
                "confirm": True,
                "formula": "amount + count",
            },
        )
        assert response.status_code == 422
        task = client.app.state.container.task_repository.require(task_id)
        assert task.semantic_extensions == []


def test_stage2_unknown_semantic_member_returns_structured_error(mock_model):
    with TestClient(app) as client:
        task_id, dataset_id = _dataset(client)
        response = client.post(
            f"/api/v2/tasks/{task_id}/chat",
            json={"dataset_id": dataset_id, "message": "查询不存在的客户字段"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "SEMANTIC_RESOLUTION_INVALID"
        assert body["request_id"]
