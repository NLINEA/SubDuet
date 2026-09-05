from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

_PRIVATE_REQUEST: ContextVar[bool] = ContextVar("private_provider_request", default=False)
# HTTPX/HTTPCore diagnostics can include response headers, URLs, and reason phrases.
_HTTP_LOGGERS = (
    "httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)


class _ProviderDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_REQUEST.get()


_DIAGNOSTIC_FILTER = _ProviderDiagnosticFilter()


@contextmanager
def private_provider_diagnostics() -> Iterator[None]:
    """Suppress raw HTTP diagnostics only in the current provider-request context.

    Leave application logs and concurrent, unrelated requests alone. addFilter is idempotent;
    reset the context in finally so failures and nested calls cannot silence later requests.
    """
    for name in _HTTP_LOGGERS:
        logging.getLogger(name).addFilter(_DIAGNOSTIC_FILTER)
    token = _PRIVATE_REQUEST.set(True)
    try:
        yield
    finally:
        _PRIVATE_REQUEST.reset(token)


class ProviderResponseTooLargeError(ValueError):
    """Internal marker; never carry the provider's response content."""


def safe_provider_failure(error: Exception) -> str:
    """Return bounded diagnostics without stringifying any untrusted exception."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if 100 <= status <= 599:
            if 300 <= status <= 399:
                return f"provider redirect refused (HTTP {status})"
            return f"provider request failed (HTTP {status})"
        return "provider returned an invalid HTTP status"
    if isinstance(error, httpx.TimeoutException):
        return "provider request timed out"
    if isinstance(error, httpx.HTTPError):
        return "provider connection failed"
    if isinstance(error, OSError):
        return "could not read temporary audio"
    if isinstance(error, ProviderResponseTooLargeError):
        return "provider response exceeds the size limit"
    return "provider returned an invalid response"
