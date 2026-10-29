"""admin panel, payments ledger, legal consent, retention indexes

Revision ID: 0005_admin_legal
Revises: 0004_add_user_details
Create Date: 2026-10-29

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_admin_legal"
down_revision = "0004_add_user_details"
branch_labels = None
depends_on = None


def _has_index(inspector, table: str, name: str) -> bool:
    try:
        return any(ix["name"] == name for ix in inspector.get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    user_cols = {c["name"] for c in insp.get_columns("users")}
    with op.batch_alter_table("users") as batch:
        if "chat_ratings_count" not in user_cols:
            batch.add_column(sa.Column("chat_ratings_count", sa.Integer(), server_default="0", nullable=False))
        if "appearance_ratings_count" not in user_cols:
            batch.add_column(sa.Column("appearance_ratings_count", sa.Integer(), server_default="0", nullable=False))
        if "ban_reason" not in user_cols:
            batch.add_column(sa.Column("ban_reason", sa.Text(), nullable=True))
        if "banned_at" not in user_cols:
            batch.add_column(sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True))
        if "muted_until" not in user_cols:
            batch.add_column(sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True))
        if "consent_at" not in user_cols:
            batch.add_column(sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True))
        if "consent_version" not in user_cols:
            batch.add_column(sa.Column("consent_version", sa.String(length=16), nullable=True))
        if "last_seen_at" not in user_cols:
            batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
        if "is_deleted" not in user_cols:
            batch.add_column(sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False))

    complaint_cols = {c["name"] for c in insp.get_columns("complaints")}
    with op.batch_alter_table("complaints") as batch:
        if "accused_user_id" not in complaint_cols:
            batch.add_column(sa.Column("accused_user_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_complaints_accused_user_id", "users", ["accused_user_id"], ["id"], ondelete="SET NULL"
            )
        if "status" not in complaint_cols:
            batch.add_column(sa.Column("status", sa.String(length=16), server_default="open", nullable=False))
        if "handled_by" not in complaint_cols:
            batch.add_column(sa.Column("handled_by", sa.BigInteger(), nullable=True))
        if "handled_at" not in complaint_cols:
            batch.add_column(sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True))
        if "resolution" not in complaint_cols:
            batch.add_column(sa.Column("resolution", sa.Text(), nullable=True))

    # Backfill: complaints created before this migration have no accused user.
    op.execute(
        """
        UPDATE complaints c
           SET accused_user_id = CASE
                 WHEN d.user1_id = c.from_user_id THEN d.user2_id
                 ELSE d.user1_id
               END
          FROM dialogs d
         WHERE d.id = c.dialog_id AND c.accused_user_id IS NULL
        """
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), server_default="RUB", nullable=False),
        sa.Column("days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("receipt_email", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider", "external_id", name="ux_payments_provider_external"),
    )
    op.create_index("ix_payments_telegram_id", "payments", ["telegram_id"])
    op.create_index("ix_payments_created_at", "payments", ["created_at"])
    op.create_index("ix_payments_status", "payments", ["status"])

    op.create_table(
        "admin_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admin_tg", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_tg", sa.BigInteger(), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_admin_audit_created_at", "admin_audit", ["created_at"])
    op.create_index("ix_admin_audit_admin", "admin_audit", ["admin_tg", "created_at"])

    # Indexes that the ORM always assumed but earlier migrations never created.
    for name, table, cols in (
        ("ix_users_season_rating_chat", "users", ["season_rating_chat"]),
        ("ix_users_season_rating_appearance", "users", ["season_rating_appearance"]),
        ("ix_users_created_at", "users", ["created_at"]),
        ("ix_users_premium_match", "users", ["season_rating_chat", "season_rating_appearance"]),
        ("ix_photos_created_at", "photos", ["created_at"]),
        ("ix_complaints_created_at", "complaints", ["created_at"]),
        ("ix_complaints_status_created", "complaints", ["status", "created_at"]),
        ("ix_complaints_accused", "complaints", ["accused_user_id"]),
        ("ix_messages_dialog_created", "messages", ["dialog_id", "created_at"]),
        ("ix_messages_created_at", "messages", ["created_at"]),
        ("ix_dialogs_status_created", "dialogs", ["status", "created_at"]),
        ("ix_ratings_created_at", "ratings", ["created_at"]),
    ):
        if not _has_index(insp, table, name):
            op.create_index(name, table, cols)


def downgrade() -> None:
    for name, table in (
        ("ix_ratings_created_at", "ratings"),
        ("ix_dialogs_status_created", "dialogs"),
        ("ix_messages_created_at", "messages"),
        ("ix_messages_dialog_created", "messages"),
        ("ix_complaints_accused", "complaints"),
        ("ix_complaints_status_created", "complaints"),
        ("ix_complaints_created_at", "complaints"),
        ("ix_photos_created_at", "photos"),
        ("ix_users_premium_match", "users"),
        ("ix_users_created_at", "users"),
        ("ix_users_season_rating_appearance", "users"),
        ("ix_users_season_rating_chat", "users"),
    ):
        op.drop_index(name, table_name=table)

    op.drop_index("ix_admin_audit_admin", table_name="admin_audit")
    op.drop_index("ix_admin_audit_created_at", table_name="admin_audit")
    op.drop_table("admin_audit")

    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_created_at", table_name="payments")
    op.drop_index("ix_payments_telegram_id", table_name="payments")
    op.drop_table("payments")

    with op.batch_alter_table("complaints") as batch:
        batch.drop_column("resolution")
        batch.drop_column("handled_at")
        batch.drop_column("handled_by")
        batch.drop_column("status")
        batch.drop_constraint("fk_complaints_accused_user_id", type_="foreignkey")
        batch.drop_column("accused_user_id")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("is_deleted")
        batch.drop_column("last_seen_at")
        batch.drop_column("consent_version")
        batch.drop_column("consent_at")
        batch.drop_column("muted_until")
        batch.drop_column("banned_at")
        batch.drop_column("ban_reason")
        batch.drop_column("appearance_ratings_count")
        batch.drop_column("chat_ratings_count")
