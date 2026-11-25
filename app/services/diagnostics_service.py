"""Read-only project diagnostics: what can be observed about a project WITHOUT running anything of the project's.

Never run here: a provider/agent, the auth probe (`probe_write_blocked` spends tokens), a dependency installer or the
project's test command. Only version probes of tools (`git --version`, `java -version`) and file inspection.

Deadlines, honestly:
* a subprocess (CommandExecutor) is bounded by its own timeout and terminated by the executor;
* file inspection runs in a worker thread and its timeout is COOPERATIVE/best-effort: a stalled filesystem cannot be
  interrupted from Python, so a waiter that gives up leaves the thread running. Two caps keep such threads from piling
  up: one inspection per project and DIAGNOSTICS_MAX_FS_WORKERS overall, both released only when the thread really ends.
  While a project's worker is still alive, a new run reports the file-derived checks as UNKNOWN instead of starting another.

Single flight: every request for the same key shares ONE `asyncio.Task` (`_inflight`). Cache lookup, in-flight lookup and
task registration happen under `_lock`, which is never held while the diagnosis itself runs. A waiter that is cancelled
only ends itself (`asyncio.shield`). The task stores a complete result in the TTL cache BEFORE it finishes and the done
callback removes it from `_inflight` only if it is still the registered one, so there is no gap in which a request finds
neither and starts a second diagnosis, and a stale callback cannot remove a newer task.
"""
import asyncio
import dataclasses
import enum
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from app.infra.command_executor import CommandExecutor, CommandSpec
from app.infra.workspace_guard import resolve_inside_root, resolve_project_root
from app.models.states import CliOutcome
from app.providers.preflight import PreflightReport, scan_home_settings, scan_project_settings
from app.services.hashing import stable_hash
from app.services.project_service import ProjectService
from app.services.runtime_status import BackgroundComponentStatus, RuntimeSnapshot
from app.services.sweeper_stall import SweeperConfigInvalidSnapshot, SweeperStallSnapshot
from app.services.test_policy_resolver import resolve_test_policy
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import AppError

DISCLAIMER = ("Diagnostics report only what could be observed without running the agents or the project's tests. "
              "They do not guarantee that a task can run, or that it will succeed.")


