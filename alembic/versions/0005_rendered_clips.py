"""Add rendered clip artifacts

Revision ID: 0005_rendered_clips
Revises: 0004_clip_selection
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_rendered_clips"
down_revision = "0004_clip_selection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("caption_path", sa.String(length=1024), nullable=True))
    op.add_column("clips", sa.Column("caption_template", sa.String(length=64), nullable=True))
    op.add_column("clips", sa.Column("render_width", sa.Integer(), nullable=True))
    op.add_column("clips", sa.Column("render_height", sa.Integer(), nullable=True))


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("clips"):
        return
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS render_height")
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS render_width")
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS caption_template")
    op.execute("ALTER TABLE clips DROP COLUMN IF EXISTS caption_path")
