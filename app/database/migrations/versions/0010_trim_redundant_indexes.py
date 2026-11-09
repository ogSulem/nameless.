"""Drop indexes that only duplicate a prefix of another index.

Postgres serves `WHERE status = ?` from a btree on `(status, created_at)`, so a
single-column index on the same column is pure write amplification on every
INSERT/UPDATE. Same story for `messages.dialog_id`, `ratings.to_user_id` and
`users.season_rating_chat`. Two indexes are dropped because nothing queries them
at all.

Revision ID: 0010_trim_redundant_indexes
Revises: 0009_drop_legacy_rating_columns
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_trim_redundant_indexes"
down_revision = "0009_drop_legacy_rating_columns"
branch_labels = None
depends_on = None

# (index name, table, columns, covered by another index / unused)
_DROP = (
    ("ix_dialogs_status", "dialogs", ["status"]),
    ("ix_messages_dialog_id", "messages", ["dialog_id"]),
    ("ix_ratings_to_user_id", "ratings", ["to_user_id"]),
    ("ix_users_season_rating_chat", "users", ["season_rating_chat"]),
    ("ix_users_season_rating_appearance", "users", ["season_rating_appearance"]),
    ("ix_users_city", "users", ["city"]),
)


def _existing(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    for name, table, _columns in _DROP:
        if name in _existing(table):
            op.drop_index(name, table_name=table)


def downgrade() -> None:
    for name, table, columns in _DROP:
        if name not in _existing(table):
            op.create_index(name, table, columns, unique=False)
