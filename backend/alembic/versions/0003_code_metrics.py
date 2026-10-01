"""code metrics analysis

Adds the 'code_metrics' analysis job type, the file_metrics table (per-file line counts of
the analyzed snapshot) and a partial unique index allowing one active code-metrics job per
repository.

Downgrade removes code-metrics jobs and rebuilds analysis_job_type without the new value
(PostgreSQL cannot drop an enum value); the ingestion partial index depends on the column
and is re-created with its exact definition.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE_INGESTION = "job_type = 'ingestion' AND status IN ('pending', 'processing')"
ACTIVE_CODE_METRICS = "job_type = 'code_metrics' AND status IN ('pending', 'processing')"
COUNTS_MATCH_STATUS = (
    "(status = 'analyzed') = (line_count IS NOT NULL AND blank_line_count IS NOT NULL)"
    " AND (line_count IS NULL OR line_count >= 0)"
    " AND (blank_line_count IS NULL OR blank_line_count BETWEEN 0 AND line_count)"
)


def upgrade() -> None:
    # A new enum value must be committed before it can be used, and the partial index
    # below compares job_type with it: add it in its own committed transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE analysis_job_type ADD VALUE IF NOT EXISTS 'code_metrics'")

    op.create_table(
        "file_metrics",
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_job_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=True),
        sa.Column("blank_line_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('analyzed', 'binary', 'too_large', 'unavailable')",
            name=op.f("ck_file_metrics_status_valid"),
        ),
        sa.CheckConstraint(COUNTS_MATCH_STATUS, name=op.f("ck_file_metrics_counts_match_status")),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_file_metrics_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_file_metrics_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_job_id"],
            ["analysis_jobs.id"],
            name=op.f("fk_file_metrics_analysis_job_id_analysis_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("file_id", name=op.f("pk_file_metrics")),
    )
    op.create_index(
        op.f("ix_file_metrics_repository_id"), "file_metrics", ["repository_id"], unique=False
    )
    op.create_index(
        "uq_analysis_jobs_active_code_metrics",
        "analysis_jobs",
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_CODE_METRICS),
    )


def downgrade() -> None:
    op.drop_index("uq_analysis_jobs_active_code_metrics", table_name="analysis_jobs")
    op.drop_index(op.f("ix_file_metrics_repository_id"), table_name="file_metrics")
    op.drop_table("file_metrics")
    op.execute("DELETE FROM analysis_jobs WHERE job_type = 'code_metrics'")

    # Rebuild the type without 'code_metrics'; the ingestion index depends on the column.
    op.drop_index("uq_analysis_jobs_active_ingestion", table_name="analysis_jobs")
    op.execute("ALTER TYPE analysis_job_type RENAME TO analysis_job_type_0003")
    op.execute("CREATE TYPE analysis_job_type AS ENUM ('ingestion')")
    op.execute(
        "ALTER TABLE analysis_jobs ALTER COLUMN job_type TYPE analysis_job_type "
        "USING job_type::text::analysis_job_type"
    )
    op.execute("DROP TYPE analysis_job_type_0003")
    op.create_index(
        "uq_analysis_jobs_active_ingestion",
        "analysis_jobs",
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_INGESTION),
    )
