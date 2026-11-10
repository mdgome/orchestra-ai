"""task baselines: the workspace manifest a task started from (hashes only, no file content)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04 12:00:00.000000

A row per task, written in the SAME transaction that makes the task the project's write owner. Existing owners (tasks
that already hold the lock when this migration runs) simply have no row: the lock service gives them an explicit
UNAVAILABLE row the first time they re-enter, it never invents one.
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('task_baselines',
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('origin', sa.String(length=24), nullable=False),
    sa.Column('proof_epoch', sa.BigInteger(), nullable=True),
    sa.Column('acquired_epoch', sa.BigInteger(), nullable=True),
    sa.Column('recorded_epoch', sa.BigInteger(), nullable=False),
    sa.Column('proof_fingerprint', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=True),
    sa.Column('head_kind', sa.String(length=8), nullable=True),
    sa.Column('head_sha', sa.String(length=64), nullable=True),
    sa.Column('manifest', sa.LargeBinary(), nullable=True),
    sa.Column('manifest_version', sa.String(length=32), nullable=True),
    sa.Column('uncompressed_bytes', sa.Integer(), nullable=True),
    sa.Column('manifest_sha256', sa.String(length=64), nullable=True),
    sa.Column('file_count', sa.Integer(), nullable=True),
    sa.Column('dirty_paths', sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('unavailable_reason', sa.String(length=48), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('AVAILABLE','UNAVAILABLE')", name='ck_task_baselines_status'),
    sa.CheckConstraint("origin IN ('ACQUISITION','REENTRY_RECOVERY')", name='ck_task_baselines_origin'),
    sa.CheckConstraint("head_kind IS NULL OR head_kind IN ('COMMIT','UNBORN')", name='ck_task_baselines_head_kind'),
    sa.CheckConstraint("(proof_epoch IS NULL OR proof_epoch >= 0) AND (acquired_epoch IS NULL OR acquired_epoch >= 1) "
                       "AND recorded_epoch >= 1", name='ck_task_baselines_epochs'),
    sa.CheckConstraint("proof_fingerprint ~ '^WORKSPACE_FINGERPRINT_V[0-9]+:[0-9a-f]{64}$'", name='ck_task_baselines_proof_fp'),
    sa.CheckConstraint(
        "status <> 'AVAILABLE' OR ((mode IN ('GIT','CONTENT') AND manifest IS NOT NULL AND manifest_version IS NOT NULL "
        "AND manifest_sha256 IS NOT NULL AND length(manifest_sha256) = 64 AND uncompressed_bytes IS NOT NULL "
        "AND uncompressed_bytes >= 0 AND file_count IS NOT NULL AND file_count >= 0 AND dirty_paths IS NOT NULL "
        "AND jsonb_typeof(dirty_paths) = 'array' "
        "AND ((mode = 'GIT' AND head_kind = 'COMMIT' AND head_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$') "
        "OR (mode = 'GIT' AND head_kind = 'UNBORN' AND head_sha IS NULL) "
        "OR (mode = 'CONTENT' AND head_kind IS NULL AND head_sha IS NULL)) "
        "AND proof_epoch IS NOT NULL AND acquired_epoch IS NOT NULL AND acquired_epoch >= proof_epoch "
        "AND recorded_epoch >= acquired_epoch) IS TRUE)", name='ck_task_baselines_available'),
    sa.CheckConstraint(
        "origin <> 'ACQUISITION' OR ((proof_epoch IS NOT NULL AND acquired_epoch = proof_epoch + 1 "
        "AND recorded_epoch = acquired_epoch) IS TRUE)", name='ck_task_baselines_acquisition'),
    sa.CheckConstraint(
        "status <> 'UNAVAILABLE' OR (unavailable_reason IS NOT NULL AND manifest IS NULL AND manifest_version IS NULL "
        "AND manifest_sha256 IS NULL AND uncompressed_bytes IS NULL AND file_count IS NULL AND mode IS NULL "
        "AND head_kind IS NULL AND head_sha IS NULL AND dirty_paths IS NULL)", name='ck_task_baselines_unavailable'),
    sa.CheckConstraint("status = 'UNAVAILABLE' OR unavailable_reason IS NULL", name='ck_task_baselines_reason_only_unavailable'),
    sa.CheckConstraint(
        "unavailable_reason IS DISTINCT FROM 'BASELINE_MISSING_AT_REENTRY' OR (origin = 'REENTRY_RECOVERY' "
        "AND status = 'UNAVAILABLE' AND acquired_epoch IS NULL AND proof_epoch IS NULL)",
        name='ck_task_baselines_reentry_missing'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_task_baselines_task_id'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_task_baselines_project_id'),
    sa.PrimaryKeyConstraint('task_id')
    )


def downgrade() -> None:
    op.drop_table('task_baselines')
