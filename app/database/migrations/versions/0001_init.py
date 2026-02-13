"""init

Revision ID: 0001_init
Revises: 
Create Date: 2026-02-11

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'gender') THEN
                CREATE TYPE gender AS ENUM ('male', 'female');
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'dialog_status') THEN
                CREATE TYPE dialog_status AS ENUM ('active', 'finished');
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'rating_type') THEN
                CREATE TYPE rating_type AS ENUM ('chat', 'appearance');
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("gender", pg.ENUM("male", "female", name="gender", create_type=False), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("rating_chat", sa.Float(), server_default="5.0", nullable=False),
        sa.Column("rating_appearance", sa.Float(), nullable=True),
        sa.Column("last_20_avg_chat", sa.Float(), server_default="0", nullable=False),
        sa.Column("last_20_avg_appearance", sa.Float(), server_default="0", nullable=False),
        sa.Column("season_rating_chat", sa.Float(), server_default="0", nullable=False),
        sa.Column("season_rating_appearance", sa.Float(), server_default="0", nullable=False),
        sa.Column("calibration_counter", sa.Integer(), server_default="0", nullable=False),
        sa.Column("subscription_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_under_review", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_banned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=False)
    op.create_index("ix_users_city", "users", ["city"], unique=False)
    op.create_index("ix_users_subscription_until", "users", ["subscription_until"], unique=False)

    op.create_table(
        "dialogs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user1_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user2_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "status",
            pg.ENUM("active", "finished", name="dialog_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
        sa.Column("has_photos", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_dialogs_user_pair", "dialogs", ["user1_id", "user2_id"], unique=False)
    op.create_index("ix_dialogs_status", "dialogs", ["status"], unique=False)

    op.create_table(
        "photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_photos_dialog_id", "photos", ["dialog_id"], unique=False)
    op.create_index("ix_photos_owner_user_id", "photos", ["owner_user_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("photos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_messages_dialog_id", "messages", ["dialog_id"], unique=False)
    op.create_index("ix_messages_from_user_id", "messages", ["from_user_id"], unique=False)

    op.create_table(
        "ratings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("to_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating_type", pg.ENUM("chat", "appearance", name="rating_type", create_type=False), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ux_ratings_unique",
        "ratings",
        ["dialog_id", "from_user_id", "to_user_id", "rating_type"],
        unique=True,
    )
    op.create_index("ix_ratings_to_user_id", "ratings", ["to_user_id"], unique=False)
    op.create_index("ix_ratings_dialog_id", "ratings", ["dialog_id"], unique=False)

    op.create_table(
        "complaints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dialog_id", sa.Integer(), sa.ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_complaints_dialog_id", "complaints", ["dialog_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_complaints_dialog_id", table_name="complaints")
    op.drop_table("complaints")

    op.drop_index("ix_ratings_dialog_id", table_name="ratings")
    op.drop_index("ix_ratings_to_user_id", table_name="ratings")
    op.drop_index("ux_ratings_unique", table_name="ratings")
    op.drop_table("ratings")

    op.drop_index("ix_messages_from_user_id", table_name="messages")
    op.drop_index("ix_messages_dialog_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_photos_owner_user_id", table_name="photos")
    op.drop_index("ix_photos_dialog_id", table_name="photos")
    op.drop_table("photos")

    op.drop_index("ix_dialogs_status", table_name="dialogs")
    op.drop_index("ix_dialogs_user_pair", table_name="dialogs")
    op.drop_table("dialogs")

    op.drop_index("ix_users_subscription_until", table_name="users")
    op.drop_index("ix_users_city", table_name="users")
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")

    op.execute("DROP TYPE IF EXISTS rating_type")
    op.execute("DROP TYPE IF EXISTS dialog_status")
    op.execute("DROP TYPE IF EXISTS gender")
