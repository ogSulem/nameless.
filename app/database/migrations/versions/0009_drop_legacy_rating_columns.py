"""Drop three rating columns nothing ever read.

`users.rating_chat`, `users.rating_appearance` and `users.calibration_counter`
were left over from the first draft: the bot rates through the `ratings` table
and stores the result in `season_rating_*`. Keeping dead columns on the hot
`users` table only makes every `SELECT *` heavier and the model harder to trust.

Revision ID: 0009_drop_legacy_rating_columns
Revises: 0008_seek_gender
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_drop_legacy_rating_columns"
down_revision = "0008_seek_gender"
branch_labels = None
depends_on = None

_COLUMNS = ("rating_chat", "rating_appearance", "calibration_counter")


def _columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    existing = _columns()
    for name in _COLUMNS:
        if name in existing:
            op.drop_column("users", name)


def downgrade() -> None:
    existing = _columns()
    if "rating_chat" not in existing:
        op.add_column("users", sa.Column("rating_chat", sa.Float(), server_default="5.0", nullable=False))
    if "rating_appearance" not in existing:
        op.add_column("users", sa.Column("rating_appearance", sa.Float(), nullable=True))
    if "calibration_counter" not in existing:
        op.add_column(
            "users", sa.Column("calibration_counter", sa.Integer(), server_default="0", nullable=False)
        )
