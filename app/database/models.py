from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Gender(str, enum.Enum):
    male = "male"
    female = "female"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    gender: Mapped[Gender] = mapped_column(Enum(Gender, name="gender"), nullable=False)
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(256), nullable=True)

    rating_chat: Mapped[float] = mapped_column(Float, nullable=False, server_default="5.0")
    rating_appearance: Mapped[float | None] = mapped_column(Float, nullable=True)

    last_20_avg_chat: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    last_20_avg_appearance: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    season_rating_chat: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    season_rating_appearance: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")

    calibration_counter: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    subscription_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_under_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_users_telegram_id", "telegram_id"),
        Index("ix_users_city", "city"),
        Index("ix_users_subscription_until", "subscription_until"),
        Index("ix_users_rating_chat", "season_rating_chat"),
        Index("ix_users_rating_appearance", "season_rating_appearance"),
        Index("ix_users_created_at", "created_at"),
        Index("ix_users_premium_match", "season_rating_chat", "id"),
    )


class DialogStatus(str, enum.Enum):
    active = "active"
    finished = "finished"


class Dialog(Base):
    __tablename__ = "dialogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user1_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user2_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    status: Mapped[DialogStatus] = mapped_column(
        Enum(DialogStatus, name="dialog_status"),
        nullable=False,
        server_default=DialogStatus.active.value,
    )
    has_photos: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list["Message"]] = relationship("Message", back_populates="dialog")

    __table_args__ = (
        Index("ix_dialogs_user_pair", "user1_id", "user2_id"),
        Index("ix_dialogs_status", "status"),
    )


class ActiveDialog(Base):
    __tablename__ = "active_dialogs"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_active_dialogs_dialog_id", "dialog_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_id: Mapped[int | None] = mapped_column(ForeignKey("photos.id", ondelete="SET NULL"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    dialog: Mapped["Dialog"] = relationship("Dialog", back_populates="messages")

    __table_args__ = (
        Index("ix_messages_dialog_id", "dialog_id"),
        Index("ix_messages_from_user_id", "from_user_id"),
    )


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_photos_dialog_id", "dialog_id"),
        Index("ix_photos_owner_user_id", "owner_user_id"),
        Index("ix_photos_created_at", "created_at"),
    )


class RatingType(str, enum.Enum):
    chat = "chat"
    appearance = "appearance"


class Rating(Base):
    __tablename__ = "ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)

    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    rating_type: Mapped[RatingType] = mapped_column(Enum(RatingType, name="rating_type"), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)

    is_seasonal_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ux_ratings_unique", "dialog_id", "from_user_id", "to_user_id", "rating_type", unique=True),
        Index("ix_ratings_to_user_id", "to_user_id"),
        Index("ix_ratings_dialog_id", "dialog_id"),
        Index("ix_ratings_seasonal_valid", "to_user_id", "rating_type", "is_seasonal_valid"),
    )


class Complaint(Base):
    __tablename__ = "complaints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dialog_id: Mapped[int] = mapped_column(ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_complaints_dialog_id", "dialog_id"),
        Index("ix_complaints_created_at", "created_at"),
    )
