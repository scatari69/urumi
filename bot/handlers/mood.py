import html
import logging
import time

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.filters import ActiveChat
from core.config import settings
from core.db import get_chat_settings, setting_bool
from core.moods import (SOURCE_CHAT, SWITCH_COOLDOWN_SECONDS, ADMIN_ONLY_KEY, get_mood,
                        get_default_mood, list_moods, log_switch, resolve_current, set_current)

logger = logging.getLogger(__name__)

router = Router(name="mood")

RESET_KEYWORD = "reset"
DENIED_MESSAGE = "Настроение может менять только админ."
NO_MOODS_MESSAGE = "Настроения не заданы."

_last_switch_at: dict[int, float] = {}


def _acquire_switch_slot(chat_id: int) -> float:
    """Returns 0 when the switch may proceed, otherwise the seconds still to wait."""
    now = time.monotonic()
    last = _last_switch_at.get(chat_id)
    if last is not None and now - last < SWITCH_COOLDOWN_SECONDS:
        return SWITCH_COOLDOWN_SECONDS - (now - last)
    return 0.0


def _names_line(moods: list[dict]) -> str:
    return ", ".join(m["name"] for m in moods)


@router.message(ActiveChat(), Command("mood"))
async def mood_command(message: Message, command: CommandObject) -> None:
    moods = await list_moods()
    if not moods:
        await message.reply(NO_MOODS_MESSAGE)
        return

    values = await get_chat_settings(message.chat.id)
    current = await resolve_current(values)
    requested = (command.args or "").strip().split(" ")[0].strip().lower()

    if not requested:
        label = (current or {}).get("label") or "—"
        name = (current or {}).get("name") or "—"
        await message.reply(
            html.escape(
                f"Сейчас: {name} ({label})\nДоступно: {_names_line(moods)}\n"
                f"Сменить: /mood <name>, вернуть: /mood {RESET_KEYWORD}",
                quote=False,
            ),
            parse_mode="HTML",
        )
        return

    if setting_bool(values, ADMIN_ONLY_KEY, False):
        if message.from_user is None or message.from_user.id not in settings.ADMIN_USER_IDS:
            await message.reply(DENIED_MESSAGE)
            return

    if requested == RESET_KEYWORD:
        target = await get_default_mood()
        if target is None:
            await message.reply(NO_MOODS_MESSAGE)
            return
    else:
        target = await get_mood(requested)
        if target is None:
            # Unknown name must not touch state.
            await message.reply(
                html.escape(
                    f"Нет такого настроения: {requested}\nДоступно: {_names_line(moods)}",
                    quote=False,
                ),
                parse_mode="HTML",
            )
            return

    if current is not None and target["name"] == current["name"]:
        await message.reply(f"Уже {target['name']}.")
        return

    wait = _acquire_switch_slot(message.chat.id)
    if wait:
        await message.reply(f"Настроение уже меняли недавно, подожди {int(wait) + 1} с.")
        return

    _last_switch_at[message.chat.id] = time.monotonic()

    previous = (current or {}).get("name")
    await set_current(message.chat.id, target["name"])
    await log_switch(
        message.chat.id,
        message.from_user.id if message.from_user else None,
        message.from_user.full_name if message.from_user else None,
        previous,
        target["name"],
        SOURCE_CHAT,
    )

    label = target.get("label") or target["name"]
    await message.reply(f"Настроение: {previous or '—'} → {target['name']} ({label})")
