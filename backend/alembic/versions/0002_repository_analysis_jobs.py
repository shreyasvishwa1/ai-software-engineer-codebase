"""repository analysis jobs

Links analysis jobs to repositories and gives them a type.

Existing rows are never assigned a guessed repository or type: both new columns are
nullable, and a NOT VALID check constraint requires them for rows inserted or updated
after this migration only. Existing status values keep their meaning through an in-place
rename (queued -> pending, running -> processing).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_INGESTION = "job_type = 'ingestion' AND status IN ('pending', 'processing')"


def upgrade() -> None:
    # Status: same four states, clearer names (existing rows keep their meaning).
    op.execute("ALTER TYPE analysis_job_status RENAME VALUE 'queued' TO 'pending'")
    op.execute("ALTER TYPE analysis_job_status RENAME VALUE 'running' TO 'processing'")
    op.alter_column('analysis_jobs', 'status', server_default='pending')

    job_type = postgresql.ENUM('ingestion', name='analysis_job_type')
    job_type.create(op.get_bind())
    op.add_column('analysis_jobs', sa.Column('repository_id', sa.Uuid(), nullable=True))
    op.add_column(
        'analysis_jobs',
        sa.Column(
            'job_type',
            postgresql.ENUM('ingestion', name='analysis_job_type', create_type=False),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        op.f('fk_analysis_jobs_repository_id_repositories'),
        'analysis_jobs',
        'repositories',
        ['repository_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_index(
        'ix_analysis_jobs_repository_id_created_at',
        'analysis_jobs',
        ['repository_id', 'created_at'],
        unique=False,
    )
    op.create_index(
        'uq_analysis_jobs_active_ingestion',
        'analysis_jobs',
        ['repository_id'],
        unique=True,
        postgresql_where=sa.text(ACTIVE_INGESTION),
    )
    op.create_check_constraint(
        op.f('ck_analysis_jobs_repository_and_type_required'),
        'analysis_jobs',
        'repository_id IS NOT NULL AND job_type IS NOT NULL',
        postgresql_not_valid=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f('ck_analysis_jobs_repository_and_type_required'), 'analysis_jobs', type_='check'
    )
    op.drop_index('uq_analysis_jobs_active_ingestion', table_name='analysis_jobs')
    op.drop_index('ix_analysis_jobs_repository_id_created_at', table_name='analysis_jobs')
    op.drop_constraint(
        op.f('fk_analysis_jobs_repository_id_repositories'), 'analysis_jobs', type_='foreignkey'
    )
    op.drop_column('analysis_jobs', 'job_type')
    op.drop_column('analysis_jobs', 'repository_id')
    postgresql.ENUM(name='analysis_job_type').drop(op.get_bind(), checkfirst=True)

    # Rename the values back first: the old default 'queued' only exists afterwards.
    op.execute("ALTER TYPE analysis_job_status RENAME VALUE 'processing' TO 'running'")
    op.execute("ALTER TYPE analysis_job_status RENAME VALUE 'pending' TO 'queued'")
    op.alter_column('analysis_jobs', 'status', server_default='queued')
