from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from excel_agent.api import app

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _create_task(client: TestClient, name: str = "stage1") -> str:
    response = client.post("/api/v2/tasks", json={"name": name})
    assert response.status_code == 200
    return response.json()["task_id"]


def test_stage1_csv_upload_dataset_profile_preview_and_binding():
    with TestClient(app) as client:
        task_id = _create_task(client)
        upload = client.post(
            f"/api/v2/tasks/{task_id}/uploads",
            files={"file": ("sample.csv", (FIXTURES / "sample.csv").read_bytes(), "text/csv")},
        )
        assert upload.status_code == 200
        inspection = upload.json()
        assert inspection["format"] == "csv"
        dataset = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": inspection["upload_id"]},
        )
        assert dataset.status_code == 200
        normalized = dataset.json()
        assert normalized["status"] == "ready"
        assert normalized["parent_dataset_id"]

        profile = client.get(
            f"/api/v2/tasks/{task_id}/datasets/{normalized['dataset_id']}/profile"
        )
        assert profile.status_code == 200
        assert profile.json()["row_count"] == 4
        preview = client.get(
            f"/api/v2/tasks/{task_id}/datasets/{normalized['dataset_id']}/preview?limit=2"
        )
        assert preview.status_code == 200
        assert len(preview.json()["rows"]) == 2
        bindings = client.get(
            f"/api/v2/tasks/{task_id}/semantic-bindings",
            params={"dataset_id": normalized["dataset_id"]},
        )
        assert bindings.status_code == 200
        by_member = {item["semantic_member_id"]: item for item in bindings.json()["bindings"]}
        assert by_member["branch"]["status"] == "confirmed"
        assert by_member["amount"]["status"] == "confirmed"
        assert by_member["count"]["status"] == "confirmed"

        semantic_model = client.get("/api/v2/semantic-model")
        assert semantic_model.status_code == 200
        assert semantic_model.json()["source_path"] == "v1.yaml"
        assert "ExcelMind-main-v0" not in semantic_model.text


