import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update

from app.models.states import CliOutcome
from app.models.tables import (
    AgentSession, Approval, Conversation, DesignDecision, Execution, Message, Task, TaskStep,
)
from app.providers.base import ProviderResult
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.postgres_session_store import PostgresSessionStore
from app.services.session_service import PreparedSession, SessionConflict, SessionService
from app.validation.exceptions import Conflict, FailClosed, ValidationFailed
from tests.harness import Harness
from tests.helpers import make_task


# ---- SessionStore: CAS, rollback, relationship -------------------------------------------------------------------
async def new_session(db, task, role="DEVELOPER", provider="CLAUDE"):
    async with db.transaction() as s:
        return await PostgresSessionStore(s).create_agent_session(
            conversation_id=task.conversation_id, task_id=task.id, role=role, provider=provider, expires_at=None)


async def test_agent_session_starts_without_a_native_id(db):
    task = await make_task(db)
    rec = await new_session(db, task)
    assert rec.native_session_id is None and rec.status == "ACTIVE" and rec.version == 0


async def test_version_cas_allows_exactly_one_of_two_concurrent_writers(db):
    task = await make_task(db)
    rec = await new_session(db, task)

    async def write(native):
        async with db.transaction() as s:
            return await PostgresSessionStore(s).upsert_native_session_if_version(rec.id, rec.version, native, "ACTIVE")
    results = await asyncio.gather(write("native-A"), write("native-B"), write("native-C"))
    assert results.count(True) == 1
    async with db.transaction() as s:
        row = await PostgresSessionStore(s).get_agent_session(task.id, "DEVELOPER")
    assert row.version == 1 and row.native_session_id in ("native-A", "native-B", "native-C")


async def test_a_delayed_response_cannot_overwrite_a_newer_session_id(db):
    task = await make_task(db)
    rec = await new_session(db, task)
    svc = SessionService()
    old_prepared = PreparedSession(rec, None)                    # what the slow, old CLI call started from
    async with db.transaction() as s:                            # meanwhile a newer session was registered
        assert await PostgresSessionStore(s).upsert_native_session_if_version(rec.id, rec.version, "newer-id", "ACTIVE")
    late = ProviderResult(outcome=CliOutcome.SUCCESS, native_session_id="stale-old-id")
    with pytest.raises(SessionConflict):
        async with db.transaction() as s:
            await svc.record_result(s, PostgresSessionStore(s), await _task(db, task), "DEVELOPER", old_prepared, late)
    async with db.transaction() as s:
        assert (await PostgresSessionStore(s).get_agent_session(task.id, "DEVELOPER")).native_session_id == "newer-id"


async def _task(db, task):
    async with db.transaction() as s:
        return (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()


async def test_session_update_is_rolled_back_with_the_failing_workflow_transaction(db):
    task = await make_task(db)
    rec = await new_session(db, task)
    svc = SessionService()
    ok = ProviderResult(outcome=CliOutcome.SUCCESS, native_session_id="fresh-id")
    with pytest.raises(RuntimeError):
        async with db.transaction() as s:
            await svc.record_result(s, PostgresSessionStore(s), await _task(db, task), "DEVELOPER",
                                    PreparedSession(rec, None), ok)
            raise RuntimeError("the workflow transaction fails after the session was touched")
    async with db.transaction() as s:
        row = await PostgresSessionStore(s).get_agent_session(task.id, "DEVELOPER")
    assert row.native_session_id is None and row.version == 0       # SessionStore never committed on its own
    # and the audit event rolled back with it
    async with db.transaction() as s:
        n = (await s.execute(text("select count(*) from state_transitions where kind='SESSION_EVENT'"))).scalar()
    assert n == 0


async def test_db_constraints_protect_sessions(db):
    task = await make_task(db)
    await new_session(db, task)
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):          # UNIQUE(task_id, agent_role)
        await new_session(db, task)
    with pytest.raises(IntegrityError):          # CHECK on role
        async with db.transaction() as s:
            s.add(AgentSession(id=uuid.uuid4(), conversation_id=task.conversation_id, task_id=task.id,
                               agent_role="REVIEWER", provider="CODEX", status="ACTIVE", version=0))
    with pytest.raises(IntegrityError):          # FK to tasks
        async with db.transaction() as s:
            s.add(AgentSession(id=uuid.uuid4(), conversation_id=task.conversation_id, task_id=uuid.uuid4(),
                               agent_role="PLANNER", provider="CLAUDE", status="ACTIVE", version=0))


