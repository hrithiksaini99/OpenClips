"""Add clip caption edits

Revision ID: 0006_caption_edits
Revises: 0005_rendered_clips
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_caption_edits"
down_revision = "0005_rendered_clips"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("caption_edits", sa.JSON(), nullable=True))


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("clips"):
        return
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS caption_edits")
