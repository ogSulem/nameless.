"""active dialogs guard

Revision ID: 0003_active_dialogs_guard
Revises: 0002_ratings_seasonal_valid
Create Date: 2026-02-12

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_active_dialogs_guard"
down_revision = "0002_ratings_seasonal_valid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "active_dialogs",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_active_dialogs_dialog_id", "active_dialogs", ["dialog_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_active_dialogs_dialog_id", table_name="active_dialogs")
    op.drop_table("active_dialogs")
