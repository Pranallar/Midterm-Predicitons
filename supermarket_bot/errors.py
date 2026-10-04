"""Exceptions raised by the API client.

The API returns errors as ``{"error": {"code", "message", "details"?}}``. Branch on
``code`` (stable), never on ``message`` (free-form).
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, Iterable, Optional, Set

# Codes whose fix is on the account/key, not in the request. Retrying never helps.
AUTH_CODES = {
    "MISSING_API_KEY",
    "INVALID_API_KEY",
    "API_KEY_REVOKED",
    "API_KEY_EXPIRED",
    "INSUFFICIENT_SCOPES",
    "ACCOUNT_BANNED",
    "RESIDENCE_UPDATE_REQUIRED",
    "TERMS_NOT_ACKNOWLEDGED",
    "ADMIN_REQUIRED",
}

HINTS = {
    "MISSING_API_KEY": "No API key was sent. Set SUPERMARKET_API_KEY.",
    "INVALID_API_KEY": "The key is not recognised (typo, or the owning account was deleted).",
    "API_KEY_REVOKED": "This key was revoked. Create a new one under My Profile → API Keys.",
    "API_KEY_EXPIRED": "This key has expired. Create a new one under My Profile → API Keys.",
    "INSUFFICIENT_SCOPES": "The key is missing a scope. Market data needs the `read` scope.",
    "ACCOUNT_BANNED": "The account that owns this key is banned.",
    "RESIDENCE_UPDATE_REQUIRED": "Update your Predictions Cup state/territory on the site.",
    "TERMS_NOT_ACKNOWLEDGED": "Accept the current Terms & Conditions on the site.",
    "RATE_LIMITED": "Per-account rate limit hit; the bot will back off and retry.",
}


class SuperMarketError(Exception):
    """Base class for every error this package raises."""


class NetworkError(SuperMarketError):
    """The request never got an HTTP response (DNS, TLS, timeout, connection reset)."""


class InvalidPathParam(SuperMarketError, ValueError):
    """A path parameter (ID or slug) is empty or a dot segment that would change the URL."""


class RequestCancelled(SuperMarketError):
    """The client was cancelled (shutdown) while a request was waiting or retrying."""


class ApiError(SuperMarketError):
    """The API answered with a non-2xx status."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        retry_after: Optional[float] = None,
        method: str = "",
        path: str = "",
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.retry_after = retry_after
        self.method = method
        self.path = path
        super().__init__(str(self))

    @property
    def hint(self) -> Optional[str]:
        if self.code == "FORBIDDEN" and "Confirm the email" in self.message:
            return "Confirm the email address on the account that owns this key."
        return HINTS.get(self.code)

    def __str__(self) -> str:
        where = f" ({self.method} {self.path})" if self.path else ""
        text = f"HTTP {self.status} {self.code}: {self.message}{where}"
        if self.hint:
            text += f" — {self.hint}"
        return text


# --------------------------------------------------------------------------- secret masking
# The API key must never reach a log line, a status message or a JSON response. An upstream
# error (a proxy, a misconfigured gateway) can echo the request's ``Authorization`` header, so
# error text is masked where it is created and again on its way into the log.

MIN_SECRET_LEN = 8  # shorter strings (the demo's "demo" key) would mask ordinary words
MASK = "***"
_SECRETS: Set[str] = set()
_SECRETS_LOCK = threading.Lock()
# "Bearer <token>" in any text: the token is a credential whatever it is.
_BEARER_RE = re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=-]{4,}")


def register_secret(value: Any) -> None:
    """Mask ``value`` in every later :func:`redact` call (and so in every package log line)."""
    if isinstance(value, str) and len(value.strip()) >= MIN_SECRET_LEN:
        with _SECRETS_LOCK:
            _SECRETS.add(value.strip())


def redact(text: Any, secrets: Iterable[Any] = ()) -> str:
    """``text`` with every registered secret, every one of ``secrets`` and any ``Bearer <token>``
    replaced by ``***``. Mask before truncating: a key cut at the boundary would leak its start."""
    out = text if isinstance(text, str) else str(text)
    with _SECRETS_LOCK:
        known = set(_SECRETS)
    known.update(s.strip() for s in secrets if isinstance(s, str) and len(s.strip()) >= MIN_SECRET_LEN)
    for secret in sorted(known, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, MASK)
    return _BEARER_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, out)


def redact_json(value: Any, secrets: Iterable[Any] = ()) -> Any:
    """:func:`redact` applied to every string in a JSON-shaped value (dict keys included)."""
    secrets = list(secrets)
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {redact_json(k, secrets): redact_json(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_json(v, secrets) for v in value]
    return value


class RedactingFilter(logging.Filter):
    """A logging filter that masks secrets in the formatted message (see :func:`redact`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string: let logging report it as usual
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


def install_log_redaction(*loggers: logging.Logger) -> None:
    """Add a :class:`RedactingFilter` to the package logger, the given loggers and every handler
    of the root logger (records of other libraries pass through those). Idempotent."""
    targets = [logging.getLogger("supermarket_bot"), *loggers, *logging.getLogger().handlers]
    for target in targets:
        if not any(isinstance(f, RedactingFilter) for f in getattr(target, "filters", [])):
            target.addFilter(RedactingFilter())


install_log_redaction()  # every package log line is masked, however logging is configured