async def test_session_is_rejected_for_a_task_of_another_conversation(db):
    task = await make_task(db)
    other = await make_task(db)                    # different conversation
    svc = SessionService()
    async with db.transaction() as s:
        store = PostgresSessionStore(s)
        await store.create_agent_session(conversation_id=other.conversation_id, task_id=task.id, role="DEVELOPER",
                                         provider="CLAUDE", expires_at=None)   # inconsistent row (FK alone allows it)
    with pytest.raises(FailClosed):
        async with db.transaction() as s:
            await svc.prepare(PostgresSessionStore(s), await _task(db, task), "DEVELOPER")


async def test_provider_change_never_reuses_a_native_id(db):
    task = await make_task(db)
    svc = SessionService()
    async with db.transaction() as s:
        s.add(AgentSession(id=uuid.uuid4(), conversation_id=task.conversation_id, task_id=task.id,
                           agent_role="DEVELOPER", provider="CODEX", native_session_id="x", status="ACTIVE", version=0))
    with pytest.raises(FailClosed):
        async with db.transaction() as s:
            await svc.prepare(PostgresSessionStore(s), await _task(db, task), "DEVELOPER")


@pytest.mark.parametrize("session_status,conv_status,expect", [
    ("ACTIVE", "ACTIVE", "resume"), ("INVALID", "ACTIVE", "fresh"), ("CLOSED", "ACTIVE", "blocked"),
    ("ACTIVE", "CLOSED", "blocked"), ("ACTIVE", "EXPIRED", "blocked")])
async def test_resume_is_allowed_only_for_active_sessions_of_active_conversations(db, session_status, conv_status, expect):
    task = await make_task(db)
    async with db.transaction() as s:
        await s.execute(update(Conversation).where(Conversation.id == task.conversation_id).values(status=conv_status))
        s.add(AgentSession(id=uuid.uuid4(), conversation_id=task.conversation_id, task_id=task.id,
                           agent_role="DEVELOPER", provider="CLAUDE", native_session_id="n1", status=session_status,
                           version=0))
    svc = SessionService()
    if expect == "blocked":
        with pytest.raises(FailClosed):
            async with db.transaction() as s:
                await svc.prepare(PostgresSessionStore(s), await _task(db, task), "DEVELOPER")
        return
    async with db.transaction() as s:
        prepared = await svc.prepare(PostgresSessionStore(s), await _task(db, task), "DEVELOPER")
    assert (prepared.resume_id == "n1") == (expect == "resume")


async def test_expired_agent_session_or_conversation_blocks_resume(db):
    task = await make_task(db)
    past = datetime.now(UTC) - timedelta(minutes=1)
    async with db.transaction() as s:
        s.add(AgentSession(id=uuid.uuid4(), conversation_id=task.conversation_id, task_id=task.id,
                           agent_role="DEVELOPER", provider="CLAUDE", native_session_id="n1", status="ACTIVE",
                           expires_at=past, version=0))
    svc = SessionService()
    async with db.transaction() as s:
        prepared = await svc.prepare(PostgresSessionStore(s), await _task(db, task), "DEVELOPER")
    assert prepared.resume_id is None                                   # DB expiry != CLI session existence: no resume
    async with db.transaction() as s:
        await s.execute(update(Conversation).where(Conversation.id == task.conversation_id).values(expires_at=past))
    with pytest.raises(FailClosed):
        async with db.transaction() as s:
            await svc.prepare(PostgresSessionStore(s), await _task(db, task), "DEVELOPER")


