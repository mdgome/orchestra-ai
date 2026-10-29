"""Claude Code CLI adapter (`claude -p --output-format json`). Prompt goes through stdin."""
import json

from app.infra.command_executor import CommandExecutor, CommandResult, CommandSpec, OnStarted
from app.models.states import CliOutcome
from app.providers.base import ProviderRequest, ProviderResult, command_fingerprint, redact
from app.providers.permissions import claude_permission_args

SESSION_MISSING_MARKER = "No conversation found with session ID"


class ClaudeCliProvider:
    name = "CLAUDE"

    def __init__(self, executor: CommandExecutor, binary: str = "claude"):
        self._executor = executor
        self._bin = binary

    def build_argv(self, request: ProviderRequest, resume_id: str | None = None) -> list[str]:
        argv = [self._bin, "-p", "--output-format", "json", *claude_permission_args(request.role, request.one_off_tool)]
        if request.output_schema:
            argv += ["--json-schema", json.dumps(request.output_schema, separators=(",", ":"))]
        if resume_id:
            argv += ["--resume", resume_id]
        return argv

    async def execute(self, request: ProviderRequest, on_started: OnStarted | None = None) -> ProviderResult:
        return await self._run(request, None, on_started)

    async def resume(self, session_id: str, request: ProviderRequest,
                     on_started: OnStarted | None = None) -> ProviderResult:
        return await self._run(request, session_id, on_started)

    def cancel(self, run_key: str) -> None:
        self._executor.cancel(run_key)

    async def _run(self, request, resume_id, on_started) -> ProviderResult:
        argv = self.build_argv(request, resume_id)
        raw = await self._executor.run(
            CommandSpec(argv=argv, cwd=request.cwd, timeout_seconds=request.timeout_seconds,
                        stdin_text=request.prompt),
            run_key=request.run_key, on_started=on_started)
        # fingerprint binds the argv (tools/permissions) but not the resume id or the prompt
        fp_argv = [a for a in argv if a != resume_id]
        result = self.parse_result(raw, resumed_id=resume_id, schema=request.output_schema)
        result.command_fingerprint = command_fingerprint(fp_argv)
        return result

    def get_session_id(self, raw: CommandResult) -> str | None:
        data = _load_json(raw.stdout)
        sid = data.get("session_id") if data else None
        return sid if isinstance(sid, str) and sid else None

    def parse_result(self, raw: CommandResult, *, resumed_id: str | None = None,
                     schema: dict | None = None) -> ProviderResult:
        known = (resumed_id,) if resumed_id else ()
        sid = self.get_session_id(raw)
        base = ProviderResult(
            outcome=raw.outcome, exit_code=raw.exit_code, native_session_id=sid,
            stdout_tail=redact(raw.stdout, known + ((sid,) if sid else ())),
            stderr_tail=redact(raw.stderr, known), identity=raw.identity, termination=raw.termination)
        if raw.outcome in (CliOutcome.TIMEOUT, CliOutcome.IDLE, CliOutcome.CANCELLED):
            return base                                    # never reinterpret as a permission request
        if SESSION_MISSING_MARKER in raw.stderr or SESSION_MISSING_MARKER in raw.stdout:
            base.outcome = CliOutcome.SESSION_MISSING      # explicit "session does not exist" only
            base.native_session_id = None
            return base
        data = _load_json(raw.stdout)
        if data is None:
            base.outcome = CliOutcome.ERROR
            base.detail = {"reason": "unparseable CLI output" if raw.exit_code == 0 else "cli failed"}
            return base
        base.text = data.get("result") or ""
        structured = data.get("structured_output")
        base.structured = structured if isinstance(structured, dict) else None
        denials = data.get("permission_denials") or []
        base.permission_requests = [
            {"tool": d.get("tool_name"), "input": redact(json.dumps(d.get("tool_input", {}), ensure_ascii=False)),
             "tool_input": d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}}
            for d in denials if isinstance(d, dict)]
        if base.permission_requests:
            base.outcome = CliOutcome.PERMISSION_REQUESTED  # explicit denial reported by the CLI
        elif data.get("is_error") or raw.exit_code != 0:
            base.outcome = CliOutcome.ERROR
            base.detail = {"subtype": data.get("subtype"), "api_error_status": data.get("api_error_status")}
        else:
            base.outcome = CliOutcome.SUCCESS
        return base


def _load_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
