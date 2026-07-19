"""Catalog policies — minimal surface (read-mostly service).

Catalog has no write policies in v0.7; the only error here is the absence
of an active price row, which the renewal-time stack must never hit
silently. The middleware surfaces this as 422 POLICY_VIOLATION matching
the shape used by every other BSS service.
"""

from __future__ import annotations


class PolicyViolation(Exception):
    """Structured policy error — surfaced over HTTP as 422 POLICY_VIOLATION."""

    def __init__(self, *, rule: str, message: str, context: dict | None = None):
        self.rule = rule
        self.message = message
        self.context = context or {}
        super().__init__(message)
