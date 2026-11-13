"""Exceptions tools raise. The orchestrator maps each one to a ToolResult status."""


class ToolError(Exception):
    """Generic tool failure (status ``error``)."""


class ServiceUnavailable(ToolError):
    """The backing service cannot be reached (status ``unavailable``)."""


class InvalidRequest(ToolError):
    """The arguments are wrong, e.g. an unknown run id or a bad value (status ``invalid_request``)."""


class PolicyDenied(ToolError):
    """The operation is not allowed by the safety policy (status ``denied``)."""
