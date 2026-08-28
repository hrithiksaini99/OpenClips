"""Index due publications for skip-locked dispatch claims.

Revision ID: 0009_publication_dispatch
Revises: 0008_operational_core
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_publication_dispatch"
down_revision = "0008_operational_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The QUEUED and CANCELLED lifecycle states need no column change: status is
    # already a String(32). Scheduler polls filter on status plus scheduled_at.
    op.create_index(
        "ix_publication_records_due", "publication_records", ["status", "scheduled_at"]
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("publication_records"):
        return
    op.execute("DROP INDEX IF EXISTS ix_publication_records_due")
