from __future__ import annotations

from io import BytesIO

import pandas as pd
from fastapi.testclient import TestClient

from excel_agent.api import app
from excel_agent.config import get_config
from excel_agent.excel_loader import reset_loader


def test_health_and_request_id_are_safe():
    with TestClient(app) as client:
        response = client.get("/api/v2/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "api_key" not in response.text.lower()
    assert "file_path" not in response.text.lower()
    assert response.headers["x-request-id"]


def test_load_endpoint_is_removed_and_uses_error_envelope():
    with TestClient(app) as client:
        response = client.post("/load", json={"file_path": "C:\\secret.xlsx"})
    assert response.status_code == 404
    payload = response.json()
    assert set(payload) == {"code", "message", "details", "request_id"}
    assert "secret.xlsx" not in response.text
    assert response.headers["x-request-id"] == payload["request_id"]


def test_cors_rejects_unconfigured_origin():
    with TestClient(app) as client:
        response = client.options(
            "/api/v2/health",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_validation_and_method_errors_use_same_envelope():
    with TestClient(app) as client:
        validation = client.post("/upload")
        method = client.post("/api/v2/health")
    assert validation.status_code == 422
    assert validation.json()["code"] == "VALIDATION_ERROR"
    assert method.status_code == 405
    assert method.json()["code"] == "METHOD_NOT_ALLOWED"


def test_upload_rejects_path_traversal_and_fake_workbook():
    with TestClient(app) as client:
        traversal = client.post(
            "/upload",
            files={"file": ("..\\evil.xlsx", b"bad", "application/octet-stream")},
        )
        fake = client.post(
            "/upload",
            files={"file": ("report.xlsx", b"not a zip", "application/octet-stream")},
        )
    assert traversal.status_code == 400
    assert traversal.json()["code"] == "UPLOAD_FILENAME_INVALID"
    assert fake.status_code == 400
    assert fake.json()["code"] == "UPLOAD_SIGNATURE_INVALID"


def test_upload_stream_loads_xlsx_without_returning_server_path():
    stream = BytesIO()
    pd.DataFrame({"branch": ["A"], "amount": [10]}).to_excel(stream, index=False)
    stream.seek(0)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/upload",
                files={
                    "file": (
                        "report.xlsx",
                        stream.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )
        assert response.status_code == 200
        assert response.json()["structure"]["file_path"] == "[managed-upload]"
    finally:
        reset_loader()


def test_missing_model_key_is_safe_and_deterministic_path_stays_testable():
    stream = BytesIO()
    pd.DataFrame({"branch": ["A"], "amount": [10]}).to_excel(stream, index=False)
    stream.seek(0)
    provider = get_config().model.get_active_provider()
    original_key = provider.api_key
    provider.api_key = ""
    try:
        with TestClient(app) as client:
            upload = client.post(
                "/upload",
                files={"file": ("report.xlsx", stream.getvalue(), "application/octet-stream")},
            )
            assert upload.status_code == 200
            response = client.post("/chat", json={"message": "统计金额"})
        assert response.status_code == 503
        assert response.json()["code"] == "MODEL_CREDENTIAL_MISSING"
        assert "authorization" not in response.text.lower()
        assert "secret" not in response.text.lower()
    finally:
        provider.api_key = original_key
        reset_loader()
