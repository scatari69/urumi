import logging
from datetime import timezone

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message

from bot.filters import ActiveChat
from core.db import insert_message

logger = logging.getLogger(__name__)

router = Router(name="logger")

# Placeholder so a captionless photo still leaves readable text in history/summaries
# instead of a NULL that downstream f-string formatting would render as "None".
PHOTO_PLACEHOLDER = "[фото]"


@router.message(ActiveChat(), F.text | F.photo)
async def log_message(message: Message) -> None:
    if message.from_user is None:
        raise SkipHandler

    await insert_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
        text=message.text or message.caption or PHOTO_PLACEHOLDER,
        reply_to_message_id=(
            message.reply_to_message.message_id if message.reply_to_message else None
        ),
        ts=int(message.date.astimezone(timezone.utc).timestamp()),
    )

    raise SkipHandler
