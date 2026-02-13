import argparse
import asyncio
import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import select

from app.config import Settings
from app.database.models import Dialog, Message as DbMessage, User
from app.database.session import create_engine, create_sessionmaker


def _fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "-"
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


async def dump_dialog(session, dialog_id: int, limit: int | None) -> None:
    res_d = await session.execute(select(Dialog).where(Dialog.id == dialog_id))
    dialog = res_d.scalar_one_or_none()
    if dialog is None:
        print(f"Dialog {dialog_id} not found")
        return

    users = {}
    res_u = await session.execute(select(User).where(User.id.in_([dialog.user1_id, dialog.user2_id])))
    for u in res_u.scalars().all():
        users[u.id] = u.telegram_id

    q = (
        select(DbMessage)
        .where(DbMessage.dialog_id == dialog_id)
        .order_by(DbMessage.created_at.asc())
    )
    res_m = await session.execute(q)
    msgs = res_m.scalars().all()
    if limit:
        msgs = msgs[-limit:]

    print(f"=== Dialog {dialog_id} ===")
    print(f"user1: {users.get(dialog.user1_id)} (user_id={dialog.user1_id})")
    print(f"user2: {users.get(dialog.user2_id)} (user_id={dialog.user2_id})")
    print(f"status: {dialog.status} created: {_fmt_dt(dialog.created_at)} finished: {_fmt_dt(dialog.finished_at)}")
    print()

    for m in msgs:
        tg = users.get(m.from_user_id)
        content = m.text if m.text else (f"<photo_id={m.photo_id}>" if m.photo_id else "<empty>")
        print(f"[{_fmt_dt(m.created_at)}] from tg={tg} (user_id={m.from_user_id}) :: {content}")


async def dump_user_messages(session, tg_id: int, limit: int | None) -> None:
    res_u = await session.execute(select(User).where(User.telegram_id == tg_id))
    u = res_u.scalar_one_or_none()
    if u is None:
        print(f"User with tg_id={tg_id} not found")
        return

    q = (
        select(DbMessage)
        .where(DbMessage.from_user_id == u.id)
        .order_by(DbMessage.created_at.desc())
    )
    res_m = await session.execute(q)
    msgs = res_m.scalars().all()
    if limit:
        msgs = msgs[:limit]

    print(f"=== Messages by tg_id={tg_id} (user_id={u.id}) ===")
    print(f"total: {len(msgs)}")
    print()

    # show oldest -> newest for readability
    for m in reversed(msgs):
        content = m.text if m.text else (f"<photo_id={m.photo_id}>" if m.photo_id else "<empty>")
        print(f"[{_fmt_dt(m.created_at)}] dialog={m.dialog_id} :: {content}")


async def main() -> None:
    load_dotenv()
    settings = Settings()

    engine = create_engine(settings.database_dsn)
    session_factory = create_sessionmaker(engine)

    parser = argparse.ArgumentParser(description="Dump messages from DB")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dialog", type=int, help="Dialog ID")
    g.add_argument("--user", type=int, help="User Telegram ID")
    parser.add_argument("--limit", type=int, default=None, help="Limit messages (last N)")
    args = parser.parse_args()

    async with session_factory() as session:
        if args.dialog is not None:
            await dump_dialog(session, args.dialog, args.limit)
        if args.user is not None:
            await dump_user_messages(session, args.user, args.limit)


if __name__ == "__main__":
    asyncio.run(main())
