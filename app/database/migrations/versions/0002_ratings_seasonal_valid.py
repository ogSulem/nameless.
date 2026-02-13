"""ratings seasonal validity

Revision ID: 0002_ratings_seasonal_valid
Revises: 0001_init
Create Date: 2026-02-12

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_ratings_seasonal_valid"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ratings",
        sa.Column("is_seasonal_valid", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.create_index(
        "ix_ratings_seasonal_valid",
        "ratings",
        ["to_user_id", "rating_type", "is_seasonal_valid"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ratings_seasonal_valid", table_name="ratings")
    op.drop_column("ratings", "is_seasonal_valid")
