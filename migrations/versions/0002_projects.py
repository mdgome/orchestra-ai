"""projects, project ownership of tasks, execution request fencing/backoff

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('projects',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('workspace_path', sa.Text(), nullable=False),
    sa.Column('workspace_key', sa.Text(), nullable=False),
    sa.Column('repository_url', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='ACTIVE', nullable=False),
    sa.Column('test_policy', sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('write_owner_task_id', sa.UUID(), nullable=True),
    sa.Column('write_epoch', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('baseline_fingerprint', sa.Text(), nullable=True),
    sa.Column('write_baseline_fingerprint', sa.Text(), nullable=True),
    sa.Column('workspace_state', sa.String(length=24), server_default='CLEAN', nullable=False),
    sa.Column('row_version', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ACTIVE','ARCHIVED')", name='ck_projects_status'),
    sa.CheckConstraint("workspace_state IN ('CLEAN','DIRTY_UNRESOLVED')", name='ck_projects_workspace_state'),
    sa.CheckConstraint('(write_owner_task_id IS NULL) = (write_baseline_fingerprint IS NULL)',
                       name='ck_projects_write_owner_baseline'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_key', name='uq_projects_workspace_key')
    )
    op.add_column('tasks', sa.Column('project_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_tasks_project_id'), 'tasks', ['project_id'], unique=False)
    op.create_foreign_key('fk_tasks_project_id', 'tasks', 'projects', ['project_id'], ['id'], ondelete='RESTRICT')
    # tasks <-> projects reference each other: the owner FK is added once both tables exist.
    op.create_foreign_key('fk_projects_write_owner_task_id', 'projects', 'tasks', ['write_owner_task_id'], ['id'])

    op.add_column('execution_requests', sa.Column('claim_token', sa.UUID(), nullable=True))
    op.add_column('execution_requests', sa.Column('not_before', sa.DateTime(timezone=True), nullable=True))
    op.add_column('execution_requests', sa.Column('defer_count', sa.Integer(), server_default='0', nullable=False))

    op.create_index('uq_approvals_project_dirty_pending', 'approvals', ['task_id'], unique=True,
                    postgresql_where=sa.text("status = 'PENDING' AND (target ->> 'reason_code') = 'PROJECT_DIRTY'"))


def downgrade() -> None:
    op.drop_index('uq_approvals_project_dirty_pending', table_name='approvals',
                  postgresql_where=sa.text("status = 'PENDING' AND (target ->> 'reason_code') = 'PROJECT_DIRTY'"))
    op.drop_column('execution_requests', 'defer_count')
    op.drop_column('execution_requests', 'not_before')
    op.drop_column('execution_requests', 'claim_token')
    op.drop_constraint('fk_projects_write_owner_task_id', 'projects', type_='foreignkey')
    op.drop_constraint('fk_tasks_project_id', 'tasks', type_='foreignkey')
    op.drop_index(op.f('ix_tasks_project_id'), table_name='tasks')
    op.drop_column('tasks', 'project_id')
    op.drop_table('projects')
