"""Builders for real python-telegram-bot objects used in tests. All ids are invented."""

from __future__ import annotations

import datetime as dt

from telegram import (
    CallbackQuery,
    Chat,
    Document,
    Message,
    MessageEntity,
    PhotoSize,
    Sticker,
    Update,
    User,
    Voice,
)

WHEN = dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.UTC)
ALLOWED_CHAT = 111111
STRANGER_CHAT = 999999


def tg_user(user_id: int = ALLOWED_CHAT) -> User:
    return User(id=user_id, first_name="Alex", is_bot=False)


def tg_chat(chat_id: int = ALLOWED_CHAT, chat_type: str = Chat.PRIVATE) -> Chat:
    return Chat(id=chat_id, type=chat_type)


def message(
    chat_id: int = ALLOWED_CHAT,
    text: str | None = "hello",
    chat_type: str = Chat.PRIVATE,
    message_id: int = 1,
    **fields,
) -> Message:
    if text and text.startswith("/") and "entities" not in fields:
        # Telegram marks commands with an entity; CommandHandler and filters.COMMAND need it.
        command = text.split()[0]
        fields["entities"] = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len(command))]
    return Message(
        message_id=message_id,
        date=WHEN,
        chat=tg_chat(chat_id, chat_type),
        from_user=tg_user(chat_id),
        text=text,
        **fields,
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


def photo_sizes(unique_id: str) -> list[PhotoSize]:
    """Telegram sends every photo in several sizes, smallest first."""
    return [
        PhotoSize(f"{unique_id}-s", f"{unique_id}-small", 90, 90, file_size=1_000),
        PhotoSize(f"{unique_id}-l", unique_id, 1280, 960, file_size=200_000),
    ]


def photo_update(unique_id: str = "PH1", media_group_id: str | None = None) -> Update:
    msg = message(text=None, photo=photo_sizes(unique_id), media_group_id=media_group_id)
    return Update(update_id=4, message=msg)


def document_update(
    unique_id: str = "DOC1", mime_type: str = "image/png", file_size: int = 500_000
) -> Update:
    doc = Document(
        f"{unique_id}-id",
        unique_id,
        file_name="board.png",
        mime_type=mime_type,
        file_size=file_size,
    )
    return Update(update_id=5, message=message(text=None, document=doc))


def sticker_update() -> Update:
    sticker = Sticker("st", "ST1", 512, 512, False, False, Sticker.REGULAR)
    return Update(update_id=6, message=message(text=None, sticker=sticker))


def voice_update() -> Update:
    voice = Voice("vo", "VO1", duration=3)
    return Update(update_id=7, message=message(text=None, voice=voice))
