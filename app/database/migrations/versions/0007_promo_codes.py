"""Promo codes: free Premium campaigns with hard usage caps.

Revision ID: 0007_promo_codes
Revises: 0006_referrals
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_promo_codes"
down_revision = "0006_referrals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "promo_codes" not in tables:
        op.create_table(
            "promo_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(length=32), nullable=False, unique=True),
            sa.Column("days", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("note", sa.String(length=128), nullable=True),
            sa.Column("created_by", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_promo_codes_active", "promo_codes", ["is_active", "expires_at"])

    if "promo_redemptions" not in tables:
        op.create_table(
            "promo_redemptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "code_id",
                sa.Integer(),
                sa.ForeignKey("promo_codes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("days", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("code_id", "telegram_id", name="ux_promo_redemption_user"),
        )
        op.create_index("ix_promo_redemptions_code_id", "promo_redemptions", ["code_id"])
        op.create_index("ix_promo_redemptions_tg", "promo_redemptions", ["telegram_id"])

    # Growth defaults: a referral now buys a month, not a week.
    op.execute("UPDATE promo_codes SET used_count = 0 WHERE used_count < 0")


def downgrade() -> None:
    op.drop_table("promo_redemptions")
    op.drop_table("promo_codes")
