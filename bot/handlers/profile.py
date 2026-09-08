import html
import logging

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, User

from bot.filters import ActiveChat
from core.config import settings
from core.db import clear_chat_history
from core.llm import LLMModelNotFound, LLMQuotaError, LLMUnavailableError
from core.profiles import get_profile, opt_out, rebuild_profile

logger = logging.getLogger(__name__)

router = Router(name="profile")

NO_REPLY_MESSAGE = "Ответь этой командой на сообщение участника, чью заметку нужно показать."
NO_PROFILE_MESSAGE = "Заметки об этом участнике пока нет."
OPTED_OUT_MESSAGE = "Участник отказался от профилирования."
FORGET_DONE_MESSAGE = "Заметка удалена. Профиль для тебя больше не собирается."

FORGET_CONTEXT_CONFIRM_ARG = "confirm"
FORGET_CONTEXT_WARNING = (
    "Это необратимо удалит всю сохранённую историю сообщений этого чата — контекст "
    "для ответов, пересказов и заметок об участниках. Сами заметки и профили не "
    "трогает (для них — /forgetme или админка). Чтобы подтвердить: "
    "/forgetcontext confirm"
)


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

    await _reply_profile(message, target.from_user)


async def _reply_profile(message: Message, user: User) -> None:
    profile = await get_profile(message.chat.id, user.id)
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

    header = f"Заметка о {display_name or user.full_name}:"
    await message.reply(html.escape(f"{header}\n{notes}", quote=False), parse_mode="HTML")


@router.message(
    ActiveChat(),
    Command("createprofile"),
)
async def create_profile(message: Message) -> None:
    if message.from_user is None or message.sender_chat is not None or message.from_user.is_bot:
        await message.reply("Отправь команду от своего имени, а не от имени группы или канала.")
        return
    target = message.reply_to_message or message
    if target.from_user is None or target.sender_chat is not None or target.from_user.is_bot:
        await message.reply("Ответь этой командой на сообщение участника, чей профиль нужно создать.")
        return
    if target.from_user.id != message.from_user.id and message.from_user.id not in settings.ADMIN_USER_IDS:
        await message.reply("Можно создать только свой профиль. Отправь /createprofile без ответа на чужое сообщение.")
        return

    profile = await get_profile(message.chat.id, target.from_user.id)
    if profile is not None and profile[3]:
        await message.reply(OPTED_OUT_MESSAGE)
        return

    await message.reply("Собираю заметку по сохранённой истории этого чата…")
    try:
        updated = await rebuild_profile(message.chat.id, target.from_user.id)
    except (LLMModelNotFound, LLMQuotaError, LLMUnavailableError, httpx.HTTPError):
        logger.exception("Profile creation failed for user %s in chat %s", target.from_user.id, message.chat.id)
        await message.reply("Не удалось получить заметку от модели. Попробуй позже.")
        return

    if not updated:
        await message.reply("Заметка не создана: нет подходящих сообщений или модель вернула пустой ответ.")
        return
    await _reply_profile(message, target.from_user)


@router.message(ActiveChat(), Command("forgetme"))
async def forget_me(message: Message) -> None:
    if message.from_user is None:
        return

    await opt_out(message.chat.id, message.from_user.id, message.from_user.full_name)
    logger.info("User %s opted out of profiling", message.from_user.id)
    await message.reply(FORGET_DONE_MESSAGE)


@router.message(
    ActiveChat(),
    Command("forgetcontext"),
    F.from_user.id.in_(settings.ADMIN_USER_IDS),
)
async def forget_context(message: Message, command: CommandObject) -> None:
    """Bot-admin-only (ADMIN_USER_IDS), not a Telegram chat-admin check: wipes this
    chat's message history, i.e. the context fed into replies/summaries/profiles."""
    if (command.args or "").strip().lower() != FORGET_CONTEXT_CONFIRM_ARG:
        await message.reply(FORGET_CONTEXT_WARNING)
        return

    deleted = await clear_chat_history(message.chat.id)
    logger.info(
        "Admin %s cleared context for chat %s: %d message(s) deleted",
        message.from_user.id, message.chat.id, deleted,
    )
    await message.reply(f"Контекст очищен: удалено сообщений — {deleted}.")