def test_stage1_binding_confirmation_errors_and_rejection():
    with TestClient(app) as client:
        task_id = _create_task(client)
        upload = client.post(
            f"/api/v2/tasks/{task_id}/uploads",
            files={"file": ("sample.csv", (FIXTURES / "sample.csv").read_bytes(), "text/csv")},
        ).json()
        dataset = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": upload["upload_id"]},
        ).json()
        bindings = client.get(
            f"/api/v2/tasks/{task_id}/semantic-bindings",
            params={"dataset_id": dataset["dataset_id"]},
        ).json()["bindings"]
        pending = next(item for item in bindings if item["semantic_member_id"] == "customer")
        base = (
            f"/api/v2/tasks/{task_id}/datasets/"
            f"{dataset['dataset_id']}/semantic-binding-decisions"
        )
        missing_field = client.post(
            base,
            json={"binding_id": pending["binding_id"], "confirm": True},
        )
        assert missing_field.status_code == 422
        invalid_field = client.post(
            base,
            json={
                "binding_id": pending["binding_id"],
                "physical_field_id": "not-a-field",
                "confirm": True,
            },
        )
        assert invalid_field.status_code == 404
        rejected = client.post(
            base,
            json={"binding_id": pending["binding_id"], "confirm": False},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"

        branch = next(item for item in bindings if item["semantic_member_id"] == "branch")
        confirmed = client.post(
            base,
            json={
                "binding_id": branch["binding_id"],
                "physical_field_id": branch["physical_field_id"],
                "confirm": True,
            },
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["source"] == "user"


def test_stage1_xlsx_sheet_selection_and_cross_task_isolation():
    first = BytesIO()
    with pd.ExcelWriter(first, engine="openpyxl") as writer:
        pd.DataFrame({"branch": ["A"], "amount": [10]}).to_excel(
            writer, sheet_name="一月", index=False
        )
        pd.DataFrame({"branch": ["B"], "amount": [20]}).to_excel(
            writer, sheet_name="二月", index=False
        )
    first.seek(0)
    with TestClient(app) as client:
        task_a = _create_task(client, "A")
        task_b = _create_task(client, "B")
        upload = client.post(
            f"/api/v2/tasks/{task_a}/uploads",
            files={
                "file": (
                    "book.xlsx",
                    first.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert upload.status_code == 200
        inspection = upload.json()
        assert {item["name"] for item in inspection["objects"]} == {"一月", "二月"}
        dataset = client.post(
            f"/api/v2/tasks/{task_a}/datasets",
            json={"upload_id": inspection["upload_id"], "object_name": "二月"},
        )
        assert dataset.status_code == 200
        assert dataset.json()["source_object"] == "二月"
        foreign = client.get(
            f"/api/v2/tasks/{task_b}/datasets/{dataset.json()['dataset_id']}"
        )
        assert foreign.status_code == 404


def test_stage1_task_limit_and_task_delete():
    with TestClient(app) as client:
        task_ids = [_create_task(client, str(index)) for index in range(5)]
        overflow = client.post("/api/v2/tasks", json={"name": "overflow"})
        assert overflow.status_code == 409
        assert overflow.json()["code"] == "TASK_LIMIT_REACHED"
        deleted = client.delete(f"/api/v2/tasks/{task_ids[0]}")
        assert deleted.status_code == 200
        replacement = client.post("/api/v2/tasks", json={"name": "replacement"})
        assert replacement.status_code == 200
        assert client.get(f"/api/v2/tasks/{task_ids[0]}").status_code == 404


def test_stage1_ambiguous_normalization_requires_confirmation():
    content = "日期,转化率\n01/02/2024,10%\n03/04/2024,20%\n".encode("utf-8")
    with TestClient(app) as client:
        task_id = _create_task(client)
        upload = client.post(
            f"/api/v2/tasks/{task_id}/uploads",
            files={"file": ("ambiguous.csv", content, "text/csv")},
        )
        dataset = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": upload.json()["upload_id"]},
        )
        assert dataset.status_code == 200
        blocked = dataset.json()
        assert blocked["status"] == "blocked"
        decision = blocked["pending_decisions"][0]
        confirmed = client.post(
            f"/api/v2/tasks/{task_id}/datasets/{blocked['dataset_id']}/normalization-decisions",
            json={"decision_id": decision["decision_id"], "choice": decision["options"][0]},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] in {"blocked", "ready"}


def test_stage1_ambiguous_csv_can_be_retried_with_delimiter():
    content = "name;value\nA,west;1\n".encode("utf-8")
    with TestClient(app) as client:
        task_id = _create_task(client)
        upload = client.post(
            f"/api/v2/tasks/{task_id}/uploads",
            files={"file": ("ambiguous.csv", content, "text/csv")},
        ).json()
        rejected = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": upload["upload_id"]},
        )
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "CSV_DELIMITER_CONFIRMATION_REQUIRED"
        imported = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": upload["upload_id"], "delimiter": ";"},
        )
        assert imported.status_code == 200


def test_stage1_dataset_decision_and_duplicate_import_errors():
    with TestClient(app) as client:
        task_id = _create_task(client)
        upload = client.post(
            f"/api/v2/tasks/{task_id}/uploads",
            files={"file": ("sample.csv", (FIXTURES / "sample.csv").read_bytes(), "text/csv")},
        ).json()
        dataset = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": upload["upload_id"]},
        ).json()
        duplicate = client.post(
            f"/api/v2/tasks/{task_id}/datasets",
            json={"upload_id": upload["upload_id"]},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "UPLOAD_ALREADY_IMPORTED"
        invalid_decision = client.post(
            f"/api/v2/tasks/{task_id}/datasets/{dataset['dataset_id']}/normalization-decisions",
            json={"decision_id": "missing", "choice": "dayfirst"},
        )
        assert invalid_decision.status_code == 404
