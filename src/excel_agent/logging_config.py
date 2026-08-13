"""阶段0使用的最小 JSON 日志配置。"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|authorization|token|secret|password|private[_-]?key|prompt)",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s]+")


def redact(value: Any) -> Any:
    """递归脱敏日志字段，不改变业务返回值。"""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _ABSOLUTE_PATH.sub("[PATH]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
            "request_id": getattr(record, "request_id", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "outcome": getattr(record, "outcome", None),
            "error_code": getattr(record, "error_code", None),
        }
        for key in ("task_id", "dataset_id", "analysis_id", "graph_node"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(redact(payload), ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("excel_agent")
    logger.setLevel(level)
    logger.propagate = False
    if not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
