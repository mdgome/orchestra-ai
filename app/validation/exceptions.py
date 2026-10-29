"""Exception hierarchy mapped to the single error envelope by error_handlers.py."""


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationFailed(AppError):
    status_code = 422
    code = "VALIDATION_FAILED"


class NotFound(AppError):
    status_code = 404
    code = "NOT_FOUND"


class Conflict(AppError):
    status_code = 409
    code = "CONFLICT"


class Gone(AppError):
    status_code = 410
    code = "GONE"


class InvalidStateTransition(Conflict):
    code = "INVALID_STATE_TRANSITION"


class LeaseLost(Conflict):
    code = "LEASE_LOST"


class VersionConflict(Conflict):
    code = "VERSION_CONFLICT"


class FailClosed(AppError):
    """A safety precondition (DB, lease, gate, session) failed; CLI must not run."""
    status_code = 409
    code = "FAIL_CLOSED"
