"""Scripted provider for tests: no external CLI. Records every call and can touch files / simulate failures."""
import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.infra.command_executor import OnStarted
from app.infra.process_control import ProcessIdentity
from app.models.states import CliOutcome
from app.providers.base import ProviderRequest, ProviderResult, command_fingerprint

Handler = Callable[["MockCall"], ProviderResult | Awaitable[ProviderResult]]


@dataclass
class MockCall:
    role: str
    prompt: str
    cwd: object
    resume_id: str | None
    request: ProviderRequest
    n: int                      # 1-based call number for this role


def ok(text: str = "ok", structured: dict | None = None, session: str | None = None) -> ProviderResult:
    return ProviderResult(outcome=CliOutcome.SUCCESS, text=text, structured=structured, native_session_id=session,
                          exit_code=0)


class MockProvider:
    # A PID that cannot exist: process-control tests must never target the real test process.
    FAKE_IDENTITY = ProcessIdentity(2_147_483_632, 1.0)

    def __init__(self, name: str = "MOCK", handlers: dict[str, Handler] | None = None,
                 identity: ProcessIdentity | None = FAKE_IDENTITY):
        self.name = name
        self.identity = identity
        self.handlers = handlers or {}
        self.calls: list[MockCall] = []
        self.log: list[str] = []            # may be shared between providers for chronological order
        self._counts: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.sessions: dict[str, str] = {}   # role -> last native id issued
        self.live_ids: set[str] = set()      # ids the mock "CLI" still knows (for resume validity)

    def _prepare(self, request: ProviderRequest, resume_id: str | None) -> MockCall:
        self._counts[request.role] = self._counts.get(request.role, 0) + 1
        call = MockCall(request.role, request.prompt, request.cwd, resume_id, request, self._counts[request.role])
        self.calls.append(call)
        self.log.append(request.role)
        return call

    async def _respond(self, call: MockCall, on_started: OnStarted | None) -> ProviderResult:
        if on_started:
            await on_started(self.identity)
        handler = self.handlers.get(call.role)
        result = handler(call) if handler else ok()
        if asyncio.iscoroutine(result):
            result = await result
        if call.resume_id and call.resume_id not in self.live_ids and result.outcome is CliOutcome.SUCCESS:
            return ProviderResult(outcome=CliOutcome.SESSION_MISSING, exit_code=1)
        if result.outcome is CliOutcome.SUCCESS and result.native_session_id is None:
            result.native_session_id = call.resume_id or str(uuid.uuid4())
        if result.native_session_id:
            self.live_ids.add(result.native_session_id)
            self.sessions[call.role] = result.native_session_id
        result.command_fingerprint = result.command_fingerprint or command_fingerprint([self.name, call.role])
        return result

    async def execute(self, request: ProviderRequest, on_started: OnStarted | None = None) -> ProviderResult:
        return await self._respond(self._prepare(request, None), on_started)

    async def resume(self, session_id: str, request: ProviderRequest,
                     on_started: OnStarted | None = None) -> ProviderResult:
        return await self._respond(self._prepare(request, session_id), on_started)

    def cancel(self, run_key: str) -> None:
        self.cancelled.append(run_key)

    def parse_result(self, raw, *, resumed_id=None, schema=None) -> ProviderResult:  # pragma: no cover
        raise NotImplementedError("MockProvider does not parse raw CLI output")

    def get_session_id(self, raw) -> str | None:  # pragma: no cover
        return None

