"""Process identity and tree termination.

A process is identified by (pid, create_time); pid alone is never trusted because
PIDs are reused. Every failure mode is returned as a distinct, explicit result.
"""
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import psutil

from app.validation import policies as P

_CREATE_TIME_TOLERANCE = 1.0  # seconds; psutil create_time is float epoch


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float


class TerminationResult(StrEnum):
    TERMINATED = "TERMINATED"              # we killed it and verified it is gone
    ALREADY_EXITED = "ALREADY_EXITED"      # no such process (or pid reused by a different process)
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"  # alive but not the process we recorded -> not killed
    ACCESS_DENIED = "ACCESS_DENIED"
    STILL_ALIVE = "STILL_ALIVE"            # kill attempted, still running after grace period
    UNKNOWN = "UNKNOWN"                    # could not determine


# Results after which it is *proven* that the recorded process is not running.
CONFIRMED_GONE = frozenset({TerminationResult.TERMINATED, TerminationResult.ALREADY_EXITED})


def capture_identity(pid: int) -> ProcessIdentity | None:
    try:
        return ProcessIdentity(pid, psutil.Process(pid).create_time())
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied:
        return None


def check_alive(identity: ProcessIdentity) -> TerminationResult:
    """ALREADY_EXITED = gone, IDENTITY_MISMATCH = pid reused by another process,
    STILL_ALIVE = the recorded process runs, ACCESS_DENIED/UNKNOWN = cannot tell."""
    try:
        proc = psutil.Process(identity.pid)
        created = proc.create_time()
    except psutil.NoSuchProcess:
        return TerminationResult.ALREADY_EXITED
    except psutil.AccessDenied:
        return TerminationResult.ACCESS_DENIED
    except psutil.Error:
        return TerminationResult.UNKNOWN
    if abs(created - identity.create_time) > _CREATE_TIME_TOLERANCE:
        return TerminationResult.IDENTITY_MISMATCH
    try:
        if proc.status() == psutil.STATUS_ZOMBIE:
            return TerminationResult.ALREADY_EXITED
    except psutil.Error:
        pass
    return TerminationResult.STILL_ALIVE


def terminate_tree(identity: ProcessIdentity, grace_seconds: float = 5.0) -> TerminationResult:
    """Terminate the recorded process and its descendants, but only if identity still matches."""
    state = check_alive(identity)
    if state is not TerminationResult.STILL_ALIVE:
        # ALREADY_EXITED -> nothing to do; IDENTITY_MISMATCH/ACCESS_DENIED/UNKNOWN -> do NOT kill
        return state
    try:
        root = psutil.Process(identity.pid)
        victims = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        return TerminationResult.ALREADY_EXITED
    except psutil.AccessDenied:
        return TerminationResult.ACCESS_DENIED
    except psutil.Error:
        return TerminationResult.UNKNOWN

    denied = False
    for proc in victims:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied = True
    _, alive = psutil.wait_procs(victims, timeout=grace_seconds)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            denied = True
    _, alive = psutil.wait_procs(alive, timeout=grace_seconds)
    if alive:
        return TerminationResult.ACCESS_DENIED if denied else TerminationResult.STILL_ALIVE
    return TerminationResult.TERMINATED


# --- tree ownership (B2 design 5): the Job protocol, the platform decision and the startup self-test -------------------
class JobError(Exception):
    """A Job primitive failed. `code` is a reason code (JOB_CREATE_FAILED, JOB_ASSIGN_FAILED, RESUME_FAILED, ...), kept in
    `CommandResult.reason`, never in `termination`."""

    def __init__(self, code: str, message: str = "", *, winerror: int | None = None, access_denied: bool = False):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.winerror, self.access_denied = code, winerror, access_denied


class IdentityFenced(Exception):
    """Raised by a tracked `on_started` callback whose fenced identity update lost the fence (lost lease, row already
    closed): the executor kills the suspended process and reports SPAWN_FAILED(IDENTITY_FENCED)."""


