import json
from pathlib import Path

import pytest

from app.infra.command_executor import CommandResult
from app.models.states import CliOutcome
from app.providers.base import ProviderRequest, redact, session_fingerprint
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider
from app.providers.permissions import (
    authorize_one_off, claude_permission_args, codex_permission_args, is_test_path, permissions_for,
)
from app.validation.exceptions import FailClosed

FX = Path(__file__).parent.parent / "fixtures" / "cli"


def raw(stdout="", stderr="", exit_code=0, outcome=CliOutcome.SUCCESS):
    return CommandResult(argv=["x"], outcome=outcome, exit_code=exit_code, stdout=stdout, stderr=stderr,
                         duration_seconds=1.0)


claude = ClaudeCliProvider(executor=None)  # parse-only tests never touch the executor
codex = CodexCliProvider(executor=None)


# --- Claude parsing (real CLI fixtures) ----------------------------------------------------
def test_claude_parses_real_result_and_session_id():
    out = (FX / "claude_result_ok.json").read_text(encoding="utf-8")
    r = claude.parse_result(raw(out))
    assert r.outcome is CliOutcome.SUCCESS and "pong" in r.text
    assert r.native_session_id == json.loads(out)["session_id"]


def test_claude_structured_output_is_extracted():
    r = claude.parse_result(raw((FX / "claude_result_structured.json").read_text(encoding="utf-8")),
                            schema={"type": "object"})
    assert r.structured == {"verdict": "PASS", "reason": "2+2=4 is a true, basic arithmetic fact."}


def test_claude_missing_session_is_explicit_and_clears_the_id():
    err = (FX / "claude_resume_missing.stderr").read_text(encoding="utf-8")
    r = claude.parse_result(raw("", err, exit_code=1, outcome=CliOutcome.ERROR),
                            resumed_id="00000000-0000-4000-8000-000000000000")
    assert r.outcome is CliOutcome.SESSION_MISSING and r.native_session_id is None
    assert "00000000-0000-4000-8000-000000000000" not in r.stderr_tail  # redacted


def test_claude_generic_failure_is_not_session_missing():
    r = claude.parse_result(raw("", "network unreachable", exit_code=1, outcome=CliOutcome.ERROR))
    assert r.outcome is CliOutcome.ERROR


def test_claude_permission_denials_become_permission_requested():
    out = json.dumps({"is_error": False, "result": "x", "session_id": "s1", "permission_denials": [
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}]})
    r = claude.parse_result(raw(out))
    assert r.outcome is CliOutcome.PERMISSION_REQUESTED and r.permission_requests[0]["tool"] == "Bash"


@pytest.mark.parametrize("outcome", [CliOutcome.TIMEOUT, CliOutcome.IDLE, CliOutcome.CANCELLED])
def test_timeout_idle_cancel_are_never_treated_as_permission_requests(outcome):
    r = claude.parse_result(raw("", "", exit_code=None, outcome=outcome))
    assert r.outcome is outcome and not r.permission_requests
    r = codex.parse_result(raw("", "", exit_code=None, outcome=outcome))
    assert r.outcome is outcome and not r.permission_requests


def test_claude_unparseable_output_is_an_error_not_success():
    assert claude.parse_result(raw("not json")).outcome is CliOutcome.ERROR


# --- Codex parsing (real CLI fixtures) -------------------------------------------------------
def test_codex_parses_thread_id_and_last_agent_message():
    r = codex.parse_result(raw((FX / "codex_exec_ok.jsonl").read_text(encoding="utf-8")))
    assert r.outcome is CliOutcome.SUCCESS and r.text == "pong"
    assert r.native_session_id == "01a18672-8af5-7020-99e9-e89c588145c2"


def test_codex_structured_message_is_json_decoded_when_schema_given():
    r = codex.parse_result(raw((FX / "codex_exec_structured.jsonl").read_text(encoding="utf-8")),
                           schema={"type": "object"})
    assert r.structured == {"verdict": "PASS", "reason": "2 + 2 equals 4."}


def test_codex_missing_thread_is_explicit():
    err = (FX / "codex_resume_missing.stderr").read_text(encoding="utf-8")
    r = codex.parse_result(raw("", err, exit_code=1, outcome=CliOutcome.ERROR),
                           resumed_id="00000000-0000-4000-8000-000000000000")
    assert r.outcome is CliOutcome.SESSION_MISSING and r.native_session_id is None


