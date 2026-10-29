from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    consent = State()
    gender = State()
    birth_date = State()
    city = State()
    seek = State()


class ProfileEdit(StatesGroup):
    city = State()
    seek = State()


class Complaint(StatesGroup):
    reason = State()


class AdminPanel(StatesGroup):
    waiting_user_id = State()


class Payment(StatesGroup):
    waiting_email = State()