class TreeJob(ABC):
    """What the executor needs from a process-tree owner (a Windows Job Object; fakes in tests). An abstract base, not a
    typing.Protocol: the architecture rules allow exactly two Protocols (providers, session store)."""

    @abstractmethod
    def assign(self, pid: int) -> None: ...          # the process is still suspended; JobError("JOB_ASSIGN_FAILED")

    @abstractmethod
    def resume(self, pid: int) -> None: ...          # JobError("RESUME_FAILED")

    @abstractmethod
    def terminate(self, exit_code: int) -> None: ...  # kills every member of the tree at once

    @abstractmethod
    def active_processes(self) -> int: ...           # JobError when it cannot be determined

    @abstractmethod
    def close(self) -> None: ...                     # idempotent


class TrackingState(StrEnum):
    """Design 6.2: the process-tracking capability states."""
    SUPPORTED = "SUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    UNKNOWN = "UNKNOWN"


def platform_supports_tracking() -> bool:
    """Tracked commands exist on Windows only (5.2): POSIX degrades to untracked execution."""
    return sys.platform == "win32"


def default_job_factory() -> Callable[[], TreeJob] | None:
    if not platform_supports_tracking():
        return None
    from app.infra.windows_job import WindowsJob
    return WindowsJob


@dataclass(frozen=True)
class SelfTestResult:
    state: TrackingState
    reason_code: str | None = None       # PROCESS_TRACKING_UNAVAILABLE / PROCESS_TRACKING_UNSUPPORTED_PLATFORM
    failure: str | None = None           # the step and the Job reason code that failed (diagnostic text, no secrets)


def run_tracking_self_test(job_factory: Callable[[], TreeJob] | None = None) -> SelfTestResult:
    """Design 5.1 step 7 (blocking; the Runner calls it through a thread): the whole protocol once with a trivial child,
    spawn suspended -> assign -> resume by thread -> terminate -> ActiveProcesses == 0. Covers the environment, not each
    future command. The capability upsert flow of 6.2 is NOT here."""
    if not platform_supports_tracking():
        return SelfTestResult(TrackingState.UNSUPPORTED_PLATFORM, "PROCESS_TRACKING_UNSUPPORTED_PLATFORM")
    if job_factory is None:
        job_factory = default_job_factory()
    from app.infra.windows_job import CREATE_SUSPENDED
    unavailable = "PROCESS_TRACKING_UNAVAILABLE"
    job: TreeJob | None = None
    child: subprocess.Popen | None = None
    step = "create"
    try:
        job = job_factory()
        step = "spawn"
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED)
        step = "assign"
        job.assign(child.pid)
        step = "identity"
        if capture_identity(child.pid) is None:
            return SelfTestResult(TrackingState.UNAVAILABLE, unavailable, "identity: unavailable")
        step = "resume"
        job.resume(child.pid)
        step = "populated"
        if job.active_processes() < 1:
            return SelfTestResult(TrackingState.UNAVAILABLE, unavailable, "populated: the Job is empty after assignment")
        step = "terminate"
        job.terminate(P.JOB_TERMINATE_EXIT_CODE)
        step = "prove"
        deadline = time.monotonic() + P.PROCESS_TERMINATE_GRACE_SECONDS
        while job.active_processes() != 0:
            if time.monotonic() >= deadline:
                return SelfTestResult(TrackingState.UNAVAILABLE, unavailable, "prove: the Job is not empty after the kill")
            time.sleep(P.JOB_POLL_INTERVAL_SECONDS)
        return SelfTestResult(TrackingState.SUPPORTED)
    except JobError as exc:
        return SelfTestResult(TrackingState.UNAVAILABLE, unavailable, f"{step}: {exc.code}")
    except Exception as exc:       # noqa: BLE001 - any failure of the environment means "tracking is not available"
        return SelfTestResult(TrackingState.UNAVAILABLE, unavailable, f"{step}: {type(exc).__name__}")
    finally:
        if child is not None and child.poll() is None:
            child.kill()
        if child is not None:
            try:
                child.wait(timeout=P.PROCESS_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if job is not None:
            try:
                job.close()
            except Exception:      # noqa: BLE001
                pass
