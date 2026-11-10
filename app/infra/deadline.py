"""A monotonic deadline that is handed to the measuring code itself, not just wrapped around it.

`asyncio.timeout` around `to_thread` only stops WAITING: the worker thread keeps running. A `Deadline` is passed down so the
work checks it between entries and between hash chunks and stops by itself (a COOPERATIVE limit), and so a git subprocess
gets `min(its own timeout, remaining)` as its timeout and is killed by `subprocess.run` when that expires.

What it cannot do: a blocking read, directory listing or stat that stalls inside the operating system cannot be
interrupted from Python, so the worker may outlive the deadline in that case. Callers bound how many such workers can exist.
"""
import time
from collections.abc import Callable


class DeadlineExceeded(Exception):
    """The measurement ran out of time; its partial result must not be used."""


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return self._end - self._clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise DeadlineExceeded("the measurement deadline passed")
