"""Add publication records

Revision ID: 0007_publication_records
Revises: 0006_caption_edits
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_publication_records"
down_revision = "0006_caption_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("clip_id", sa.Uuid(), sa.ForeignKey("clips.id"), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("external_url", sa.String(length=512), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_publication_records_clip_id", "publication_records", ["clip_id"]
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("publication_records"):
        return
    op.execute("DROP INDEX IF EXISTS ix_publication_records_clip_id")
    op.execute("DROP TABLE IF EXISTS publication_records")
