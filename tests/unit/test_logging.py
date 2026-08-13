import asyncio
import json
import logging

from starlette.requests import Request

from excel_agent.errors import safe_public_message, unexpected_error_handler
from excel_agent.logging_config import JsonFormatter, redact


def test_redact_hides_secrets_and_paths():
    value = redact({"api_key": "secret", "path": r"C:\private\file.xlsx", "nested": ["ok"]})
    assert value["api_key"] == "[REDACTED]"
    assert "private" not in value["path"]
    assert value["nested"] == ["ok"]


def test_json_log_has_stable_safe_fields():
    record = logging.LogRecord("excel_agent", logging.INFO, __file__, 1, "ignored", (), None)
    record.event = "upload_completed"
    record.request_id = "request-1"
    record.api_key = "secret"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "upload_completed"
    assert payload["request_id"] == "request-1"
    assert "api_key" not in payload


def test_public_error_message_redacts_paths_and_credentials():
    message = safe_public_message(r"failed C:\private\file.xlsx api_key=secret-value")
    assert "private" not in message
    assert "secret-value" not in message


def test_unexpected_error_handler_is_safe():
    request = Request({"type": "http", "method": "GET", "path": "/"})
    request.state.request_id = "request-1"
    response = asyncio.run(unexpected_error_handler(request, RuntimeError("secret path")))
    assert response.status_code == 500
    assert b"secret path" not in response.body
