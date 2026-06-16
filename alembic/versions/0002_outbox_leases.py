"""add outbox, job leases, and correlation ids

Revision ID: 0002_outbox_leases
Revises: 0001_initial
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002_outbox_leases'
down_revision = '0001_initial'
branch_labels = None
depends_on = None

def upgrade():
    outbox_status = postgresql.ENUM('pending', 'sent', 'failed', name='outbox_status')
    outbox_status.create(op.get_bind(), checkfirst=True)
    outbox_status_type = postgresql.ENUM('pending', 'sent', 'failed', name='outbox_status', create_type=False)
    op.add_column('jobs', sa.Column('correlation_id', sa.String(length=64), nullable=True))
    op.execute("UPDATE jobs SET correlation_id = md5(random()::text || clock_timestamp()::text) WHERE correlation_id IS NULL")
    op.alter_column('jobs', 'correlation_id', nullable=False)
    op.add_column('jobs', sa.Column('locked_by', sa.String(length=128), nullable=True))
    op.add_column('jobs', sa.Column('lock_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_jobs_correlation_id', 'jobs', ['correlation_id'])
    op.create_index('ix_jobs_locked_by', 'jobs', ['locked_by'])
    op.create_index('ix_jobs_lock_expires_at', 'jobs', ['lock_expires_at'])
    op.create_index('ix_jobs_owner_status_lock', 'jobs', ['owner_id', 'status', 'lock_expires_at'])
    op.create_table('outbox_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('status', outbox_status_type, nullable=False, server_default='pending'),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_outbox_events_event_type', 'outbox_events', ['event_type'])
    op.create_index('ix_outbox_events_status', 'outbox_events', ['status'])
    op.create_index('ix_outbox_events_next_attempt_at', 'outbox_events', ['next_attempt_at'])
    op.create_index('ix_outbox_events_created_at', 'outbox_events', ['created_at'])

def downgrade():
    op.drop_table('outbox_events')
    op.drop_index('ix_jobs_owner_status_lock', table_name='jobs')
    op.drop_index('ix_jobs_lock_expires_at', table_name='jobs')
    op.drop_index('ix_jobs_locked_by', table_name='jobs')
    op.drop_index('ix_jobs_correlation_id', table_name='jobs')
    op.drop_column('jobs', 'lock_expires_at')
    op.drop_column('jobs', 'locked_by')
    op.drop_column('jobs', 'correlation_id')
    op.execute('DROP TYPE IF EXISTS outbox_status')
