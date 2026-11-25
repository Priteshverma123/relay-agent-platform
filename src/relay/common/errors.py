"""Domain errors → RFC 9457 problem responses."""

from __future__ import annotations


class RelayError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(RelayError):
    status_code, code = 404, "not_found"


class Conflict(RelayError):
    status_code, code = 409, "conflict"


class InvalidInput(RelayError):
    status_code, code = 422, "invalid_input"


class Unauthorized(RelayError):
    status_code, code = 401, "unauthorized"


class Forbidden(RelayError):
    status_code, code = 403, "forbidden"


class PolicyDenied(RelayError):
    """Raised by the tool router: the policy engine refused a tool call."""

    status_code, code = 403, "policy_denied"


class BudgetExceeded(RelayError):
    status_code, code = 402, "budget_exceeded"


class UpstreamError(RelayError):
    status_code, code = 502, "upstream_error"


class SandboxError(RelayError):
    status_code, code = 500, "sandbox_error"
