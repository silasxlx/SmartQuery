from pathlib import Path

from excel_agent.errors import error_payload


def test_error_payload_does_not_accept_hidden_sensitive_data():
    payload = error_payload(
        code="UPLOAD_FAILED",
        message="上传失败",
        details={"field": "filename"},
        request_id="request-1",
    )
    assert set(payload) == {"code", "message", "details", "request_id"}
    assert str(Path("secret.xlsx")) not in str(payload)


def test_error_payload_redacts_sensitive_detail_values():
    payload = error_payload(
        code="MODEL_ERROR",
        message="调用失败",
        details={"Authorization": "Bearer secret", "path": r"C:\\private\\file.csv"},
        request_id="request-1",
    )
    assert payload["details"]["Authorization"] == "[REDACTED]"
    assert "C:\\private" not in payload["details"]["path"]
