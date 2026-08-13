from __future__ import annotations

from fastapi.testclient import TestClient

from excel_agent.api import app


def test_v2_upload_rejects_legacy_excel_extensions():
    with TestClient(app) as client:
        task = client.post("/api/v2/tasks", json={"name": "security"}).json()
        for filename in ("legacy.xls", "macro.xlsm", "report.xlsx.exe", "..\\escape.csv"):
            response = client.post(
                f"/api/v2/tasks/{task['task_id']}/uploads",
                files={"file": (filename, b"not-a-file", "application/octet-stream")},
            )
            assert response.status_code in {400, 413}
            assert response.json()["code"] in {
                "UPLOAD_FORMAT_UNSUPPORTED",
                "UPLOAD_FILENAME_INVALID",
            }


def test_v2_upload_rejects_empty_csv():
    with TestClient(app) as client:
        task = client.post("/api/v2/tasks", json={"name": "empty"}).json()
        response = client.post(
            f"/api/v2/tasks/{task['task_id']}/uploads",
            files={"file": ("empty.csv", b"", "text/csv")},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "UPLOAD_EMPTY"
