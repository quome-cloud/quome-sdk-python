"""Typed exception hierarchy for the quome SDK.

None of these exceptions ever carry the API key — only HTTP status codes and
server-provided detail strings, both of which are safe to log or print.
"""

from __future__ import annotations


class QuomeError(Exception):
    """Base class for all quome SDK errors."""


class QuomeAPIError(QuomeError):
    """An error response from the Quome API.

    Carries both the HTTP status code and the server-provided detail message.
    ``status_code`` is ``None`` for errors raised locally by the SDK before a
    request was ever sent (e.g. a missing API key).
    """

    def __init__(self, status_code: int | None, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        message = detail if status_code is None else f"[{status_code}] {detail}"
        super().__init__(message)


class AuthenticationError(QuomeAPIError):
    """Raised for 401/403 responses, or locally when no API key is configured."""

    def __init__(self, detail: str, status_code: int | None = None) -> None:
        super().__init__(status_code, detail)


class NotFoundError(QuomeAPIError):
    """Raised for 404 responses."""


class QuotaExceededError(QuomeAPIError):
    """Raised for 429 responses. Carries ``retry_after`` (seconds) when known."""

    def __init__(
        self, status_code: int | None, detail: str, retry_after: float | None = None
    ) -> None:
        super().__init__(status_code, detail)
        self.retry_after = retry_after


class SandboxNotRunningError(QuomeAPIError):
    """Raised for 409 responses whose detail names a sandbox not-running state."""


class SandboxProvisioningError(QuomeError):
    """Raised when a sandbox lands in a terminal failure state while a caller
    is waiting for it to reach a desired status.

    This is a local SDK-side error (not an HTTP error) — it fires the moment
    ``wait_for_status`` sees ``status`` in the terminal-fail set, without
    waiting out the rest of the timeout. Distinct from ``TimeoutError``,
    which means the desired status was never observed within the deadline.
    """

    def __init__(
        self, message: str, sandbox_id: str | None = None, status: str | None = None
    ) -> None:
        self.sandbox_id = sandbox_id
        self.status = status
        super().__init__(message)
