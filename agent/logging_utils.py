"""Structured JSON logging with secret redaction.

Every tool call produces one log line with the tool name, status, duration and the
request id, e.g.

    {"ts": "...", "level": "INFO", "event": "tool_call", "request_id": "c0ffee12",
     "tool": "mlflow_list_runs", "status": "ok", "duration_ms": 84, "args": {...}}

Argument values whose key looks like a credential, and anything that looks like a
token or password in free text, are replaced with "***".
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(pass(word)?|secret|token|api[_-]?key|authorization|credential|bearer)", re.I)
_SECRET_TEXT = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;\"']+"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
]


def redact(value: Any) -> Any:
    """Recursively hide secrets in dicts, lists and strings."""
    if isinstance(value, dict):
        return {k: ("***" if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        out = value
        for rx in _SECRET_TEXT:
            out = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "***", out)
        return out
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(redact(getattr(record, "fields", {})))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def setup_logging(log_file: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("mlops_agent")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    if verbose:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(JsonFormatter())
        console.setLevel(logging.INFO)
        logger.addHandler(console)
    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})
