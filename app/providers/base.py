"""Provider interface shared by the Codex / Claude adapters and the mock."""
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.infra.command_executor import CommandResult, OnStarted
from app.infra.process_control import ProcessIdentity
from app.models.states import CliOutcome
from app.validation import policies as P

_UUIDISH = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def session_fingerprint(native_id: str) -> str:
    """Irreversible short tag, safe for audit logs. Cannot be used to resume a session."""
    return "sess:" + hashlib.sha256(native_id.encode()).hexdigest()[:8]


def redact(text: str | None, known_ids: tuple[str, ...] = ()) -> str:
    """Mask native session ids (known ones, and anything UUID-shaped) before text is stored or returned."""
    if not text:
        return ""
    for known in known_ids:
        if known:
            text = text.replace(known, session_fingerprint(known))
    return _UUIDISH.sub(lambda m: session_fingerprint(m.group(0)), text)[-P.LOG_TAIL_CHARS:]


def command_fingerprint(argv: list[str], extra: str = "") -> str:
    """Identifies *what may run* (argv without the stdin prompt); used to bind PERMISSION approvals."""
    return hashlib.sha256(("\x00".join(argv) + "\x01" + extra).encode()).hexdigest()


@dataclass
class ProviderRequest:
    role: str
    prompt: str
    cwd: Path
    timeout_seconds: int = P.DEFAULT_CLI_TIMEOUT_SECONDS
    output_schema: dict | None = None
    run_key: str | None = None
    # A one-off, execution-bound tool grant from a PERMISSION approval (never persisted as a standing right).
    one_off_tool: str | None = None


@dataclass
class ProviderResult:
    outcome: CliOutcome
    text: str = ""
    structured: dict | None = None
    native_session_id: str | None = None   # internal: persist in agent_sessions only, never log/return
    exit_code: int | None = None
    stdout_tail: str = ""                   # already redacted
    stderr_tail: str = ""                   # already redacted
    command_fingerprint: str | None = None
    permission_requests: list[dict] = field(default_factory=list)
    identity: ProcessIdentity | None = None
    termination: str | None = None
    detail: dict = field(default_factory=dict)


class CliProvider(Protocol):
    name: str

    async def execute(self, request: ProviderRequest, on_started: OnStarted | None = None) -> ProviderResult: ...

    async def resume(self, session_id: str, request: ProviderRequest,
                     on_started: OnStarted | None = None) -> ProviderResult: ...

    def cancel(self, run_key: str) -> None: ...

    def parse_result(self, raw: CommandResult, *, resumed_id: str | None = None,
                     schema: dict | None = None) -> ProviderResult: ...

    def get_session_id(self, raw: CommandResult) -> str | None: ...
