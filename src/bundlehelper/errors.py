from __future__ import annotations


class BundleHelperError(Exception):
    """Base error shown to the user."""


class ValidationError(BundleHelperError):
    """Configuration or path validation failed."""


class PreflightError(BundleHelperError):
    """An operation was blocked before it could modify repository state."""


class CommandError(BundleHelperError):
    def __init__(self, message: str, *, command: str = "", returncode: int | None = None):
        super().__init__(message)
        self.command = command
        self.returncode = returncode


class CancelledError(BundleHelperError):
    """The user requested cancellation."""