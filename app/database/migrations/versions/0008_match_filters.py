"""Premium matching filters: age range and "my city only".

Revision ID: 0008_match_filters
Revises: 0007_promo_codes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_match_filters"
down_revision = "0007_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("users")}

    with op.batch_alter_table("users") as batch:
        if "filter_age_min" not in columns:
            batch.add_column(
                sa.Column("filter_age_min", sa.Integer(), nullable=False, server_default="18")
            )
        if "filter_age_max" not in columns:
            batch.add_column(
                sa.Column("filter_age_max", sa.Integer(), nullable=False, server_default="99")
            )
        if "filter_city_only" not in columns:
            batch.add_column(
                sa.Column(
                    "filter_city_only", sa.Boolean(), nullable=False, server_default=sa.text("false")
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("filter_city_only")
        batch.drop_column("filter_age_max")
        batch.drop_column("filter_age_min")
