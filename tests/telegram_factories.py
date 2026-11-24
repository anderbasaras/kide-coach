"""Builders for real python-telegram-bot objects used in tests. All ids are invented."""

from __future__ import annotations

import datetime as dt

from telegram import CallbackQuery, Chat, Message, Update, User

WHEN = dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.UTC)
ALLOWED_CHAT = 111111
STRANGER_CHAT = 999999


def tg_user(user_id: int = ALLOWED_CHAT) -> User:
    return User(id=user_id, first_name="Alex", is_bot=False)


def tg_chat(chat_id: int = ALLOWED_CHAT, chat_type: str = Chat.PRIVATE) -> Chat:
    return Chat(id=chat_id, type=chat_type)


def message(
    chat_id: int = ALLOWED_CHAT, text: str | None = "hello", chat_type: str = Chat.PRIVATE
) -> Message:
    return Message(
        message_id=1,
        date=WHEN,
        chat=tg_chat(chat_id, chat_type),
        from_user=tg_user(chat_id),
        text=text,
    )


def message_update(chat_id: int = ALLOWED_CHAT, text: str | None = "hello", **kwargs) -> Update:
    return Update(update_id=1, message=message(chat_id, text, **kwargs))


def edited_update(chat_id: int = ALLOWED_CHAT, text: str = "edited") -> Update:
    return Update(update_id=2, edited_message=message(chat_id, text))


def callback_update(chat_id: int = ALLOWED_CHAT, data: str = "x") -> Update:
    query = CallbackQuery(
        id="1",
        from_user=tg_user(chat_id),
        chat_instance="instance",
        message=message(chat_id, "What's on today?"),
        data=data,
    )
    return Update(update_id=3, callback_query=query)
