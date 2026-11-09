"""Store the seek preference on the user instead of only in Redis.

A flushed or evicted cache used to silently reset "whom am I looking for" to the
opposite gender, which could pair people the user never wanted to see.

Revision ID: 0008_seek_gender
Revises: 0007_promo_codes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_seek_gender"
down_revision = "0007_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "seek_gender" not in columns:
        op.add_column("users", sa.Column("seek_gender", sa.String(length=8), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    if "seek_gender" in columns:
        op.drop_column("users", "seek_gender")
