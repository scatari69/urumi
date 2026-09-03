from aiogram.filters import BaseFilter
from aiogram.types import Message

from core.db import get_active_chat_ids


class ActiveChat(BaseFilter):
    """True for a chat an admin has activated on the /chats page. Replaces the old
    static F.chat.id == settings.GROUP_CHAT_ID now that the bot serves more than one."""

    async def __call__(self, message: Message) -> bool:
        return message.chat.id in await get_active_chat_ids()
