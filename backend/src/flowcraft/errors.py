"""Typed failure taxonomy — the core of FlowCraft's retry contract.

Every failure raised inside the engine carries a `FailureClass`. The class,
combined with each tool's declared idempotency support, deterministically
decides whether a retry is permitted:

- TRANSIENT  : may be retried if attempts remain AND (the tool declares itself
               idempotent OR the step declares an idempotency key). Safe to
               replay because duplicates are suppressed or harmless.
- RATE_LIMITED: may be retried with backoff; same idempotency rule applies.
- PERMANENT  : never retried (bad input, unsupported operation, auth failure).
- GUARDED    : the tool failed but its side effects are UNKNOWN and the tool
               does NOT declare idempotency support. The engine refuses to
               retry (at-most-once guarantee) instead of risking duplicate
               side effects. This is a hard stop, surfaced in run history.
- CONFIG     : workflow/provider/tool configuration error; never retried.
- CANCELLED  : operator cancellation; terminal.
"""

from __future__ import annotations

import enum


class FailureClass(str, enum.Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    PERMANENT = "permanent"
    GUARDED = "guarded"
    CONFIG = "config"
    CANCELLED = "cancelled"

    @property
    def retryable_by_class(self) -> bool:
        return self in (FailureClass.TRANSIENT, FailureClass.RATE_LIMITED)


RETRY_AFTER_HINT_DEFAULT = 1.0  # seconds; providers may override via hint


class FlowcraftError(Exception):
    """Base error. Always carries a machine-readable code and failure class."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "internal_error",
        failure_class: FailureClass = FailureClass.PERMANENT,
        retry_after: float | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.failure_class = failure_class
        self.retry_after = retry_after
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "class": self.failure_class.value,
            "message": self.message,
            "retry_after": self.retry_after,
            "details": self.details,
        }


class ValidationError(FlowcraftError):
    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message, code="validation_error",
                         failure_class=FailureClass.CONFIG, details=details)


class ProviderError(FlowcraftError):
    """Raised by provider adapters. Class must be set explicitly by adapter."""


class ToolExecutionError(FlowcraftError):
    """Raised by tools. Class must be set explicitly by the tool."""
