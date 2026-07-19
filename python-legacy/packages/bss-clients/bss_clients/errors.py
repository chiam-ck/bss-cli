"""Typed errors for bss-clients.

Callers branch on exception type, not JSON parsing.
"""


class ClientError(Exception):
    """Base for all bss-clients errors."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class NotFound(ClientError):
    """HTTP 404 from downstream service."""

    def __init__(self, detail: str = "Not found"):
        super().__init__(404, detail)


class PolicyViolationFromServer(ClientError):
    """HTTP 422 with code=POLICY_VIOLATION from downstream service."""

    def __init__(self, rule: str, message: str, context: dict | None = None):
        self.rule = rule
        self.context = context or {}
        super().__init__(422, message)


class ServerError(ClientError):
    """HTTP 5xx from downstream service."""

    def __init__(self, status_code: int = 500, detail: str = "Server error"):
        super().__init__(status_code, detail)


class Timeout(ClientError):
    """Request timed out."""

    def __init__(self, detail: str = "Request timed out"):
        super().__init__(504, detail)
