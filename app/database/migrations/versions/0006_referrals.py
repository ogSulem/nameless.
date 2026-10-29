"""referrals (cold start) and removal of unused personal-data columns

Revision ID: 0006_referrals
Revises: 0005_admin_legal
Create Date: 2026-10-29

Why:
  * `users.username` / `users.full_name` were added in 0004 but the bot never
    reads them — dead personal data. Dropping them keeps the privacy policy
    ("we do not store your name or @username") literally true.
  * Referrals give the product an honest cold-start loop: an existing user
    invites a friend, the inviter gets Premium days, the queue grows.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_referrals"
down_revision = "0005_admin_legal"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def _has_index(table: str, name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    try:
        return any(ix["name"] == name for ix in insp.get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    user_cols = _columns("users")

    with op.batch_alter_table("users") as batch:
        if "invited_by" not in user_cols:
            batch.add_column(sa.Column("invited_by", sa.BigInteger(), nullable=True))
        if "invite_rewarded" not in user_cols:
            batch.add_column(
                sa.Column("invite_rewarded", sa.Boolean(), server_default=sa.text("false"), nullable=False)
            )

    if not _has_index("users", "ix_users_invited_by"):
        op.create_index("ix_users_invited_by", "users", ["invited_by"])

    # Dead PII: never written by the application, never read.
    with op.batch_alter_table("users") as batch:
        if "username" in user_cols:
            batch.drop_column("username")
        if "full_name" in user_cols:
            batch.drop_column("full_name")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("username", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("full_name", sa.String(length=256), nullable=True))

    op.drop_index("ix_users_invited_by", table_name="users")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("invite_rewarded")
        batch.drop_column("invited_by")
