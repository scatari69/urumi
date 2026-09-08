import logging
from datetime import timezone

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (Message, MessageOriginChannel, MessageOriginChat,
                           MessageOriginHiddenUser, MessageOriginUser)

from bot.filters import ActiveChat
from core.db import insert_message

logger = logging.getLogger(__name__)

router = Router(name="logger")

# Placeholder so a captionless photo still leaves readable text in history/summaries
# instead of a NULL that downstream f-string formatting would render as "None".
PHOTO_PLACEHOLDER = "[фото]"


def _forward_origin_name(message: Message) -> str | None:
    """Who a forwarded message was originally written by, per Bot API 7.0's unified
    forward_origin (a MessageOrigin union) — None if this isn't a forward."""
    origin = message.forward_origin
    if origin is None:
        return None

    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name
    if isinstance(origin, MessageOriginChannel):
        name = origin.chat.title or "канал"
        return f"{name} ({origin.author_signature})" if origin.author_signature else name
    if isinstance(origin, MessageOriginChat):
        name = origin.sender_chat.title or "группа"
        return f"{name} ({origin.author_signature})" if origin.author_signature else name
    return "неизвестного автора"


def extract_text(message: Message) -> str:
    """Text to store/show for a message: caption or a placeholder for a photo, with a
    forward marker prepended when applicable — so the model sees who actually wrote a
    forwarded message instead of crediting it to whoever hit "forward"."""
    body = message.text or message.caption or (
        PHOTO_PLACEHOLDER if message.photo else "[медиа или нетекстовое сообщение]"
    )
    author = _forward_origin_name(message)
    if author is None:
        return body
    return f"[переслано, автор: {author}] {body}"


@router.message(ActiveChat(), F.text | F.photo)
async def log_message(message: Message) -> None:
    if message.from_user is None:
        raise SkipHandler

    await insert_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
        text=extract_text(message),
        reply_to_message_id=(
            message.reply_to_message.message_id if message.reply_to_message else None
        ),
        ts=int(message.date.astimezone(timezone.utc).timestamp()),
    )

    raise SkipHandler
