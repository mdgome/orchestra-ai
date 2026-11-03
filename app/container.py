"""Dependency wiring (the only place concrete classes are assembled)."""
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.db import Database, create_engine
from app.infra.command_executor import CommandExecutor
from app.infra.event_bus import EventBus
from app.infra.workspace_guard import WorkspaceGuard
from app.providers.claude_cli import ClaudeCliProvider
from app.providers.codex_cli import CodexCliProvider
from app.providers.preflight import PreflightReport
from app.services.human_gate_service import HumanGateService
from app.services.lease_service import LeaseService
from app.services.project_lock_service import ProjectLockService
from app.services.project_recovery_effect import ProjectDirtyRecoveryEffect
from app.services.project_service import ProjectService
from app.services.recovery_effects import RecoveryEffects
from app.services.runner import Runner, new_runner_id
from app.services.session_service import SessionService
from app.services.step_runner import StepRunner
from app.services.task_finalizer import TaskFinalizer
from app.services.task_service import TaskService
from app.services.test_policy_service import TestPolicyService
from app.services.workflow_service import WorkflowService
from app.validation import policies as P


@dataclass
class Container:
    settings: Settings
    db: Database
    guard: WorkspaceGuard
    executor: CommandExecutor
    events: EventBus
    tasks: TaskService
    gates: HumanGateService
    runner: Runner
    providers: dict
    preflight: PreflightReport | None = field(default=None)
    runner_active: bool = False
    projects: ProjectService | None = None


def build_container(settings: Settings, providers: dict | None = None, db: Database | None = None) -> Container:
    root = Path(settings.workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    database = db or Database(create_engine(settings.database_url, settings.effective_db_pool_size,
                                          settings.db_max_overflow))
    guard = WorkspaceGuard(root)
    executor = CommandExecutor(guard, max_concurrent=settings.max_concurrent_cli_processes)
    events = EventBus()
    providers = providers or {"CLAUDE": ClaudeCliProvider(executor, settings.claude_bin),
                              "CODEX": CodexCliProvider(executor, settings.codex_bin)}
    leases = LeaseService(database, new_runner_id(), ttl_seconds=settings.lease_ttl_seconds)
    sessions = SessionService()
    steps = StepRunner(database, leases, sessions, providers, events)
    # assembly order: repositories -> project_terminal (stateless) -> locks -> finalizer -> effects -> gates -> workflow
    locks = ProjectLockService(database, leases)
    finalizer = TaskFinalizer(database, locks)
    effects = RecoveryEffects({P.RECOVERY_REASON_PROJECT_DIRTY: ProjectDirtyRecoveryEffect(locks)})
    gates = HumanGateService(database, effects=effects, measure=locks.measure_for_task)
    workflow = WorkflowService(database, leases, steps, gates, TestPolicyService(executor), guard, sessions, events,
                               locks=locks, finalizer=finalizer)
    runner = Runner(database, leases, workflow, gates, list(providers.values()),
                    advisory_key=settings.runner_advisory_lock_key, claim_timeout_seconds=settings.claim_timeout_seconds,
                    max_concurrent_tasks=settings.max_concurrent_tasks,
                    max_tasks_per_project=settings.max_tasks_per_project,
                    shutdown_grace_seconds=settings.runner_shutdown_grace_seconds)
    projects = ProjectService(database, executor, clone_root=Path(settings.project_clone_root),
                              protected=(root, Path(__file__).resolve().parents[1]))
    return Container(settings, database, guard, executor, events, TaskService(database, guard), gates, runner, providers,
                     projects=projects)
