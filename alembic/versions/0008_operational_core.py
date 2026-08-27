"""Add transactional job outbox.

Revision ID: 0008_operational_core
Revises: 0007_publication_records
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_operational_core"
down_revision = "0007_publication_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_assets",
        sa.Column("auto_process", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("queue_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_events_job_id", "outbox_events", ["job_id"])
    op.create_index("ix_outbox_events_available_at", "outbox_events", ["available_at"])
    op.create_index("ix_outbox_events_due", "outbox_events", ["status", "available_at"])


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("outbox_events"):
        return
    op.drop_index("ix_outbox_events_due", table_name="outbox_events")
    op.drop_index("ix_outbox_events_available_at", table_name="outbox_events")
    op.drop_index("ix_outbox_events_job_id", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_column("source_assets", "auto_process")