class Status(str, enum.Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


_SEVERITY_RANK = {Status.FAIL: 3, Status.WARN: 2, Status.UNKNOWN: 1, Status.PASS: 0}


@dataclass(frozen=True)
class DiagnosticCause:
    """One cause of an item (design 14.1, 14.2). Internal view model: its attribute names equal the field names of
    `DiagnosticCauseOut`; it carries identifiers and fixed codes only, never `detail`, SQL text or an exception string."""
    reason_code: str
    severity: Status
    blocks_execution: bool
    cause: str
    recommendation: str
    related_execution_ids: tuple[uuid.UUID, ...] = ()
    related_request_ids: tuple[uuid.UUID, ...] = ()
    anomalies: tuple = ()                           # TrackedAnomaly items (project_terminal), only TRACKED_EXECUTION_ANOMALY
    anomalies_returned: int = 0
    anomalies_truncated: bool = False
    anomalies_total: int | None = None
    anomalies_total_is_exact: bool = False
    may_be_permanently_stuck: bool = False
    sweeper: SweeperStallSnapshot | None = None
    config_invalid: SweeperConfigInvalidSnapshot | None = None
    components: tuple[BackgroundComponentStatus, ...] = ()


@dataclass(frozen=True)
class DiagnosticItem:
    code: str
    status: Status
    checked_at: datetime
    cause: str = ""
    recommendation: str = ""
    blocks_execution: bool = False        # only ever true for a FAIL
    causes: tuple[DiagnosticCause, ...] = ()
    blocks_features: tuple[str, ...] = ()   # PROCESS_TRACKING only, and only when the capability is not SUPPORTED


@dataclass(frozen=True)
class TrackedVerdict:
    """What the injected tracked-executions reader returns for one project: the causes (unmerged) and the number of tracked
    executions that are running normally (information only)."""
    causes: tuple[DiagnosticCause, ...] = ()
    running: int = 0


def merge_causes(causes) -> tuple[DiagnosticCause, ...]:
    """14.1: duplicates by `reason_code` are removed (their ids merged in ascending order); FAIL before WARN, then by
    `reason_code` in ascending ASCII order."""
    merged: dict[str, DiagnosticCause] = {}
    for c in causes:
        first = merged.get(c.reason_code)
        if first is None:
            merged[c.reason_code] = c
            continue
        merged[c.reason_code] = dataclasses.replace(
            first, blocks_execution=first.blocks_execution or c.blocks_execution,
            severity=max(first.severity, c.severity, key=_SEVERITY_RANK.get),
            related_execution_ids=tuple(sorted({*first.related_execution_ids, *c.related_execution_ids})),
            related_request_ids=tuple(sorted({*first.related_request_ids, *c.related_request_ids})))
    return tuple(sorted(merged.values(), key=lambda c: (-_SEVERITY_RANK[c.severity], c.reason_code)))


@dataclass(frozen=True)
class DiagnosticsReport:
    project_id: uuid.UUID
    generated_at: datetime
    can_start: bool
    blockers: tuple[str, ...]
    items: tuple[DiagnosticItem, ...]
    disclaimer: str = DISCLAIMER
    cached: bool = False


@dataclass(frozen=True)
class _ProjectView:
    """What a run needs from the Project row, copied so the shared task never holds an ORM object."""
    id: uuid.UUID
    workspace_path: str
    test_policy: dict | None
    write_owner_task_id: uuid.UUID | None
    workspace_state: str


def _first_line(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line[:200]


class DiagnosticsService:
    def __init__(self, projects: ProjectService, executor: CommandExecutor, *, protected: tuple[Path, ...],
                 get_runtime: Callable[[], RuntimeSnapshot], get_preflight: Callable[[], PreflightReport | None],
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 read_capability: Callable[[], Awaitable] | None = None,
                 read_tracked: Callable[[uuid.UUID], Awaitable[TrackedVerdict]] | None = None):
        self._projects, self._executor, self._protected = projects, executor, tuple(protected)
        self._get_runtime, self._get_preflight = get_runtime, get_preflight
        # plain async callables (no import of a repository or a session here): the capability reader of 6.2 (a reading with
        # `.state` and `.reason_code`) and the project-scoped tracked-executions reader of 14.1; None = not configured
        self._read_capability, self._read_tracked = read_capability, read_tracked
        self._clock, self._now = clock, now
        self._lock = asyncio.Lock()
        self._cache: dict[tuple, tuple[float, DiagnosticsReport]] = {}
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._fs_busy: set[uuid.UUID] = set()
        self._fs_total = 0

    # --- single flight ---------------------------------------------------------------------------------
    async def diagnose(self, project_id: uuid.UUID, *, refresh: bool = False) -> DiagnosticsReport:
        project = await self._projects.get_project(project_id)       # 404 for an unknown project
        view = _ProjectView(project.id, project.workspace_path, project.test_policy, project.write_owner_task_id,
                            project.workspace_state)
        # workspace identity + test policy + policy version + the live state some items are derived from (the background
        # revision, Runner readiness, the derived display set of the SAME snapshot, write-lock owner, workspace state): a
        # change of any of them makes the old entry unreachable (14.3). The ONE runtime snapshot of this read is passed on.
        runtime = self._get_runtime()
        key = (view.id, os.path.normcase(view.workspace_path), stable_hash(view.test_policy), P.DIAGNOSTICS_POLICY_VERSION,
               runtime.background_revision, runtime.ready, runtime.blockers, runtime.derived_states,
               view.write_owner_task_id, view.workspace_state)
        async with self._lock:
            hit = self._cache.get(key)
            if hit is not None and not refresh and hit[0] > self._clock():
                return dataclasses.replace(hit[1], cached=True)
            shared = self._inflight.get(key)
            if shared is not None and shared.done():
                shared = None                       # finished, its callback has not run yet: not "in flight" any more
            if shared is None:
                shared = asyncio.create_task(self._run(key, view, runtime))      # created, not awaited: the lock stays short
                self._inflight[key] = shared
                shared.add_done_callback(partial(self._finished, key))
        return await asyncio.shield(shared)          # this waiter's cancellation never cancels the shared task

    def _finished(self, key: tuple, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()                         # consumed: a failure reaches the waiters, it is not "never retrieved"
        if self._inflight.get(key) is task:          # a stale callback must not remove a newer task of the same key
            del self._inflight[key]

    def _store(self, key: tuple, report: DiagnosticsReport) -> None:
        for other in [k for k in self._cache if k[0] == key[0] and k != key]:
            del self._cache[other]                   # the project's workspace/policy changed: the old entry is stale
        self._cache[key] = (self._clock() + P.DIAGNOSTICS_CACHE_TTL_SECONDS, report)

    # --- bounded file inspection ------------------------------------------------------------------------
    async def _fs(self, project_id: uuid.UUID, fn: Callable[[], dict], timeout: float) -> tuple[str, object]:
        """("ok", facts) | ("busy", None) | ("timeout", None) | ("error", exc). A busy answer starts no new thread."""
        if project_id in self._fs_busy or self._fs_total >= P.DIAGNOSTICS_MAX_FS_WORKERS:
            return "busy", None
        fut = asyncio.get_running_loop().run_in_executor(None, fn)
        self._fs_busy.add(project_id)
        self._fs_total += 1
        fut.add_done_callback(partial(self._fs_released, project_id))     # released when the THREAD ends, not on timeout
        done, _ = await asyncio.wait({fut}, timeout=timeout)              # unlike wait_for it does not cancel the future
        if fut not in done:
            return "timeout", None
        try:
            return "ok", fut.result()
        except Exception as exc:  # noqa: BLE001 - reported as an UNKNOWN item
            return "error", exc

    def _fs_released(self, project_id: uuid.UUID, fut: asyncio.Future) -> None:
        if not fut.cancelled():
            fut.exception()
        self._fs_busy.discard(project_id)
        self._fs_total -= 1

    def _inspect_fs(self, view: _ProjectView) -> dict:
        """Blocking. Reads files only; runs in a worker thread."""
        facts: dict = {}
        try:
            root = resolve_project_root(view.workspace_path)
            facts["root"] = root
        except AppError as exc:
            facts["access_error"] = exc.message
            root = None
        try:
            V.validate_project_path(view.workspace_path, protected=self._protected)
        except AppError as exc:
            facts["path_error"] = exc.message
        if root is None:
            return facts
        try:
            facts["settings"] = scan_project_settings(root) + scan_home_settings()
        except Exception as exc:  # noqa: BLE001
            facts["settings_error"] = type(exc).__name__
        try:
            policy = resolve_test_policy(view.test_policy, root)
            facts["policy"] = policy
            argv0 = policy["commands"][0][0]
            facts["wrapper"] = os.path.normcase(str(argv0)).startswith(os.path.normcase(str(root)) + os.sep)
        except AppError as exc:
            facts["policy_error"] = exc.message
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
            try:
                if resolve_inside_root(root, rel).exists():
                    facts["venv"] = rel
                    break
            except AppError:
                facts["venv_link"] = rel
        return facts

    # --- one run ------------------------------------------------------------------------------------------
    async def _run(self, key: tuple, view: _ProjectView, runtime: RuntimeSnapshot) -> DiagnosticsReport:
        deadline = self._clock() + P.DIAGNOSTICS_DEADLINE_SECONDS
        status, facts = await self._fs(view.id, partial(self._inspect_fs, view), P.DIAGNOSTICS_ITEM_TIMEOUT_SECONDS)
        facts = facts if status == "ok" else None
        fs_note = {"busy": "a previous file inspection of this project is still running",
                   "timeout": "the file inspection did not finish in time (the filesystem may be slow)",
                   "error": "the file inspection failed unexpectedly", "ok": ""}[status]
        root: Path | None = facts.get("root") if facts else None
        policy = facts.get("policy") if facts else None
        preset = (policy or {}).get("preset")
        probes: dict[str, tuple[str, str]] = {}
        if root is not None:
            argvs = {"git": ["git", "--version"]}
            if preset in ("maven", "gradle"):
                argvs["java"] = ["java", "-version"]
            results = await asyncio.gather(*(self._version(root, argv, deadline) for argv in argvs.values()))
            probes = dict(zip(argvs, results))
        git, java = probes.get("git"), probes.get("java")
        capability = await self._reading(self._read_capability)
        tracked = await self._reading(self._read_tracked, view.id)
        items = [*self._workspace_items(facts, fs_note), self._git_item(root, git, fs_note),
                 *self._cli_items(), self._settings_item(facts, fs_note), self._auth_item(),
                 *self._policy_items(facts, fs_note), self._java_item(facts, java), self._python_item(facts, policy),
                 self._env_item(policy), self._junit_item(policy), self._runner_item(runtime),
                 self._process_tracking_item(capability), self._tracked_item(tracked), self._lock_item(view)]
        blockers = tuple(i.code for i in items if i.blocks_execution)
        report = DiagnosticsReport(view.id, self._now(), not blockers, blockers, tuple(items))
        async with self._lock:
            self._store(key, report)         # BEFORE the task ends, so no request ever sees "no cache, no in-flight"
        return report

    async def _version(self, root: Path, argv: list[str], deadline: float) -> tuple[str, str]:
        """("ok"|"missing"|"timeout"|"error", first line). A version probe only; no project code runs."""
        timeout = max(1, int(min(P.DIAGNOSTICS_ITEM_TIMEOUT_SECONDS, deadline - self._clock())))
        try:
            result = await self._executor.run(CommandSpec(argv=argv, cwd=root, timeout_seconds=timeout,
                                                          idle_seconds=None, workspace_root=root))
        except AppError:
            return "missing", ""
        if result.outcome is CliOutcome.TIMEOUT:
            return "timeout", ""
        if result.exit_code != 0:
            return "error", _first_line(result.stderr or result.stdout)
        return "ok", _first_line(result.stdout or result.stderr)       # `java -version` prints to stderr

    def _item(self, code: str, status: Status, cause: str = "", recommendation: str = "",
              blocks: bool = False) -> DiagnosticItem:
        return DiagnosticItem(code, status, self._now(), cause, recommendation, blocks and status is Status.FAIL)

    def _unknown(self, code: str, why: str) -> DiagnosticItem:
        return self._item(code, Status.UNKNOWN, why or "not checked", "retry the diagnostics later")

    # --- items ------------------------------------------------------------------------------------------------
    def _workspace_items(self, facts: dict | None, note: str) -> list[DiagnosticItem]:
        if facts is None:
            return [self._unknown("WORKSPACE_ACCESS", note), self._unknown("WORKSPACE_PATH_POLICY", note)]
        access = (self._item("WORKSPACE_ACCESS", Status.FAIL, facts["access_error"],
                             "restore the registered workspace (it must be a plain, accessible directory)", True)
                  if "access_error" in facts else self._item("WORKSPACE_ACCESS", Status.PASS, "workspace is accessible"))
        path = (self._item("WORKSPACE_PATH_POLICY", Status.FAIL, facts["path_error"],
                           "register a workspace that is not a system, home or orchestrator-managed location", True)
                if "path_error" in facts else self._item("WORKSPACE_PATH_POLICY", Status.PASS,
                                                         "workspace path satisfies the path policy"))
        return [access, path]

    def _git_item(self, root: Path | None, probe: tuple[str, str] | None, note: str) -> DiagnosticItem:
        if root is None or probe is None:
            return self._unknown("GIT_AVAILABLE", note or "the workspace was not accessible")
        state, text = probe
        if state == "ok":
            return self._item("GIT_AVAILABLE", Status.PASS, text)
        return self._item("GIT_AVAILABLE", Status.WARN, f"git could not be run ({state})",
                          "install git: change tracking falls back to content hashing without it")

    def _cli_items(self) -> list[DiagnosticItem]:
        report = self._get_preflight()
        out: list[DiagnosticItem] = []
        for name in ("claude", "codex"):
            up, flags = f"CLI_{name.upper()}_INSTALLED", f"CLI_{name.upper()}_FLAGS"
            if report is None:
                out += [self._unknown(up, "the startup preflight did not run"), self._unknown(flags, "the startup preflight did not run")]
                continue
            missing = report.missing_flags.get(name)
            if missing is None:
                out += [self._unknown(up, "not checked"), self._unknown(flags, "not checked")]
            elif "--help" in missing:
                out += [self._item(up, Status.FAIL, f"`{name}` could not be run", f"install the {name} CLI and restart", True),
                        self._unknown(flags, f"{name} is not installed")]
            elif missing:
                out += [self._item(up, Status.PASS, report.versions.get(name, "")),
                        self._item(flags, Status.FAIL, f"missing: {', '.join(missing)}", f"upgrade the {name} CLI", True)]
            else:
                out += [self._item(up, Status.PASS, report.versions.get(name, "")),
                        self._item(flags, Status.PASS, "all required flags are supported")]
        return out

    def _settings_item(self, facts: dict | None, note: str) -> DiagnosticItem:
        if facts is None or "settings" not in facts:
            return self._unknown("CLI_SETTINGS_SAFETY",
                                 note or (facts or {}).get("settings_error") or "workspace not accessible")
        findings = facts["settings"]
        high = [f for f in findings if f["severity"] == "high"]
        listed = "; ".join(f"{f['source']}: {f['issue']}" for f in (high or findings)[:5])
        if high:
            return self._item("CLI_SETTINGS_SAFETY", Status.FAIL, listed, "remove the permission-widening settings", True)
        if findings:
            return self._item("CLI_SETTINGS_SAFETY", Status.WARN, listed, "review the broad allow rules")
        return self._item("CLI_SETTINGS_SAFETY", Status.PASS, "no permission-widening settings found")

    def _auth_item(self) -> DiagnosticItem:
        return self._item("CLI_AUTH", Status.UNKNOWN,
                          "login state is not probed: a probe would run the CLI and spend tokens",
                          "check it yourself with the CLI's own login/status command")

    def _policy_items(self, facts: dict | None, note: str) -> list[DiagnosticItem]:
        if facts is None or ("policy" not in facts and "policy_error" not in facts):
            why = note or "workspace not accessible"
            return [self._unknown("TEST_PRESET_RESOLVABLE", why), self._unknown("TEST_WRAPPER", why)]
        if "policy_error" in facts:
            return [self._item("TEST_PRESET_RESOLVABLE", Status.FAIL, facts["policy_error"],
                               "install the build tool or add its wrapper to the workspace", True),
                    self._unknown("TEST_WRAPPER", "the test policy does not resolve")]
        policy = facts["policy"]
        preset = policy.get("preset")
        command = policy["commands"][0][0]
        resolved = self._item("TEST_PRESET_RESOLVABLE", Status.PASS,
                              f"{'preset ' + preset if preset else 'explicit command'} resolves to {command}")
        if preset not in ("maven", "gradle"):
            return [resolved, self._item("TEST_WRAPPER", Status.PASS, "not applicable to this test policy")]
        if facts.get("wrapper"):
            return [resolved, self._item("TEST_WRAPPER", Status.PASS, "the project's own wrapper will be used")]
        return [resolved, self._item("TEST_WRAPPER", Status.WARN, "no project wrapper: the system installation is used",
                                     "add the Maven/Gradle wrapper for a reproducible build")]

    def _java_item(self, facts: dict | None, probe: tuple[str, str] | None) -> DiagnosticItem:
        preset = ((facts or {}).get("policy") or {}).get("preset")
        if preset not in ("maven", "gradle"):
            return self._item("JAVA_RUNTIME", Status.PASS, "not required by this test policy")
        if probe is None:
            return self._unknown("JAVA_RUNTIME", "not checked")
        state, text = probe
        if state == "ok":
            return self._item("JAVA_RUNTIME", Status.PASS, text)
        if state in ("missing", "error"):
            return self._item("JAVA_RUNTIME", Status.FAIL, f"`java` could not be run ({state})",
                              "install a JDK and make `java` available on PATH", True)
        return self._unknown("JAVA_RUNTIME", "`java -version` timed out")

    def _python_item(self, facts: dict | None, policy: dict | None) -> DiagnosticItem:
        selected = f"selected: the orchestrator's interpreter ({Path(sys.executable).name})"
        if facts is None or policy is None:
            return self._unknown("PYTHON_INTERPRETER", selected)
        runs_python = (policy.get("preset") in (None, "pytest")
                       and any(c and c[0] in ("python", sys.executable) for c in policy["commands"]))
        if not runs_python:
            return self._item("PYTHON_INTERPRETER", Status.PASS, "not used by this test policy")
        if "venv" in facts:
            return self._item("PYTHON_INTERPRETER", Status.WARN,
                              f"discovered the project interpreter {facts['venv']}, but it is NOT used; {selected}",
                              "install the project's dependencies into the orchestrator's environment, "
                              "or wait for the interpreter opt-in (not available yet)")
        return self._item("PYTHON_INTERPRETER", Status.PASS, f"no project interpreter found; {selected}")

    def _env_item(self, policy: dict | None) -> DiagnosticItem:
        if policy is None:
            return self._unknown("TEST_ENV", "the test policy is not resolved")
        wanted = list(policy.get("env_allow") or [])
        missing = [n for n in wanted if n not in os.environ]
        base = "tests run with a restricted environment; the CLI login variables are never forwarded"
        if missing:
            return self._item("TEST_ENV", Status.WARN, f"{base}; not set in the orchestrator, so not forwarded: "
                              f"{', '.join(missing)}", "set them in the orchestrator's environment")
        return self._item("TEST_ENV", Status.PASS, base + (f"; forwarded: {', '.join(wanted)}" if wanted else ""))

    def _junit_item(self, policy: dict | None) -> DiagnosticItem:
        if policy is None:
            return self._unknown("JUNIT_COLLECTION", "the test policy is not resolved")
        detail = (f"results are read from {policy['junit_path']}; at least {policy['min_tests_executed']} executed "
                  f"test(s) and a result newer than the run are required")
        if policy.get("preset") == "gradle":
            return self._item("JUNIT_COLLECTION", Status.WARN,
                              detail + "; Gradle may skip the test task (UP-TO-DATE / FROM-CACHE), which leaves the XML "
                              "unchanged and is reported as 'no new result'",
                              "run with a rerun option if a task fails for that reason (not handled automatically yet)")
        return self._item("JUNIT_COLLECTION", Status.PASS, detail)

    async def _reading(self, fn, *args) -> tuple[str, object]:
        """("ok", value) | ("unconfigured", None) | ("failed", None): an injected reader is bounded by the item timeout and a
        failure of it is an UNKNOWN item, never an exception of the whole diagnosis."""
        if fn is None:
            return "unconfigured", None
        try:
            return "ok", await asyncio.wait_for(fn(*args), P.DIAGNOSTICS_ITEM_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - includes TimeoutError; the detail is never put into a response
            return "failed", None

    def _runner_item(self, snap: RuntimeSnapshot) -> DiagnosticItem:
        """The base PASS / WARN / FAIL decision from the blockers is unchanged; the background and sweeper causes (14.1) are
        independent of it and the item keeps the HIGHEST severity of its base status and of every cause (revision 34, A5)."""
        base = self._runner_base(snap)
        causes = merge_causes(self._runner_causes(snap))
        if not causes:
            return base
        status = max((base.status, *(c.severity for c in causes)), key=_SEVERITY_RANK.get)
        return dataclasses.replace(
            base, status=status, blocks_execution=base.blocks_execution or any(c.blocks_execution for c in causes),
            cause="; ".join([base.cause, *(c.cause for c in causes)]),
            recommendation="; ".join(dict.fromkeys([base.recommendation, *(c.recommendation for c in causes)])),
            causes=causes)

    def _runner_base(self, snap: RuntimeSnapshot) -> DiagnosticItem:
        if snap.ready:
            return self._item("RUNNER_STATE", Status.PASS, "the Runner is running and ready")
        if snap.blockers == ("ADVISORY_LOCK_HELD",):
            # supported topology: another API process runs the tasks, this one only stores requests. That process is not
            # inspected from here, so it is neither PASS (unproven) nor a blocker (requests do get executed).
            return self._item("RUNNER_STATE", Status.WARN,
                              "another process holds the Runner lock; this instance only stores requests and that "
                              "process executes them (its health is not checked from here)",
                              "check the instance that runs the Runner if tasks stay PENDING")
        return self._item("RUNNER_STATE", Status.FAIL,
                          f"the Runner is not ready ({', '.join(snap.blockers)}): execution requests are stored as "
                          "PENDING and wait until a Runner is ready",
                          "fix the blocker (see the other items) or restart the API", True)

    @staticmethod
    def _runner_causes(snap: RuntimeSnapshot) -> list[DiagnosticCause]:
        out: list[DiagnosticCause] = []
        degraded = tuple(c for c in snap.background_components if not c.is_derived)
        derived = tuple(c for c in snap.background_components if c.is_derived)
        if snap.background_degraded and degraded:       # the REAL cause exists only while a unit is really DEGRADED
            supervisor = any(c.component == "supervisor" for c in degraded)
            out.append(DiagnosticCause(
                "BACKGROUND_COMPONENT_DEGRADED", Status.FAIL if supervisor else Status.WARN, supervisor,
                "background component(s) are degraded: " + ", ".join(c.component for c in degraded),
                "restart the Runner after diagnosing the component; it is not restarted automatically", components=degraded))
        if derived:                                     # display only: always WARN, never a blocker (RUNNING snapshots only)
            out.append(DiagnosticCause(
                "BACKGROUND_COMPONENT_DERIVED", Status.WARN, False,
                "background component(s) are in a derived abnormal state that the supervisor has not settled (display only): "
                + ", ".join(f"{c.component} ({c.display_state})" for c in derived),
                "check the Runner; the underlying component state is unchanged and a lookup starts nothing",
                components=derived))
        stall, config = snap.sweeper_stall, snap.sweeper_config_invalid
        if stall is not None and stall.stalled:
            out.append(DiagnosticCause(
                "SWEEPER_STALLED", Status.WARN, False,
                f"the orphan sweeper is stalled at the position {stall.current_failure_cursor} (first failure at "
                f"{stall.first_failure_cursor}) after {stall.consecutive_failures} failed passes (last fixed failure code "
                f"{stall.last_failure_code}, counted since the Runner started); it skips no row and retries with a capped backoff",
                "diagnose the failing page or classification (an operator action; B2 never skips the row)", sweeper=stall))
        if config is not None and config.config_invalid_active:
            if config.config_currently_valid is True:
                text = ("an earlier orphan sweeper configuration error is not cleared yet; the configuration is valid now and "
                        "the state clears when a pass has passed the position "
                        f"({config.config_invalid_trigger_position}); no row is skipped")
                rec = "no correction is needed; wait for the sweeper pass"
            else:
                text = ("the orphan sweeper configuration is invalid (the fixed code SWEEP_CONFIG_INVALID); the entry of a pass "
                        f"failed at the trigger position ({config.config_invalid_trigger_position}); the last entry-time "
                        "snapshot failure set a wait of ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS that the wake-up of CI-7 ends "
                        "once when the configuration is valid again; no row is skipped")
                rec = ("correct the sweeper configuration (an operator action); the state clears when the failure position "
                       "has been passed afterwards")
            neutral = SweeperStallSnapshot(False, None, None, None, None, 0, None, None, None)
            out.append(DiagnosticCause("SWEEPER_CONFIG_INVALID", Status.WARN, False, text, rec,
                                       sweeper=stall if stall is not None else neutral, config_invalid=config))
        return out

    def _process_tracking_item(self, reading: tuple[str, object]) -> DiagnosticItem:
        """6.2: the reason codes are the `cause`; `blocks_features` only for the three non-SUPPORTED states."""
        kind, value = reading
        state = getattr(value, "state", None) if kind == "ok" else None
        reason = getattr(value, "reason_code", None) if kind == "ok" else None
        suffix = f" ({reason})" if reason else ""
        if state == "SUPPORTED":
            return self._item("PROCESS_TRACKING", Status.PASS, "process tracking is supported by the Runner of this host")
        if state == "UNSUPPORTED_PLATFORM":
            item = self._item("PROCESS_TRACKING", Status.WARN,
                              "PROCESS_TRACKING_UNSUPPORTED_PLATFORM: process tracking is not supported on this platform; "
                              "workflow Tasks and TESTER steps still run, untracked",
                              "run the Runner on a supported host for features that require tracked commands")
        elif state == "UNAVAILABLE":
            item = self._item("PROCESS_TRACKING", Status.FAIL,
                              f"PROCESS_TRACKING_UNAVAILABLE: the tracking self-test of the Runner failed{suffix}; tracked "
                              "TESTER steps fail closed", "fix the host environment and restart the Runner", True)
        else:
            why = {"unconfigured": "the capability reader is not configured",
                   "failed": "the capability could not be read"}.get(
                kind, "the capability of the current Runner incarnation cannot be confirmed")
            item = self._item("PROCESS_TRACKING", Status.UNKNOWN, f"PROCESS_TRACKING_STATUS_UNKNOWN: {why}",
                              "wait for the Runner to publish its capability, or retry the diagnostics later")
        return dataclasses.replace(item, blocks_features=("TEST_RUN",))

    def _tracked_item(self, reading: tuple[str, object]) -> DiagnosticItem:
        """14.1: every applicable cause, merged and ordered; the status is the highest severity; normal progress is only
        information in the text and keeps PASS."""
        kind, value = reading
        if kind != "ok":
            why = ("the tracked-executions reader is not configured" if kind == "unconfigured"
                   else "the tracked executions of the project could not be read")
            return self._unknown("TRACKED_EXECUTIONS", why)
        causes = merge_causes(value.causes)
        if not causes:
            text = (f"{value.running} tracked executions running" if value.running
                    else "the project has no unresolved or anomalous tracked execution")
            return self._item("TRACKED_EXECUTIONS", Status.PASS, text)
        status = max((c.severity for c in causes), key=_SEVERITY_RANK.get)
        text = "; ".join(c.cause for c in causes)
        if value.running:
            text += f"; {value.running} tracked executions running"
        return DiagnosticItem("TRACKED_EXECUTIONS", status, self._now(), text,
                              "; ".join(dict.fromkeys(c.recommendation for c in causes)),
                              any(c.blocks_execution for c in causes), causes)

    def _lock_item(self, view: _ProjectView) -> DiagnosticItem:
        if view.write_owner_task_id is not None:
            return self._item("PROJECT_WRITE_LOCK", Status.WARN,
                              "another task currently holds this project's write lock; a new task waits for it",
                              "wait for that task to finish or cancel it")
        if view.workspace_state != "CLEAN":
            return self._item("PROJECT_WRITE_LOCK", Status.WARN,
                              f"the workspace is {view.workspace_state}: the next task asks for a human decision first",
                              "resolve the workspace state, or answer the PROJECT_DIRTY gate when it opens")
        return self._item("PROJECT_WRITE_LOCK", Status.PASS, "no task holds the write lock; the workspace is clean")
