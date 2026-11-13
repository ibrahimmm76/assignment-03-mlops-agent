"""Structured types shared by the tools and the orchestrator.

Every tool returns plain JSON-serialisable data; the orchestrator wraps it in a
``ToolResult`` so the language model always sees the same envelope:

    {"tool": "...", "status": "ok|error|unavailable|timeout|invalid_request|denied",
     "data": {...} | null, "error": "..." | null, "duration_ms": 12, "request_id": "..."}
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["ok", "error", "unavailable", "timeout", "invalid_request", "denied"]


@dataclass
class ToolSpec:
    name: str
    module: str  # dvc | mlflow | airflow | kubernetes
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    mutating: bool = False  # changes state: needs policy approval + explicit user confirmation
    timeout_s: float | None = None  # overrides the default tool timeout

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class ToolResult:
    tool: str
    status: Status
    data: Any = None
    error: str | None = None
    duration_ms: int = 0
    request_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def for_model(self, limit: int = 12_000) -> str:
        """JSON sent back to the language model (size-limited)."""
        payload = {"tool": self.tool, "status": self.status, "data": self.data, "error": self.error}
        text = json.dumps(payload, default=str)
        if len(text) > limit:
            text = text[:limit] + f'... [truncated {len(text) - limit} chars]"'
        return text


@dataclass
class AgentResponse:
    request_id: str
    answer: str
    tool_results: list[ToolResult]
    unsupported_values: list[str] = field(default_factory=list)
    steps: int = 0


def obj(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or []}


def string(description: str, enum: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string", "description": description}
    if enum:
        out["enum"] = enum
    return out


def integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def array(description: str, item_type: str = "string") -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": item_type}}
