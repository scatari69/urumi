import logging

from aiogram import Router
from aiogram.types import ChatMemberUpdated

from core.db import register_chat

logger = logging.getLogger(__name__)

router = Router(name="membership")

LEFT_STATUSES = {"left", "kicked"}


@router.my_chat_member()
async def on_membership_change(event: ChatMemberUpdated) -> None:
    """Fires regardless of privacy mode — it's how a new group gets on the /chats
    list at all, before the admin has activated it (or the bot could even see any
    of its messages)."""
    if event.chat.type not in ("group", "supergroup"):
        return

    if event.new_chat_member.status in LEFT_STATUSES:
        logger.info("Bot removed from chat %s (%s)", event.chat.id, event.chat.title)
        return

    if event.old_chat_member.status in LEFT_STATUSES:
        await register_chat(event.chat.id, event.chat.title)
        logger.info(
            "Bot added to chat %s (%s) — pending activation in the admin panel",
            event.chat.id, event.chat.title,
        )
