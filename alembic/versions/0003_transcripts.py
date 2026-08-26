"""Add transcripts and job payloads for transcription jobs

Revision ID: 0003_transcripts
Revises: 0002_source_assets
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_transcripts"
down_revision = "0002_source_assets"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    op.add_column("jobs", sa.Column("payload", sa.String(length=255), nullable=True))
    op.create_table(
        "transcripts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("source_assets.id"), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_transcripts_source_id", "transcripts", ["source_id"], unique=True
    )


def downgrade() -> None:
    if not _table_exists("transcripts"):
        return
    op.execute("DROP INDEX IF EXISTS ix_transcripts_source_id")
    op.execute("DROP TABLE IF EXISTS transcripts")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS payload")