# ---- fail-closed behaviour in the workflow ---------------------------------------------------------------------
async def test_closed_conversation_blocks_execution_and_message_creation(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    await h.tasks.request_execution(task.id)
    await h.tasks.close_conversation(task.conversation_id)
    assert await h.run_until_idle() == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "FAIL_CLOSED"
    assert h.claude.calls == [] and h.codex.calls == []
    with pytest.raises(Conflict):
        await h.tasks.add_message(task.conversation_id, "USER", "hello")


async def test_db_outage_prevents_any_cli_from_running(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()

    class BrokenDb:
        def transaction(self):
            raise ConnectionError("database unavailable")
    h.steps.db = BrokenDb()
    with pytest.raises(ConnectionError):
        await h.steps.begin(None, task.id, "PLAN", "PLANNER", "x", tmp_path)
    assert h.claude.calls == [] and h.codex.calls == []


# ---- messages: separate from session metadata, retention -----------------------------------------------------------
async def test_message_is_persisted_and_conversation_holds_no_content(db, tmp_path):
    h = Harness(db, tmp_path)
    conv = await h.tasks.create_conversation()
    msg = await h.tasks.add_message(conv.id, "USER", "please build a calculator")
    assert (await h.tasks.list_messages(conv.id))[0].id == msg.id
    cols = {c.name for c in Conversation.__table__.columns}
    assert "content" not in cols and not any("message" in c for c in cols)     # metadata only


async def test_message_creation_is_blocked_after_close_or_expiry(db, tmp_path):
    h = Harness(db, tmp_path)
    conv = await h.tasks.create_conversation(ttl_seconds=3600)
    async with db.transaction() as s:
        await s.execute(update(Conversation).where(Conversation.id == conv.id)
                        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    with pytest.raises(Conflict):
        await h.tasks.add_message(conv.id, "USER", "late")
    closed = await h.tasks.create_conversation()
    await h.tasks.close_conversation(closed.id)
    with pytest.raises(Conflict):
        await h.tasks.add_message(closed.id, "USER", "late")
    with pytest.raises(ValidationFailed):
        await h.tasks.add_message(closed.id, "ROBOT", "x")


async def test_message_retention_deletes_messages_only_and_keeps_task_history(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    await h.start(task.id)                                         # full run: steps, executions, approvals-free
    conv_id = task.conversation_id
    await h.tasks.add_message(conv_id, "USER", "build it")         # conversation still ACTIVE here
    await h.tasks.close_conversation(conv_id)
    before = {}
    async with db.transaction() as s:
        for model in (Task, TaskStep, Execution, Approval, DesignDecision):
            before[model.__name__] = (await s.execute(select(func.count()).select_from(model))).scalar()
    async with db.transaction() as s:
        old = datetime.now(UTC) + timedelta(days=1)                # cutoff in the future: everything ended before it
        deleted = await ConversationRepository(s).delete_messages_of_ended_conversations(old)
    assert deleted == 1
    async with db.transaction() as s:
        assert (await s.execute(select(func.count()).select_from(Message))).scalar() == 0
        for model in (Task, TaskStep, Execution, Approval, DesignDecision):
            assert (await s.execute(select(func.count()).select_from(model))).scalar() == before[model.__name__]


async def test_active_conversation_messages_are_not_retained_away(db, tmp_path):
    h = Harness(db, tmp_path)
    conv = await h.tasks.create_conversation()
    await h.tasks.add_message(conv.id, "USER", "keep me")
    async with db.transaction() as s:
        deleted = await ConversationRepository(s).delete_messages_of_ended_conversations(datetime.now(UTC) + timedelta(days=9))
    assert deleted == 0


async def test_shared_context_can_be_rebuilt_without_any_message_content(db, tmp_path):
    """A new native session must be creatable from tasks/task_steps/design_decisions alone."""
    h = Harness(db, tmp_path)
    task = await h.new_task(requirements="implement add(a, b)")
    await h.start(task.id)
    async with db.transaction() as s:
        await s.execute(text("delete from messages"))
    t = await h.task(task.id)
    steps = await h.steps_of(task.id)
    plan = next(s for s in steps if s.step_key == "PLAN").output
    test = next(s for s in steps if s.step_key == "TEST").output
    assert t.requirements == "implement add(a, b)" and plan["acceptance_criteria"]
    assert test["test_result"]["passed"] and t.decisions_hash == ""
