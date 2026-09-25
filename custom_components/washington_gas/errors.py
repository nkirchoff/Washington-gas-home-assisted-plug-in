"""Errors raised while signing in to Washington Gas or reading data."""

from __future__ import annotations

# Sign-in steps, used to say where a login stopped.
STEP_PORTAL_LOGIN = "washingtongas_login"
STEP_HANDOFF = "handoff"
STEP_OPOWER_API = "opower_api"
STEP_OPOWER_DIRECT = "opower_direct"

STEP_NAMES = {
    STEP_PORTAL_LOGIN: "My Washington Gas sign-in",
    STEP_HANDOFF: "Hand-off to Opower",
    STEP_OPOWER_API: "Opower API",
    STEP_OPOWER_DIRECT: "Direct Opower sign-in",
}


class WashingtonGasError(Exception):
    """Base error for this client."""


class InvalidAuth(WashingtonGasError):
    """The username or password was rejected."""


class CannotConnect(WashingtonGasError):
    """The site could not be reached or answered in a way we don't understand."""


class NoAccounts(WashingtonGasError):
    """The login worked but has no gas accounts linked to it."""


class ApiError(WashingtonGasError):
    """A data request failed."""

    def __init__(self, message: str, url: str, status: int | None = None) -> None:
        """Initialize the error."""
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


class LoginStepError(CannotConnect):
    """A sign-in step failed. Says which step, where, and with what HTTP status."""

    def __init__(self, step: str, message: str, host: str | None = None, status: int | None = None) -> None:
        """Initialize the error."""
        super().__init__(message)
        self.step = step
        self.host = host
        self.status = status


class PortalUnavailable(LoginStepError):
    """my.washingtongas.com couldn't be reached at all (network error, 404 or 5xx)."""


class MfaRequired(LoginStepError):
    """My Washington Gas asked for a verification code, which can't be entered headlessly."""


class CaptchaRequired(LoginStepError):
    """My Washington Gas shows a CAPTCHA, which this integration won't try to get around."""