def test_codex_no_agent_message_is_an_error():
    assert codex.parse_result(raw('{"type":"turn.started"}\n')).outcome is CliOutcome.ERROR


def test_codex_approval_event_is_permission_requested():
    r = codex.parse_result(raw('{"type":"exec_approval_request"}\n'))
    assert r.outcome is CliOutcome.PERMISSION_REQUESTED


# --- argv / permissions --------------------------------------------------------------------------
def req(role, **kw):
    return ProviderRequest(role=role, prompt="SECRET PROMPT", cwd=Path("."), **kw)


def test_prompt_is_never_part_of_argv_and_no_shell_is_used():
    argv = claude.build_argv(req("PLANNER"), None)
    assert "SECRET PROMPT" not in argv and argv[:2] == ["claude", "-p"]
    cargv = codex.build_argv(req("DESIGN_REVIEWER"), None, None)
    assert "SECRET PROMPT" not in cargv and cargv[-1] == "-"


def test_planner_uses_plan_mode_and_cannot_even_see_edit_or_bash():
    args = claude_permission_args("PLANNER")
    assert args[args.index("--permission-mode") + 1] == "plan"
    tools = args[args.index("--tools") + 1].split(",")
    assert set(tools) == {"Read", "Glob", "Grep"}
    assert "Edit" in args[args.index("--disallowedTools") + 1]


def test_tools_allowed_and_disallowed_are_three_distinct_flags():
    args = claude_permission_args("DEVELOPER")
    assert {"--tools", "--allowedTools", "--disallowedTools"} <= set(args)
    assert args[args.index("--tools") + 1] != args[args.index("--allowedTools") + 1]


def test_reviewers_are_read_only_with_never_approval():
    for role in ("DESIGN_REVIEWER", "CODE_REVIEWER"):
        assert codex_permission_args(role) == ["-s", "read-only", "-c", 'approval_policy="never"']
        assert not permissions_for(role).writes_files


def test_codex_resume_keeps_read_only_overrides():
    argv = codex.build_argv(req("CODE_REVIEWER"), "thread-1", None)
    assert argv[:4] == ["codex", "exec", "resume", "thread-1"]
    assert 'sandbox_mode="read-only"' in argv and 'approval_policy="never"' in argv


def test_one_off_grant_cannot_exceed_role_maximum():
    assert authorize_one_off("DEVELOPER", "Bash(npm test:*)") == "Bash(npm test:*)"
    with pytest.raises(FailClosed):
        authorize_one_off("PLANNER", "Bash(ls:*)")        # Planner has no Bash tool at all
    with pytest.raises(FailClosed):
        authorize_one_off("DESIGN_REVIEWER", "Bash")      # Codex cannot enforce a one-off grant
    with pytest.raises(FailClosed):
        authorize_one_off("DEVELOPER", "WebFetch")        # not in the role's tool set


def test_one_off_grant_is_added_for_that_call_only():
    base = claude_permission_args("DEVELOPER")
    granted = claude_permission_args("DEVELOPER", "Bash(npm test:*)")
    assert "Bash(npm test:*)" not in base[base.index("--allowedTools") + 1]
    assert "Bash(npm test:*)" in granted[granted.index("--allowedTools") + 1]


def test_tester_test_path_helper():
    assert is_test_path("tests/test_a.py") and is_test_path("pkg/test_b.py") and is_test_path("conftest.py")
    assert not is_test_path("app/main.py")


# --- redaction -------------------------------------------------------------------------------------
def test_redact_masks_known_and_uuid_shaped_session_ids():
    sid = "01a18672-8af5-7020-99e9-e89c588145c2"
    text = f"resuming {sid} and also 3eb2861c-bfbb-4b02-80ac-f0406bcc9ee2"
    out = redact(text, (sid,))
    assert sid not in out and "3eb2861c" not in out
    assert session_fingerprint(sid) in out


def test_needs_human_review_without_gate_request_is_completed_by_the_orchestrator():
    from app.services.artifacts import validate_code_review
    out = validate_code_review({"verdict": "NEEDS_HUMAN_REVIEW", "issues": [{"severity": "MAJOR", "description": "odd"}],
                                "summary": "unsure", "human_gate_request": None})
    assert [o["id"] for o in out["human_gate_request"]["options"]] == ["accept", "revise"]
    assert "odd" in out["human_gate_request"]["problem"]
