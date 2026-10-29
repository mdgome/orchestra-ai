"""Codex CLI adapter (`codex exec --json`). Prompt goes through stdin ('-'). Always read-only here."""
import json
import os
import tempfile

from app.infra.command_executor import CommandExecutor, CommandResult, CommandSpec, OnStarted
from app.models.states import CliOutcome
from app.providers.base import ProviderRequest, ProviderResult, command_fingerprint, redact
from app.providers.permissions import codex_permission_args

SESSION_MISSING_MARKER = "no rollout found for thread id"


class CodexCliProvider:
    name = "CODEX"

    def __init__(self, executor: CommandExecutor, binary: str = "codex"):
        self._executor = executor
        self._bin = binary

    def build_argv(self, request: ProviderRequest, resume_id: str | None, schema_file: str | None) -> list[str]:
        argv = [self._bin, "exec"]
        if resume_id:
            # `exec resume` has a reduced option set; sandbox/approval come from -c overrides.
            perms = codex_permission_args(request.role)
            argv += ["resume", resume_id, "--json", "--skip-git-repo-check",
                     "-c", f'sandbox_mode="{perms[1]}"', perms[2], perms[3], "-"]
            return argv
        argv += ["--json", "--skip-git-repo-check", *codex_permission_args(request.role)]
        if schema_file:
            argv += ["--output-schema", schema_file]
        argv.append("-")
        return argv

    async def execute(self, request: ProviderRequest, on_started: OnStarted | None = None) -> ProviderResult:
        return await self._run(request, None, on_started)

    async def resume(self, session_id: str, request: ProviderRequest,
                     on_started: OnStarted | None = None) -> ProviderResult:
        return await self._run(request, session_id, on_started)

    def cancel(self, run_key: str) -> None:
        self._executor.cancel(run_key)

    async def _run(self, request, resume_id, on_started) -> ProviderResult:
        schema_file = None
        if request.output_schema and not resume_id:
            fd, schema_file = tempfile.mkstemp(suffix=".schema.json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(request.output_schema, fh)
        try:
            argv = self.build_argv(request, resume_id, schema_file)
            raw = await self._executor.run(
                CommandSpec(argv=argv, cwd=request.cwd, timeout_seconds=request.timeout_seconds,
                            stdin_text=request.prompt),
                run_key=request.run_key, on_started=on_started)
        finally:
            if schema_file:
                os.unlink(schema_file)
        result = self.parse_result(raw, resumed_id=resume_id, schema=request.output_schema)
        result.command_fingerprint = command_fingerprint(
            [a for a in argv if a not in (resume_id, schema_file)])
        return result

    def _events(self, raw: CommandResult) -> list[dict]:
        events = []
        for line in raw.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def get_session_id(self, raw: CommandResult) -> str | None:
        for event in self._events(raw):
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                return event["thread_id"]
        return None

    def parse_result(self, raw: CommandResult, *, resumed_id: str | None = None,
                     schema: dict | None = None) -> ProviderResult:
        known = (resumed_id,) if resumed_id else ()
        sid = self.get_session_id(raw)
        base = ProviderResult(
            outcome=raw.outcome, exit_code=raw.exit_code, native_session_id=sid,
            stdout_tail=redact(raw.stdout, known + ((sid,) if sid else ())),
            stderr_tail=redact(raw.stderr, known), identity=raw.identity, termination=raw.termination)
        if raw.outcome in (CliOutcome.TIMEOUT, CliOutcome.IDLE, CliOutcome.CANCELLED):
            return base
        if SESSION_MISSING_MARKER in raw.stderr:
            base.outcome = CliOutcome.SESSION_MISSING
            base.native_session_id = None
            return base
        events = self._events(raw)
        approvals = [e for e in events if "approval" in str(e.get("type", "")).lower()]
        if approvals:  # explicit approval request reported by the CLI
            base.outcome = CliOutcome.PERMISSION_REQUESTED
            base.permission_requests = [{"tool": str(e.get("type")), "input": ""} for e in approvals]
            return base
        messages = [e["item"]["text"] for e in events
                    if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)
                    and e["item"].get("type") == "agent_message" and isinstance(e["item"].get("text"), str)]
        failed = any(e.get("type") in ("turn.failed", "error") for e in events)
        if raw.exit_code != 0 or failed or not messages:
            base.outcome = CliOutcome.ERROR
            base.detail = {"reason": "no agent message" if not messages and raw.exit_code == 0 else "cli failed"}
            return base
        base.text = messages[-1]
        if schema:
            try:
                parsed = json.loads(base.text)
                base.structured = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                base.structured = None
        base.outcome = CliOutcome.SUCCESS
        return base
