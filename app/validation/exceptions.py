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


class ResponseSizeUnsatisfiable(AppError):
    """Even the bounded fallback response exceeds its size limit: refuse to send a response that breaks the contract."""
    status_code = 500
    code = "RESPONSE_SIZE_UNSATISFIABLE"


class ServiceNotConfigured(AppError):
    """The container was built without an optional service this endpoint needs."""
    status_code = 503
    code = "SERVICE_NOT_CONFIGURED"


class ReportsNotConfigured(ServiceNotConfigured):
    """The container was built without the artifact / report views (`Container.reports` is None)."""
    code = "REPORTS_NOT_CONFIGURED"


class MeasurementBusy(AppError):
    """Another workspace measurement of this project (or too many overall) is still running: nothing new is started."""
    status_code = 503
    code = "CHANGES_MEASUREMENT_BUSY"


class IdempotencyKeyReused(Conflict):
    """The same idempotency key was sent with a request that means something different (another project policy)."""
    code = "IDEMPOTENCY_KEY_REUSED"


class ActiveTestRunExists(Conflict):
    """A project runs at most one test run at a time: the id of the active one is in `details`."""
    code = "TEST_RUN_ACTIVE"
