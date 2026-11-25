"""Windows Job Object wrapper (B2 design 5.1): ctypes over kernel32, no new dependency.

`WindowsJob` implements the platform-neutral `TreeJob` interface of `process_control`: an anonymous, NON-inheritable
Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (no breakaway flags), assignment of a suspended process through a
public `OpenProcess` handle, resume of the primary thread (exactly one thread, previous suspend count 1), the
`TerminateJobObject` kill with the fixed exit code and the `ActiveProcesses` query that proves an empty tree.

The module is importable everywhere; every kernel32 call fails with `JobError` off Windows."""
import ctypes
import logging
import sys
from ctypes import wintypes

import psutil

from app.infra.process_control import JobError, TreeJob

log = logging.getLogger(__name__)

CREATE_SUSPENDED = 0x00000004

_ERROR_ACCESS_DENIED = 5
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_THREAD_SUSPEND_RESUME = 0x0002
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectBasicAccountingInformation = 1
_JobObjectExtendedLimitInformation = 9


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


_k32 = None


def _kernel32():
    """kernel32 with explicit prototypes (64-bit handles must never be truncated to a C int)."""
    global _k32
    if _k32 is not None:
        return _k32
    if sys.platform != "win32":
        raise JobError("JOB_CREATE_FAILED", "Windows Job Objects are not available on this platform")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    h = wintypes.HANDLE
    k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], h
    k.SetInformationJobObject.argtypes = [h, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [h, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes, k.AssignProcessToJobObject.restype = [h, h], wintypes.BOOL
    k.TerminateJobObject.argtypes, k.TerminateJobObject.restype = [h, wintypes.UINT], wintypes.BOOL
    k.OpenProcess.argtypes, k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], h
    k.OpenThread.argtypes, k.OpenThread.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], h
    k.ResumeThread.argtypes, k.ResumeThread.restype = [h], wintypes.DWORD
    k.CloseHandle.argtypes, k.CloseHandle.restype = [h], wintypes.BOOL
    _k32 = k
    return k


def _fail(code: str, what: str) -> JobError:
    err = ctypes.get_last_error()
    return JobError(code, f"{what} failed (winerror {err})", winerror=err, access_denied=err == _ERROR_ACCESS_DENIED)


class WindowsJob(TreeJob):
    """One Job Object. Not thread-safe by itself; the executor drives it from one coroutine."""

    def __init__(self) -> None:
        k = _kernel32()
        self._handle = k.CreateJobObjectW(None, None)       # NULL attributes: the handle is NOT inheritable
        if not self._handle:
            raise _fail("JOB_CREATE_FAILED", "CreateJobObjectW")
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE   # no BREAKAWAY_OK flags
        if not k.SetInformationJobObject(self._handle, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                         ctypes.sizeof(info)):
            error = _fail("JOB_CREATE_FAILED", "SetInformationJobObject(KILL_ON_JOB_CLOSE)")
            self.close()
            raise error

    @property
    def closed(self) -> bool:
        return self._handle is None

    def _live(self):
        if self._handle is None:
            raise JobError("JOB_HANDLE_LOST", "the Job handle is closed")
        return _kernel32(), self._handle

    def assign(self, pid: int) -> None:
        """Assigns the (suspended) process through a public OpenProcess handle."""
        k, job = self._live()
        process = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            raise _fail("JOB_ASSIGN_FAILED", "OpenProcess")
        try:
            if not k.AssignProcessToJobObject(job, process):
                raise _fail("JOB_ASSIGN_FAILED", "AssignProcessToJobObject")
        finally:
            k.CloseHandle(process)

    def resume(self, pid: int) -> None:
        resume_primary_thread(pid)

    def terminate(self, exit_code: int) -> None:
        k, job = self._live()
        if not k.TerminateJobObject(job, exit_code):
            raise _fail("JOB_TERMINATE_FAILED", "TerminateJobObject")

    def active_processes(self) -> int:
        k, job = self._live()
        info = _BasicAccounting()
        if not k.QueryInformationJobObject(job, _JobObjectBasicAccountingInformation, ctypes.byref(info),
                                           ctypes.sizeof(info), None):
            raise _fail("JOB_QUERY_FAILED", "QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def close(self) -> None:
        """Idempotent. With KILL_ON_JOB_CLOSE a still-populated Job is killed by this call."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        if not _kernel32().CloseHandle(handle):
            log.warning("the Job handle could not be closed (winerror %s)", ctypes.get_last_error())


def resume_primary_thread(pid: int) -> None:
    """Design 5.1 step 6: exactly one thread, `OpenThread(THREAD_SUSPEND_RESUME)`, `ResumeThread`, previous count == 1.
    NtResumeProcess and psutil's resume() are deliberately not used."""
    k = _kernel32()
    try:
        threads = psutil.Process(pid).threads()
    except psutil.Error as exc:
        raise JobError("RESUME_FAILED", f"threads of the suspended process unavailable: {exc}") from exc
    if len(threads) != 1:
        raise JobError("RESUME_FAILED", f"expected exactly one thread, found {len(threads)}")
    thread = k.OpenThread(_THREAD_SUSPEND_RESUME, False, threads[0].id)
    if not thread:
        raise _fail("RESUME_FAILED", "OpenThread")
    try:
        previous = k.ResumeThread(thread)
    finally:
        k.CloseHandle(thread)
    if previous != 1:       # 0xFFFFFFFF = failure, 0 = it was not suspended, > 1 = still suspended
        raise JobError("RESUME_FAILED", f"unexpected previous suspend count {previous}")


def handle_is_inheritable(handle: int) -> bool:
    """Test helper: whether the HANDLE_FLAG_INHERIT bit of a handle of this process is set."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetHandleInformation.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    if not k.GetHandleInformation(handle, ctypes.byref(flags)):
        raise _fail("JOB_QUERY_FAILED", "GetHandleInformation")
    return bool(flags.value & 0x1)
