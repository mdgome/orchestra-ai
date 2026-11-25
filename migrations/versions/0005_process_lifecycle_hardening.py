"""process lifecycle hardening (B2 phase 1): runtime_capabilities, process_risk_requests and the tracked-row indexes

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24 12:00:00.000000

Additive only, and no column for execution facts (decision H2: `executions.detail` carries them):
  * runtime_capabilities    - the process-tracking capability shared with API-only instances (design 6.2)
  * process_risk_requests   - operator risk-acceptance / recheck requests (design 7.5 item 4); the request id is the approval id
  * ix_executions_tracked_unresolved / ix_executions_tracked_anomaly - partial indexes on executions (design 7.4)
  * uq_approvals_lineage_successor - at most one direct successor per approval (design 7.5 item 11)

The predicates below are FROZEN COPIES of the canonical SQL of app/repositories/tracked_sql.py (a migration must not import
application code that may change later); tests/unit/test_process_schema.py compares them with the current rendering by string
equality. The length limits 128 / 2000 and the absolute target limit 500 are the schema-fixed values of design 9.3: a change
needs a new migration together with the validator change. Every JSON key and literal is written out (no bind, no astext).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

TRACKED_ISH_SQL = (
    "jsonb_typeof(detail) = 'object'\n"
    "AND ( jsonb_exists(detail, 'tracked')\n"
    "      OR jsonb_exists_any(detail, ARRAY['v','runner_process','checkpoint_step_key','proof','disposition','reason_code','risk_acceptance','anomaly_original','anomaly_settlement']) )"
)

U_SQL = (
    "(detail -> 'tracked') = 'true'::jsonb\n"
    "AND (detail ->> 'proof') IS NULL AND (detail ->> 'disposition') IS NULL"
)

RA_VALID_SQL = (
    "jsonb_typeof(detail -> 'reason_code') = 'string' AND (detail ->> 'reason_code') = 'OPERATOR_RISK_ACCEPTED'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance') = 'object'\n"
    "AND (detail -> 'risk_acceptance') ?& ARRAY['approval_id','execution_id','pid','create_time','last_verdict','requested_at','rationale','reverify_result','applied_at']\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'approval_id')  = 'string' AND (detail -> 'risk_acceptance' ->> 'approval_id')  ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'execution_id') = 'string' AND (detail -> 'risk_acceptance' ->> 'execution_id') ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND (detail -> 'risk_acceptance' ->> 'execution_id') = id::text\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'pid')          = 'number' AND (detail -> 'risk_acceptance' -> 'pid') = (pid::text)::jsonb AND pid BETWEEN 1 AND 2147483647\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'create_time')  = 'number' AND (detail -> 'risk_acceptance' -> 'create_time') >= '0'::jsonb\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'last_verdict') = 'string' AND (detail -> 'risk_acceptance' ->> 'last_verdict') = 'UNCLEAR'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'requested_at') = 'string' AND (detail -> 'risk_acceptance' ->> 'requested_at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'rationale')    = 'string' AND char_length(detail -> 'risk_acceptance' ->> 'rationale') BETWEEN 1 AND 2000\n"
    "    AND (detail -> 'risk_acceptance' ->> 'rationale') ~ '[^ \\t\\n]' AND (detail -> 'risk_acceptance' ->> 'rationale') !~ '[\u0001-\b\u000b-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'reverify_result') = 'string'\n"
    "    AND (detail -> 'risk_acceptance' ->> 'reverify_result') IN ('UNKNOWN','ACCESS_DENIED','OTHER_HOST','H3_DISABLED','IDENTITY_RECORD_MISSING')\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'applied_at')   = 'string' AND (detail -> 'risk_acceptance' ->> 'applied_at')   ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$'\n"
    "AND ( NOT ((detail -> 'risk_acceptance') ? 'requested_by')\n"
    "      OR ( jsonb_typeof(detail -> 'risk_acceptance' -> 'requested_by') = 'string' AND (detail -> 'risk_acceptance' ->> 'requested_by') ~ '^[A-Za-z0-9._@:+-]{1,128}$' ) )"
)

RESOLVED_VALID_SQL = (
    "(detail -> 'tracked') = 'true'::jsonb\n"
    "AND jsonb_typeof(detail -> 'v') = 'number' AND (detail -> 'v') = '1'::jsonb\n"
    "AND finished_at IS NOT NULL\n"
    "AND (\n"
    "  ( (detail ->> 'proof') IN ('JOB_EMPTY','JOB_KILL_ON_CLOSE_ASSUMED','NEVER_RAN')\n"
    "    AND (detail ->> 'disposition') IS NULL )\n"
    "  OR\n"
    "  ( (detail ->> 'proof') IS NULL\n"
    "    AND (detail ->> 'disposition') = 'OPERATOR_RISK_ACCEPTED'\n"
    "    AND jsonb_typeof(detail -> 'reason_code') = 'string' AND (detail ->> 'reason_code') = 'OPERATOR_RISK_ACCEPTED'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance') = 'object'\n"
    "AND (detail -> 'risk_acceptance') ?& ARRAY['approval_id','execution_id','pid','create_time','last_verdict','requested_at','rationale','reverify_result','applied_at']\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'approval_id')  = 'string' AND (detail -> 'risk_acceptance' ->> 'approval_id')  ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'execution_id') = 'string' AND (detail -> 'risk_acceptance' ->> 'execution_id') ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND (detail -> 'risk_acceptance' ->> 'execution_id') = id::text\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'pid')          = 'number' AND (detail -> 'risk_acceptance' -> 'pid') = (pid::text)::jsonb AND pid BETWEEN 1 AND 2147483647\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'create_time')  = 'number' AND (detail -> 'risk_acceptance' -> 'create_time') >= '0'::jsonb\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'last_verdict') = 'string' AND (detail -> 'risk_acceptance' ->> 'last_verdict') = 'UNCLEAR'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'requested_at') = 'string' AND (detail -> 'risk_acceptance' ->> 'requested_at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'rationale')    = 'string' AND char_length(detail -> 'risk_acceptance' ->> 'rationale') BETWEEN 1 AND 2000\n"
    "    AND (detail -> 'risk_acceptance' ->> 'rationale') ~ '[^ \\t\\n]' AND (detail -> 'risk_acceptance' ->> 'rationale') !~ '[\u0001-\b\u000b-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'reverify_result') = 'string'\n"
    "    AND (detail -> 'risk_acceptance' ->> 'reverify_result') IN ('UNKNOWN','ACCESS_DENIED','OTHER_HOST','H3_DISABLED','IDENTITY_RECORD_MISSING')\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'applied_at')   = 'string' AND (detail -> 'risk_acceptance' ->> 'applied_at')   ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$'\n"
    "AND ( NOT ((detail -> 'risk_acceptance') ? 'requested_by')\n"
    "      OR ( jsonb_typeof(detail -> 'risk_acceptance' -> 'requested_by') = 'string' AND (detail -> 'risk_acceptance' ->> 'requested_by') ~ '^[A-Za-z0-9._@:+-]{1,128}$' ) ) )\n"
    ")"
)

ANOMALY_SQL = (
    "(jsonb_typeof(detail) = 'object'\n"
    "AND ( jsonb_exists(detail, 'tracked')\n"
    "      OR jsonb_exists_any(detail, ARRAY['v','runner_process','checkpoint_step_key','proof','disposition','reason_code','risk_acceptance','anomaly_original','anomaly_settlement']) )) AND (((detail -> 'tracked') = 'true'::jsonb\n"
    "AND (detail ->> 'proof') IS NULL AND (detail ->> 'disposition') IS NULL) IS NOT TRUE) AND (((detail -> 'tracked') = 'true'::jsonb\n"
    "AND jsonb_typeof(detail -> 'v') = 'number' AND (detail -> 'v') = '1'::jsonb\n"
    "AND finished_at IS NOT NULL\n"
    "AND (\n"
    "  ( (detail ->> 'proof') IN ('JOB_EMPTY','JOB_KILL_ON_CLOSE_ASSUMED','NEVER_RAN')\n"
    "    AND (detail ->> 'disposition') IS NULL )\n"
    "  OR\n"
    "  ( (detail ->> 'proof') IS NULL\n"
    "    AND (detail ->> 'disposition') = 'OPERATOR_RISK_ACCEPTED'\n"
    "    AND jsonb_typeof(detail -> 'reason_code') = 'string' AND (detail ->> 'reason_code') = 'OPERATOR_RISK_ACCEPTED'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance') = 'object'\n"
    "AND (detail -> 'risk_acceptance') ?& ARRAY['approval_id','execution_id','pid','create_time','last_verdict','requested_at','rationale','reverify_result','applied_at']\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'approval_id')  = 'string' AND (detail -> 'risk_acceptance' ->> 'approval_id')  ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'execution_id') = 'string' AND (detail -> 'risk_acceptance' ->> 'execution_id') ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND (detail -> 'risk_acceptance' ->> 'execution_id') = id::text\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'pid')          = 'number' AND (detail -> 'risk_acceptance' -> 'pid') = (pid::text)::jsonb AND pid BETWEEN 1 AND 2147483647\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'create_time')  = 'number' AND (detail -> 'risk_acceptance' -> 'create_time') >= '0'::jsonb\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'last_verdict') = 'string' AND (detail -> 'risk_acceptance' ->> 'last_verdict') = 'UNCLEAR'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'requested_at') = 'string' AND (detail -> 'risk_acceptance' ->> 'requested_at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'rationale')    = 'string' AND char_length(detail -> 'risk_acceptance' ->> 'rationale') BETWEEN 1 AND 2000\n"
    "    AND (detail -> 'risk_acceptance' ->> 'rationale') ~ '[^ \\t\\n]' AND (detail -> 'risk_acceptance' ->> 'rationale') !~ '[\u0001-\b\u000b-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]'\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'reverify_result') = 'string'\n"
    "    AND (detail -> 'risk_acceptance' ->> 'reverify_result') IN ('UNKNOWN','ACCESS_DENIED','OTHER_HOST','H3_DISABLED','IDENTITY_RECORD_MISSING')\n"
    "AND jsonb_typeof(detail -> 'risk_acceptance' -> 'applied_at')   = 'string' AND (detail -> 'risk_acceptance' ->> 'applied_at')   ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,6})?Z$'\n"
    "AND ( NOT ((detail -> 'risk_acceptance') ? 'requested_by')\n"
    "      OR ( jsonb_typeof(detail -> 'risk_acceptance' -> 'requested_by') = 'string' AND (detail -> 'risk_acceptance' ->> 'requested_by') ~ '^[A-Za-z0-9._@:+-]{1,128}$' ) ) )\n"
    ")) IS NOT TRUE)"
)


def upgrade() -> None:
    op.create_table('runtime_capabilities',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('process_risk_requests',
    sa.Column('approval_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('dirty_epoch', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('target_execution_ids', postgresql.ARRAY(sa.UUID()), nullable=False),
    sa.Column('requested_by', sa.Text(), nullable=True),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('claimed_by_incarnation', sa.UUID(), nullable=True),
    sa.Column('claim_generation', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('ACCEPT','RECHECK')", name='ck_process_risk_requests_kind'),
    sa.CheckConstraint("status IN ('REQUESTED','VERIFYING','APPLIED','REFUSED_ALIVE','RETRYABLE','NEEDS_REDECISION')", name='ck_process_risk_requests_status'),
    sa.CheckConstraint("cardinality(target_execution_ids) BETWEEN 1 AND 500", name='ck_process_risk_requests_target_count'),
    sa.CheckConstraint("requested_by IS NULL OR char_length(requested_by) BETWEEN 1 AND 128", name='ck_process_risk_requests_requested_by'),
    sa.CheckConstraint("char_length(rationale) BETWEEN 1 AND 2000", name='ck_process_risk_requests_rationale'),
    sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], name='fk_process_risk_requests_approval_id'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_process_risk_requests_task_id'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name='fk_process_risk_requests_project_id'),
    sa.PrimaryKeyConstraint('approval_id')
    )
    # worker discovery and claim
    op.create_index('ix_process_risk_requests_status_next_attempt', 'process_risk_requests', ['status', 'next_attempt_at'], unique=False)
    # latest observation, cooldown and window queries
    op.create_index('ix_process_risk_requests_project_finished', 'process_risk_requests', ['project_id', 'finished_at'], unique=False,
                    postgresql_where=sa.text('finished_at IS NOT NULL'))
    # no duplicate active recheck per project and epoch
    op.create_index('uq_process_risk_recheck_active', 'process_risk_requests', ['project_id', 'dirty_epoch'], unique=True,
                    postgresql_where=sa.text("kind = 'RECHECK' AND status IN ('REQUESTED','VERIFYING','RETRYABLE')"))
    # tracked executions: the unresolved set U and the anomaly set A (raw SQL text, literal keys)
    op.create_index('ix_executions_tracked_unresolved', 'executions', ['task_id'], unique=False,
                    postgresql_where=sa.text(U_SQL))
    op.create_index('ix_executions_tracked_anomaly', 'executions', ['task_id'], unique=False,
                    postgresql_where=sa.text(ANOMALY_SQL))
    # approval lineage: one direct successor per approval
    op.create_index('uq_approvals_lineage_successor', 'approvals', [sa.text("(target ->> 'supersedes_approval_id')")], unique=True,
                    postgresql_where=sa.text("(target ->> 'supersedes_approval_id') IS NOT NULL"))


def downgrade() -> None:
    op.drop_index('uq_approvals_lineage_successor', table_name='approvals',
                  postgresql_where=sa.text("(target ->> 'supersedes_approval_id') IS NOT NULL"))
    op.drop_index('ix_executions_tracked_anomaly', table_name='executions', postgresql_where=sa.text(ANOMALY_SQL))
    op.drop_index('ix_executions_tracked_unresolved', table_name='executions', postgresql_where=sa.text(U_SQL))
    op.drop_index('uq_process_risk_recheck_active', table_name='process_risk_requests',
                  postgresql_where=sa.text("kind = 'RECHECK' AND status IN ('REQUESTED','VERIFYING','RETRYABLE')"))
    op.drop_index('ix_process_risk_requests_project_finished', table_name='process_risk_requests',
                  postgresql_where=sa.text('finished_at IS NOT NULL'))
    op.drop_index('ix_process_risk_requests_status_next_attempt', table_name='process_risk_requests')
    op.drop_table('process_risk_requests')
    op.drop_table('runtime_capabilities')
