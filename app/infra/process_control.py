"""Process identity and tree termination.

A process is identified by (pid, create_time); pid alone is never trusted because
PIDs are reused. Every failure mode is returned as a distinct, explicit result.
"""
from dataclasses import dataclass
from enum import StrEnum

import psutil

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
