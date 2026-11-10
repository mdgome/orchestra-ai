"""Project profiles against a real PostgreSQL: versions, the active pointer, snapshots on tasks, the DB constraints, the
immutability trigger and the migration (backfill, downgrade)."""
import asyncio
import copy
import json
import uuid

import asyncpg
import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.infra.command_executor import CommandResult
from app.models.states import CliOutcome
from app.models.tables import Project, ProjectProfile, Task
from app.services import profile_service
from app.services.profile_service import LEGACY_CONTENT, NEUTRAL_CONTENT, ProfileService, hash_profile
from app.services.project_service import ProjectService
from app.services.task_service import TaskService
from app.validation.exceptions import Conflict, NotFound, ValidationFailed
from tests.conftest import alembic_config
from tests.harness import Harness


class NoClone:
    async def run(self, spec, **_kw):                         # pragma: no cover - registration of local paths only
        return CommandResult(spec.argv, CliOutcome.ERROR, 1, "", "", 0.0)


@pytest.fixture
async def env(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    projects = ProjectService(db, NoClone(), clone_root=tmp_path / "clones", protected=(h.guard.root,))
    tasks = TaskService(db, h.guard)
    (tmp_path / "projects").mkdir()
    return db, projects, ProfileService(db), tasks, tmp_path / "projects"


async def new_project(projects, root, name="svc", **kw):
    path = root / name
    path.mkdir()
    return await projects.create_project(name=name, workspace_path=str(path), **kw)


async def profile_rows(db, project_id):
    async with db.transaction() as s:
        rows = await s.execute(select(ProjectProfile).where(ProjectProfile.project_id == project_id)
                               .order_by(ProjectProfile.version))
        return list(rows.scalars())


# --- creation ----------------------------------------------------------------------------------------------------------
async def test_a_new_project_without_a_profile_gets_the_neutral_v1_and_it_is_active(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    assert project.profile_version == 1
    [row] = await profile_rows(db, project.id)
    assert row.version == 1 and row.content == NEUTRAL_CONTENT and row.content_hash == hash_profile(NEUTRAL_CONTENT)
    assert (await profiles.active(project.id))["content"] == NEUTRAL_CONTENT


async def test_a_new_project_with_a_profile_gets_that_content_as_v1(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root, profile={"description": "Billing", "architecture_rules": ["Hexagonal"],
                                                         "completion_criteria": [{"id": "DOC", "text": "documented"}]})
    active = await profiles.active(project.id)
    assert active["version"] == 1 and active["content"]["architecture_rules"] == ["Hexagonal"]
    assert active["content"]["completion_criteria"] == [{"id": "DOC", "text": "documented"}] and active["active"]


async def test_an_invalid_profile_creates_no_project_at_all(env):
    db, projects, _profiles, _tasks, root = env
    path = root / "bad"
    path.mkdir()
    with pytest.raises(ValidationFailed):
        await projects.create_project(name="bad", workspace_path=str(path), profile={"permissions": ["Bash"]})
    async with db.transaction() as s:
        assert (await s.execute(select(Project))).scalars().all() == []
        assert (await s.execute(select(ProjectProfile))).scalars().all() == []


async def test_the_project_row_the_profile_and_the_pointer_commit_together_or_not_at_all(env, monkeypatch):
    db, projects, _profiles, _tasks, root = env

    async def boom(*_a, **_k):
        raise RuntimeError("profile insert failed")

    monkeypatch.setattr(profile_service, "create_initial", boom)
    path = root / "half"
    path.mkdir()
    with pytest.raises(RuntimeError):
        await projects.create_project(name="half", workspace_path=str(path))
    async with db.transaction() as s:
        assert (await s.execute(select(Project))).scalars().all() == []         # the project row rolled back too


# --- versions ----------------------------------------------------------------------------------------------------------
async def test_a_changed_profile_creates_and_activates_a_new_version(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    created = await profiles.add_version(project.id, {"architecture_rules": ["A"]})
    assert created["created"] is True and created["version"] == 2 and created["active"] is True
    assert (await projects.get_project(project.id)).profile_version == 2
    assert [r.version for r in await profile_rows(db, project.id)] == [1, 2]       # v1 is still there, untouched


async def test_an_identical_profile_writes_nothing_and_reports_it(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    await profiles.add_version(project.id, {"architecture_rules": ["A"]})
    same = await profiles.add_version(project.id, {"architecture_rules": ["A"], "description": ""})   # same canonical content
    assert same["created"] is False and same["version"] == 2
    assert len(await profile_rows(db, project.id)) == 2


async def test_returning_to_an_older_content_is_a_new_version_not_a_rewrite(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    await profiles.add_version(project.id, {"architecture_rules": ["A"]})
    back = await profiles.add_version(project.id, NEUTRAL_CONTENT)
    assert back["created"] is True and back["version"] == 3 and back["content_hash"] == hash_profile(NEUTRAL_CONTENT)


async def test_concurrent_different_versions_are_serialised_without_gaps_or_duplicates(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    results = await asyncio.gather(*(profiles.add_version(project.id, {"description": f"d{i}"}) for i in range(8)))
    assert sorted(r["version"] for r in results) == list(range(2, 10)) and all(r["created"] for r in results)
    assert [r.version for r in await profile_rows(db, project.id)] == list(range(1, 10))
    final = (await projects.get_project(project.id)).profile_version
    assert final == 9 and (await profiles.active(project.id))["version"] == 9


async def test_concurrent_identical_requests_create_exactly_one_version(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    results = await asyncio.gather(*(profiles.add_version(project.id, {"description": "same"}) for _ in range(6)))
    assert sum(r["created"] for r in results) == 1 and {r["version"] for r in results} == {2}
    assert len(await profile_rows(db, project.id)) == 2


async def test_an_archived_project_takes_no_new_version_but_stays_readable(env):
    db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    await projects.update_project(project.id, {"status": "ARCHIVED"})
    with pytest.raises(Conflict):
        await profiles.add_version(project.id, {"description": "late"})
    assert (await profiles.active(project.id))["version"] == 1
    assert (await profiles.versions(project.id, 10, 0))["total"] == 1                # archive keeps every profile row
    assert len(await profile_rows(db, project.id)) == 1


async def test_unknown_project_and_invalid_content(env):
    _db, projects, profiles, _tasks, root = env
    with pytest.raises(NotFound):
        await profiles.add_version(uuid.uuid4(), {})
    with pytest.raises(NotFound):
        await profiles.active(uuid.uuid4())
    project = await new_project(projects, root)
    with pytest.raises(ValidationFailed):
        await profiles.add_version(project.id, {"model": "x"})
    assert len(await profile_rows(_db, project.id)) == 1


async def test_versions_are_listed_newest_first_with_the_active_one_marked(env):
    _db, projects, profiles, _tasks, root = env
    project = await new_project(projects, root)
    for i in range(3):
        await profiles.add_version(project.id, {"description": f"d{i}"})
    page = await profiles.versions(project.id, 2, 0)
    assert [v["version"] for v in page["items"]] == [4, 3] and page["total"] == 4 and page["items"][0]["active"] is True
    assert [v["version"] for v in (await profiles.versions(project.id, 2, 2))["items"]] == [2, 1]


# --- tasks copy the profile ------------------------------------------------------------------------------------------------
async def test_a_task_copies_the_active_version_and_a_later_change_does_not_touch_it(env):
    db, projects, profiles, tasks, root = env
    project = await new_project(projects, root, profile={"architecture_rules": ["old rule"]})
    first = await tasks.create_task(requirements="implement something useful", project_id=project.id)
    assert (first.profile_version, first.profile_snapshot["architecture_rules"]) == (1, ["old rule"])
    snapshot = copy.deepcopy(first.profile_snapshot)
    await profiles.add_version(project.id, {"architecture_rules": ["new rule"]})
    second = await tasks.create_task(requirements="implement something else", project_id=project.id)
    assert (second.profile_version, second.profile_snapshot["architecture_rules"]) == (2, ["new rule"])
    again = await tasks.get_task(first.id)
    assert again.profile_version == 1 and again.profile_snapshot == snapshot and again.profile_hash == first.profile_hash
    assert second.profile_hash != first.profile_hash


async def test_a_task_without_a_project_has_no_profile_and_keeps_the_builtin_rules(env):
    _db, _projects, _profiles, tasks, _root = env
    task = await tasks.create_task(requirements="implement something useful")
    assert (task.profile_version, task.profile_hash, task.profile_snapshot) == (None, None, None)


async def test_task_creation_and_a_profile_change_race_to_exactly_one_consistent_version(env):
    db, projects, profiles, tasks, root = env
    project = await new_project(projects, root)

    async def create(i):
        return await tasks.create_task(requirements=f"implement feature number {i}", project_id=project.id)

    changes = [profiles.add_version(project.id, {"description": f"d{i}"}) for i in range(4)]
    made = await asyncio.gather(*(create(i) for i in range(6)), *changes)
    for task in made[:6]:
        row = (await profile_rows(db, project.id))[task.profile_version - 1]
        assert task.profile_hash == row.content_hash and task.profile_snapshot == row.content   # one whole version


# --- database constraints ------------------------------------------------------------------------------------------------
async def _insert_task(s, **values):
    cid, tid = uuid.uuid4(), uuid.uuid4()
    await s.execute(text("insert into conversations(id,status) values (:c,'ACTIVE')"), {"c": cid})
    base = {"t": tid, "c": cid, "p": None, "v": None, "h": None, "snap": None}
    base.update(values)
    await s.execute(text(
        "insert into tasks(id,conversation_id,project_id,requirements,requirements_hash,status,limits,test_policy,"
        "test_policy_hash,plan_revision,workflow_iteration,replan_count,cli_invocations,run_seconds,decisions_hash,"
        "row_version,profile_version,profile_hash,profile_snapshot) values (:t,:c,:p,'r','h','PENDING','{}','{}','h',"
        "0,0,0,0,0,'',0,:v,:h,CAST(:snap AS jsonb))"), base)


async def test_profile_values_without_a_project_are_rejected_by_the_database(env):
    db, *_ = env
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            await _insert_task(s, p=None, v=1, h="a" * 64, snap=json.dumps(NEUTRAL_CONTENT))


async def test_a_task_profile_is_all_or_none_and_its_hash_has_the_right_length(env):
    db, projects, _profiles, _tasks, root = env
    project = await new_project(projects, root)
    good = {"p": project.id, "v": 1, "h": "a" * 64, "snap": json.dumps(NEUTRAL_CONTENT)}
    for bad in ({**good, "v": None}, {**good, "h": None}, {**good, "snap": None}, {**good, "h": "short"},
                {"p": project.id, "v": None, "h": "a" * 64, "snap": None}):
        with pytest.raises(IntegrityError):
            async with db.transaction() as s:
                await _insert_task(s, **bad)
    async with db.transaction() as s:
        await _insert_task(s, **good)                       # the one valid all-present shape is accepted
        await _insert_task(s, p=project.id)                 # all NULL with a project (predates profiles) is accepted too


async def test_a_task_can_only_point_at_a_profile_version_of_its_own_project(env):
    db, projects, profiles, _tasks, root = env
    a, b = await new_project(projects, root, "a"), await new_project(projects, root, "b")
    await profiles.add_version(b.id, {"description": "only b has v2"})
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            await _insert_task(s, p=a.id, v=2, h="a" * 64, snap=json.dumps(NEUTRAL_CONTENT))


async def test_the_active_pointer_can_only_name_a_version_of_the_same_project(env):
    db, projects, profiles, _tasks, root = env
    a, b = await new_project(projects, root, "a"), await new_project(projects, root, "b")
    await profiles.add_version(b.id, {"description": "only b has v2"})
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            await s.execute(text("update projects set profile_version = 2 where id = :a"), {"a": a.id})
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            await s.execute(text("update projects set profile_version = 0 where id = :a"), {"a": a.id})


async def test_version_numbers_hashes_and_uniqueness_are_enforced(env):
    db, projects, _profiles, _tasks, root = env
    project = await new_project(projects, root)
    for version, digest in ((1, "a" * 64), (0, "a" * 64), (-1, "a" * 64), (5, "short")):
        with pytest.raises(IntegrityError):
            async with db.transaction() as s:
                await s.execute(text("insert into project_profiles(id,project_id,version,content,content_hash) "
                                     "values (:i,:p,:v,'{}',:h)"),
                                {"i": uuid.uuid4(), "p": project.id, "v": version, "h": digest})


async def test_profile_rows_are_immutable(env):
    db, projects, _profiles, _tasks, root = env
    project = await new_project(projects, root)
    with pytest.raises(DBAPIError):
        async with db.transaction() as s:
            await s.execute(text("update project_profiles set content = '{}' where project_id = :p"), {"p": project.id})
    with pytest.raises(DBAPIError):
        async with db.transaction() as s:
            await s.execute(text("delete from project_profiles where project_id = :p"), {"p": project.id})
    assert len(await profile_rows(db, project.id)) == 1


# --- the migration ---------------------------------------------------------------------------------------------------------
async def test_the_migration_backfills_legacy_v1_for_existing_projects_and_downgrades_cleanly(pg):
    name = f"mig_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg.sync_uri)
    await admin.execute(f"CREATE DATABASE {name}")
    await admin.close()
    cfg = alembic_config(pg.async_url(name))
    url = pg.sync_uri.rsplit("/", 1)[0] + "/" + name
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0002")
        conn = await asyncpg.connect(url)
        project_ids = [uuid.uuid4(), uuid.uuid4()]
        for i, pid in enumerate(project_ids):
            await conn.execute("insert into projects(id,name,workspace_path,workspace_key) values ($1,$2,$3,$4)",
                               pid, f"p{i}", f"/w/{i}", f"/w/{i}")
        cid, tid = uuid.uuid4(), uuid.uuid4()
        await conn.execute("insert into conversations(id,status) values ($1,'ACTIVE')", cid)
        await conn.execute(
            "insert into tasks(id,conversation_id,project_id,requirements,requirements_hash,status,limits,test_policy,"
            "test_policy_hash,plan_revision,workflow_iteration,replan_count,cli_invocations,run_seconds,decisions_hash,"
            "row_version) values ($1,$2,$3,'r','h','PLANNING','{}','{}','h',0,0,0,0,0,'',0)", tid, cid, project_ids[0])
        await conn.close()

        await asyncio.to_thread(command.upgrade, cfg, "head")
        conn = await asyncpg.connect(url)
        projects = await conn.fetch("select id, profile_version from projects")
        assert {r["id"]: r["profile_version"] for r in projects} == {pid: 1 for pid in project_ids}
        rows = await conn.fetch("select project_id, version, content, content_hash from project_profiles")
        assert len(rows) == 2
        for row in rows:
            assert json.loads(row["content"]) == LEGACY_CONTENT and row["content_hash"] == hash_profile(LEGACY_CONTENT)
        task = await conn.fetchrow("select profile_version, profile_hash, profile_snapshot from tasks where id = $1", tid)
        assert tuple(task) == (None, None, None)                       # an in-flight task keeps its original rules/hashes
        with pytest.raises(asyncpg.PostgresError):                       # the trigger is installed AFTER the backfill
            await conn.execute("update project_profiles set content = '{}'")
        await conn.close()

        await asyncio.to_thread(command.downgrade, cfg, "0002")
        conn = await asyncpg.connect(url)
        assert await conn.fetchval("select to_regclass('public.project_profiles')") is None
        columns = {r["column_name"] for r in await conn.fetch(
            "select column_name from information_schema.columns where table_name in ('projects','tasks')")}
        assert not {"profile_version", "profile_hash", "profile_snapshot"} & columns
        assert await conn.fetchval("select count(*) from projects") == 2           # no project data lost
        await conn.close()
        await asyncio.to_thread(command.upgrade, cfg, "head")                       # and it can go up again
    finally:
        admin = await asyncpg.connect(pg.sync_uri)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()
