"""versioned project profiles, the active-profile pointer and the per-task profile snapshot

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-04 10:00:00.000000

Order matters because projects and project_profiles reference each other:
  upgrade:   project_profiles -> projects.profile_version + its FK -> tasks columns + CHECK + FK -> backfill of the existing
             projects (legacy v1) -> only then the immutability trigger.
  downgrade: trigger first (the table is mutable again) -> tasks FK/CHECK/columns -> projects FK/CHECK/column -> table.
"""
import hashlib
import json

from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

# The `legacy` v1 profile of the projects that existed before profiles: the original built-in architecture rules, so their
# behaviour is unchanged. A copy on purpose (a migration must not import application code that may change later);
# tests/unit/test_profile_defaults.py pins it to app.services.profile_service.LEGACY_CONTENT.
LEGACY_CONTENT = {
    "schema_version": 1,
    "description": "",
    "languages": [],
    "frameworks": [],
    "architecture_rules": [
        "Layered Architecture: Router -> Service -> Repository / Provider Adapter. Do NOT introduce DDD or a Generic Repository.",
        "REST URIs use nouns only; actions are expressed by the HTTP method (no verbs such as get/create/update/delete/execute in URIs).",
        "Controllers/routers contain no business logic and no inline validation.",
        "All input validation lives in a central validation module; do not duplicate validation.",
        "Avoid unnecessary abstractions and frameworks; prefer the simplest structure that stays maintainable and testable.",
    ],
    "coding_rules": [],
    "completion_criteria": [],
    "test_policy_ref": "project",
}


def _hash(content: dict) -> str:
    """Same algorithm as app.services.hashing.stable_hash('project_profile', content)."""
    payload = json.dumps(["project_profile", content], sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.create_table('project_profiles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version > 0', name='ck_project_profiles_version'),
    sa.CheckConstraint('length(content_hash) = 64', name='ck_project_profiles_hash'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_project_profiles_project_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'version', name='uq_project_profiles_project_version')
    )
    # active pointer: the composite FK can only name a version of THIS project (MATCH SIMPLE: NULL = no profile yet)
    op.add_column('projects', sa.Column('profile_version', sa.Integer(), nullable=True))
    op.create_check_constraint('ck_projects_profile_version', 'projects', 'profile_version IS NULL OR profile_version > 0')
    op.create_foreign_key('fk_projects_active_profile', 'projects', 'project_profiles',
                          ['id', 'profile_version'], ['project_id', 'version'])
    # task snapshot: all three NULL (no project / predates profiles) or all present and tied to the task's own project
    op.add_column('tasks', sa.Column('profile_version', sa.Integer(), nullable=True))
    op.add_column('tasks', sa.Column('profile_hash', sa.String(length=64), nullable=True))
    op.add_column('tasks', sa.Column('profile_snapshot', sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint(
        'ck_tasks_profile_all_or_none', 'tasks',
        '(profile_version IS NULL AND profile_hash IS NULL AND profile_snapshot IS NULL) OR '
        '(project_id IS NOT NULL AND profile_version IS NOT NULL AND profile_hash IS NOT NULL '
        'AND profile_snapshot IS NOT NULL AND length(profile_hash) = 64)')
    op.create_foreign_key('fk_tasks_profile', 'tasks', 'project_profiles',
                          ['project_id', 'profile_version'], ['project_id', 'version'])

    # backfill: every existing project gets legacy v1 and points at it. Existing tasks keep NULL (their rules and hashes
    # stay exactly as they were).
    bind = op.get_bind()
    content_json = json.dumps(LEGACY_CONTENT, sort_keys=True, ensure_ascii=False)
    bind.execute(sa.text(
        "INSERT INTO project_profiles (id, project_id, version, content, content_hash) "
        "SELECT gen_random_uuid(), id, 1, CAST(:content AS jsonb), :hash FROM projects"),
        {"content": content_json, "hash": _hash(LEGACY_CONTENT)})
    bind.execute(sa.text("UPDATE projects SET profile_version = 1"))

    # only now: a profile version is immutable (a change is always a new version)
    op.execute("""
        CREATE FUNCTION forbid_project_profile_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'project_profiles is immutable: add a new version instead';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_project_profiles_immutable
        BEFORE UPDATE OR DELETE ON project_profiles
        FOR EACH ROW EXECUTE FUNCTION forbid_project_profile_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_project_profiles_immutable ON project_profiles")
    op.execute("DROP FUNCTION IF EXISTS forbid_project_profile_mutation()")
    op.drop_constraint('fk_tasks_profile', 'tasks', type_='foreignkey')
    op.drop_constraint('ck_tasks_profile_all_or_none', 'tasks', type_='check')
    op.drop_column('tasks', 'profile_snapshot')
    op.drop_column('tasks', 'profile_hash')
    op.drop_column('tasks', 'profile_version')
    op.drop_constraint('fk_projects_active_profile', 'projects', type_='foreignkey')
    op.drop_constraint('ck_projects_profile_version', 'projects', type_='check')
    op.drop_column('projects', 'profile_version')
    op.drop_table('project_profiles')
