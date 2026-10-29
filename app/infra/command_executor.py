"""Runs external commands via asyncio.create_subprocess_exec (never a shell).

Responsibilities: argument-list only, env allowlist, cwd validation, timeout / idle detection,
cooperative cancel, process-tree cleanup, bounded output capture, explicit outcome.
The executor never raises on a failing command: failures are returned as CommandResult.outcome.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.infra.process_control import ProcessIdentity, capture_identity, terminate_tree
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import CliOutcome
from app.validation import policies as P
from app.validation.exceptions import FailClosed


@dataclass
class CommandResult:
    argv: list[str]
    outcome: CliOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    identity: ProcessIdentity | None = None
    termination: str | None = None
    truncated: bool = False


@dataclass
class CommandSpec:
    argv: list[str]
    cwd: Path
    timeout_seconds: int = P.DEFAULT_CLI_TIMEOUT_SECONDS
    idle_seconds: int | None = P.CLI_IDLE_SECONDS
    stdin_text: str | None = None          # None -> stdin is closed (non-interactive)
    extra_env: dict[str, str] = field(default_factory=dict)


# Hook called right after the process is spawned, so callers can persist (pid, create_time).
OnStarted = Callable[[ProcessIdentity | None], Awaitable[None]]
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    allowed = {k.upper() for k in P.ENV_ALLOWLIST}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(extra or {})
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def resolve_executable(name: str) -> str:
    """Windows: `claude`/`codex` are .cmd/.exe shims; create_subprocess_exec needs the real path."""
    found = shutil.which(name)
    if not found:
        raise FailClosed("executable not found", {"executable": name})
    return found


class CommandExecutor:
    def __init__(self, guard: WorkspaceGuard):
        self._guard = guard
        self._cancel_events: dict[str, asyncio.Event] = {}

    def cancel(self, run_key: str) -> None:
        """Cancels the run with this key, or every run whose key is `<run_key>:...` (all steps of a lease)."""
        for key, event in list(self._cancel_events.items()):
            if key == run_key or key.startswith(run_key + ":"):
                event.set()

    async def run(self, spec: CommandSpec, *, run_key: str | None = None,
                  on_started: OnStarted | None = None) -> CommandResult:
        if not spec.argv or not all(isinstance(a, str) for a in spec.argv):
            raise FailClosed("argv must be a non-empty list of strings")
        cwd = self._guard.resolve_inside(spec.cwd, must_exist=True)
        argv = [resolve_executable(spec.argv[0]), *spec.argv[1:]]
        cancel_event = asyncio.Event()
        if run_key:
            self._cancel_events[run_key] = cancel_event

        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=build_env(spec.extra_env),
                stdin=asyncio.subprocess.PIPE if spec.stdin_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
        except OSError as exc:
            self._cancel_events.pop(run_key or "", None)
            return CommandResult(argv, CliOutcome.ERROR, None, "", f"spawn failed: {exc}", time.monotonic() - started)

        identity = capture_identity(proc.pid)
        if on_started:
            await on_started(identity)

        out = bytearray()
        err = bytearray()
        last_output = time.monotonic()
        truncated = False

        async def pump(stream, buf: bytearray):
            nonlocal last_output, truncated
            while chunk := await stream.read(65536):
                last_output = time.monotonic()
                if len(buf) < MAX_CAPTURE_BYTES:
                    buf.extend(chunk)
                else:
                    truncated = True

        pumps = [asyncio.create_task(pump(proc.stdout, out)), asyncio.create_task(pump(proc.stderr, err))]
        if spec.stdin_text is not None:
            try:
                proc.stdin.write(spec.stdin_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        outcome: CliOutcome | None = None
        waiter = asyncio.create_task(proc.wait())
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        try:
            while outcome is None:
                done, _ = await asyncio.wait({waiter, cancel_waiter}, timeout=0.5,
                                             return_when=asyncio.FIRST_COMPLETED)
                now = time.monotonic()
                if waiter in done:
                    break
                if cancel_waiter in done:
                    outcome = CliOutcome.CANCELLED
                elif now - started > spec.timeout_seconds:
                    outcome = CliOutcome.TIMEOUT
                elif spec.idle_seconds and now - last_output > spec.idle_seconds:
                    outcome = CliOutcome.IDLE
        finally:
            cancel_waiter.cancel()

        termination = None
        if outcome is not None:
            termination = await self._terminate(proc, identity)
        await asyncio.gather(*pumps, return_exceptions=True)
        exit_code = proc.returncode
        if outcome is None:
            outcome = CliOutcome.SUCCESS if exit_code == 0 else CliOutcome.ERROR
        self._cancel_events.pop(run_key or "", None)
        return CommandResult(
            argv=argv, outcome=outcome, exit_code=exit_code,
            stdout=out.decode("utf-8", errors="replace"), stderr=err.decode("utf-8", errors="replace"),
            duration_seconds=time.monotonic() - started, identity=identity,
            termination=termination, truncated=truncated)

    async def _terminate(self, proc, identity: ProcessIdentity | None) -> str:
        if identity is None:
            # Could not capture an identity: fall back to the Popen handle we own.
            proc.kill()
            await proc.wait()
            return "KILLED_BY_HANDLE"
        result = await asyncio.to_thread(terminate_tree, identity, P.PROCESS_TERMINATE_GRACE_SECONDS)
        try:
            await asyncio.wait_for(proc.wait(), timeout=P.PROCESS_TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            pass
        return str(result)
