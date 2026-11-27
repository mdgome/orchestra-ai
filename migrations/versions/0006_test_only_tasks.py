"""test-only tasks (E1): tasks.mode, idempotency_key and request_fingerprint

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-27 12:00:00.000000

A Project Test Run is an ordinary Task with mode = 'TEST_ONLY' (D4). Every existing row is a WORKFLOW Task, so nothing
about an existing Task changes: the column default is 'WORKFLOW' and the new columns are NULL.

  * ck_tasks_mode                 - the two values
  * ck_tasks_test_only_project    - a TEST_ONLY Task always belongs to a project
  * ck_tasks_idempotency_test_only- only a TEST_ONLY Task can carry an idempotency key
  * uq_tasks_project_idempotency_key - one Task per (project, key); partial, so NULL keys never collide

downgrade refuses while a TEST_ONLY Task exists: dropping the column would silently turn run history into ordinary
Task history.
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('mode', sa.String(length=16), server_default='WORKFLOW', nullable=False))
    op.add_column('tasks', sa.Column('idempotency_key', sa.String(length=128), nullable=True))
    op.add_column('tasks', sa.Column('request_fingerprint', sa.String(length=64), nullable=True))
    op.create_check_constraint('ck_tasks_mode', 'tasks', "mode IN ('WORKFLOW','TEST_ONLY')")
    op.create_check_constraint('ck_tasks_test_only_project', 'tasks',
                               "(mode <> 'TEST_ONLY' OR project_id IS NOT NULL) IS TRUE")
    op.create_check_constraint('ck_tasks_idempotency_test_only', 'tasks',
                               "(idempotency_key IS NULL OR mode = 'TEST_ONLY') IS TRUE")
    op.create_index('uq_tasks_project_idempotency_key', 'tasks', ['project_id', 'idempotency_key'], unique=True,
                    postgresql_where=sa.text('idempotency_key IS NOT NULL'))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM tasks WHERE mode = 'TEST_ONLY' LIMIT 1")).first() is not None:
        raise RuntimeError("refusing to downgrade: TEST_ONLY tasks exist (their history would be lost silently)")
    op.drop_index('uq_tasks_project_idempotency_key', table_name='tasks')
    op.drop_constraint('ck_tasks_idempotency_test_only', 'tasks', type_='check')
    op.drop_constraint('ck_tasks_test_only_project', 'tasks', type_='check')
    op.drop_constraint('ck_tasks_mode', 'tasks', type_='check')
    op.drop_column('tasks', 'request_fingerprint')
    op.drop_column('tasks', 'idempotency_key')
    op.drop_column('tasks', 'mode')
