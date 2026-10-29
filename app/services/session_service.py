"""Native session bookkeeping on top of SessionStore. PostgreSQL is the source of truth.

Everything here runs inside the caller's transaction. Native ids are internal: audit events carry only an
irreversible fingerprint, never the id itself.
"""
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.tables import Task
from app.providers.base import ProviderResult, session_fingerprint
from app.repositories.session_store import AgentSessionRecord, SessionStore
from app.repositories.task_repository import TaskRepository
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import Conflict, FailClosed


class SessionConflict(Conflict):
    """A native session id could not be registered (CAS lost / ownership changed)."""
    code = "SESSION_CONFLICT"


@dataclass(frozen=True)
class PreparedSession:
    record: AgentSessionRecord
    resume_id: str | None       # None -> start a new native session


class SessionService:
    def __init__(self, clock=lambda: datetime.now(UTC)):
        self._now = clock

    async def prepare(self, store: SessionStore, task: Task, role: str, *, allow_resume: bool = True) -> PreparedSession:
        now = self._now()
        provider = P.ROLE_PROVIDER[role]
        conv = await store.get_conversation(task.conversation_id)
        if conv is None:
            raise FailClosed("conversation not found")
        if conv.status != "ACTIVE":
            raise FailClosed("conversation is not ACTIVE; execution and resume are blocked", {"status": conv.status})
        V.validate_not_expired(conv.expires_at, now, "conversation")

        record = await store.get_agent_session(task.id, role)
        if record is None:
            V.validate_session_binding(role=role, provider=provider, task_conversation_id=task.conversation_id)
            record = await store.create_agent_session(
                conversation_id=task.conversation_id, task_id=task.id, role=role, provider=provider, expires_at=None)
        # Cross-conversation / provider reuse is forbidden even if a row somehow exists.
        V.validate_session_binding(role=role, provider=provider, task_conversation_id=task.conversation_id,
                                   session_conversation_id=record.conversation_id, existing_provider=record.provider)
        if record.status == "CLOSED":
            raise FailClosed("agent session is CLOSED")

        resumable = (record.status == "ACTIVE" and record.native_session_id is not None and allow_resume
                     and (record.expires_at is None or record.expires_at > now))
        return PreparedSession(record, record.native_session_id if resumable else None)

    async def record_result(self, session, store: SessionStore, task: Task, role: str, prepared: PreparedSession,
                            result: ProviderResult) -> str | None:
        """Persist the outcome for the session (CAS on version). Returns the audit action, if any.

        Callers must have verified lease ownership in this same transaction first."""
        from app.models.states import CliOutcome

        rec = prepared.record
        action: str | None = None
        if result.outcome is CliOutcome.SESSION_MISSING and prepared.resume_id:
            if not await store.set_status_if_version(rec.id, rec.version, "INVALID"):
                raise SessionConflict("agent session changed concurrently")
            action = "INVALIDATED"
        elif result.outcome is CliOutcome.SUCCESS and result.native_session_id:
            if rec.native_session_id == result.native_session_id and rec.status == "ACTIVE":
                action = "RESUMED" if prepared.resume_id else None
                if action is None and rec.native_session_id is None:
                    action = "CREATED"
                # keep id; touch via CAS so a stale writer cannot overwrite a newer registration
                if not await store.upsert_native_session_if_version(rec.id, rec.version, rec.native_session_id, "ACTIVE"):
                    raise SessionConflict("agent session changed concurrently")
            else:
                replaced = rec.native_session_id is not None
                if not await store.upsert_native_session_if_version(rec.id, rec.version, result.native_session_id, "ACTIVE"):
                    raise SessionConflict("agent session changed concurrently")
                action = "REPLACED" if replaced else "CREATED"
        if action:
            fp = session_fingerprint(result.native_session_id) if result.native_session_id else None
            await TaskRepository(session).add_transition(
                task.id, task.status, task.status, f"session_{action.lower()}",
                {"action": action, "role": role, "provider": rec.provider, "session": fp}, kind="SESSION_EVENT")
        return action

    async def close_all_for_task(self, session, task_id) -> None:
        from sqlalchemy import func, update

        from app.models.tables import AgentSession
        await session.execute(update(AgentSession).where(AgentSession.task_id == task_id,
                                                         AgentSession.status == "ACTIVE")
                              .values(status="CLOSED", version=AgentSession.version + 1, updated_at=func.now()))
