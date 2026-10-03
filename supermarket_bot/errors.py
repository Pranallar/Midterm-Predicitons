"""Exceptions raised by the API client.

The API returns errors as ``{"error": {"code", "message", "details"?}}``. Branch on
``code`` (stable), never on ``message`` (free-form).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

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
