import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.filters import ActiveChat
from core.config import settings
from core.profiles import get_profile, opt_out

logger = logging.getLogger(__name__)

router = Router(name="profile")

NO_REPLY_MESSAGE = "Ответь этой командой на сообщение участника, чью заметку нужно показать."
NO_PROFILE_MESSAGE = "Заметки об этом участнике пока нет."
OPTED_OUT_MESSAGE = "Участник отказался от профилирования."
FORGET_DONE_MESSAGE = "Заметка удалена. Профиль для тебя больше не собирается."


@router.message(
    ActiveChat(),
    Command("profile"),
    F.from_user.id.in_(settings.ADMIN_USER_IDS),
)
async def show_profile(message: Message) -> None:
    target = message.reply_to_message
    if target is None or target.from_user is None:
        await message.reply(NO_REPLY_MESSAGE)
        return

    profile = await get_profile(message.chat.id, target.from_user.id)
    if profile is None:
        await message.reply(NO_PROFILE_MESSAGE)
        return

    display_name, notes, updated_at, opted_out_flag = profile
    if opted_out_flag:
        await message.reply(OPTED_OUT_MESSAGE)
        return
    if not notes:
        await message.reply(NO_PROFILE_MESSAGE)
        return

    header = f"Заметка о {display_name or target.from_user.full_name}:"
    await message.reply(html.escape(f"{header}\n{notes}", quote=False), parse_mode="HTML")


@router.message(ActiveChat(), Command("forgetme"))
async def forget_me(message: Message) -> None:
    if message.from_user is None:
        return

    await opt_out(message.chat.id, message.from_user.id, message.from_user.full_name)
    logger.info("User %s opted out of profiling", message.from_user.id)
    await message.reply(FORGET_DONE_MESSAGE)
