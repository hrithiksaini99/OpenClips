"""Add clip selection fields

Revision ID: 0004_clip_selection
Revises: 0003_transcripts
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_clip_selection"
down_revision = "0003_transcripts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("source_asset_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_clips_source_asset_id", "clips", "source_assets", ["source_asset_id"], ["id"]
    )
    op.create_index("ix_clips_source_asset_id", "clips", ["source_asset_id"])
    op.add_column("clips", sa.Column("title", sa.String(length=255), nullable=True))
    op.add_column("clips", sa.Column("start_time", sa.Float(), nullable=True))
    op.add_column("clips", sa.Column("end_time", sa.Float(), nullable=True))
    op.add_column("clips", sa.Column("selection_score", sa.Float(), nullable=True))


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("clips"):
        return
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS selection_score")
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS end_time")
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS start_time")
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS title")
    op.execute("DROP INDEX IF EXISTS ix_clips_source_asset_id")
    op.execute(
        "ALTER TABLE clips DROP CONSTRAINT IF EXISTS fk_clips_source_asset_id"
    )
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS source_asset_id")
