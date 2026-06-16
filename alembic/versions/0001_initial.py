"""initial schema"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def _create_enum(name: str, *values: str) -> postgresql.ENUM:
    enum = postgresql.ENUM(*values, name=name)
    enum.create(op.get_bind(), checkfirst=True)
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade():
    user_role = _create_enum('user_role', 'user', 'admin')
    job_status = _create_enum('job_status', 'pending', 'queued', 'running', 'completed', 'failed', 'cancelled')
    log_level = _create_enum('log_level', 'info', 'warning', 'error')

    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('hashed_password', sa.String(255), nullable=False),
        sa.Column('role', user_role, nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    op.create_table(
        'jobs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', job_status, nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('result', postgresql.JSONB()),
        sa.Column('error_message', sa.Text()),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_retries', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('idempotency_key', sa.String(255)),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('cancelled_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_jobs_owner_id', 'jobs', ['owner_id'])
    op.create_index('ix_jobs_status', 'jobs', ['status'])
    op.create_index('ix_jobs_created_at', 'jobs', ['created_at'])
    op.create_index('ix_jobs_owner_created', 'jobs', ['owner_id', 'created_at'])
    op.create_index(
        'uq_jobs_owner_idempotency_key',
        'jobs',
        ['owner_id', 'idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )

    op.create_table(
        'job_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('level', log_level, nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_job_logs_job_id', 'job_logs', ['job_id'])


def downgrade():
    op.drop_table('job_logs')
    op.drop_table('jobs')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
    op.execute('DROP TYPE IF EXISTS log_level')
    op.execute('DROP TYPE IF EXISTS job_status')
    op.execute('DROP TYPE IF EXISTS user_role')
