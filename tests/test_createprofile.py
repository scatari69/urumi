"""Autonomous profile-command checks; Telegram and OpenRouter are mocked."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Message

os.environ.update(BOT_TOKEN="123456:test", OPENROUTER_API_KEY="test", ADMIN_PASSWORD="test",
                  DB_PATH=f"{tempfile.gettempdir()}/urumi-profile-test.db")

from bot.handlers import profile
from core import profiles
from core.config import settings
from core.db import init_db, insert_message


class CreateProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_filters(self):
        handler = next(h for h in profile.router.message.handlers if h.callback is profile.create_profile)
        admins = settings.ADMIN_USER_IDS[:]
        settings.ADMIN_USER_IDS[:] = [10]
        try:
            for user_id, active, expected in [(10, True, True), (11, True, True), (10, False, False)]:
                message = Message.model_validate({
                    "message_id": 1, "date": 1, "text": "/createprofile",
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "Админ"},
                })
                with patch("bot.filters.get_active_chat_ids", AsyncMock(return_value=[-100] if active else [])):
                    allowed, _ = await handler.check(message, bot=AsyncMock())
                self.assertEqual(allowed, expected)
        finally:
            settings.ADMIN_USER_IDS[:] = admins

    async def test_creation_update_isolation_and_opt_out(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, "DB_PATH", f"{directory}/db.sqlite"), patch.object(
            profiles, "get_chat_settings", AsyncMock(return_value={"profile_model": "test-model"})
        ), patch.object(profiles.llm_client, "chat", AsyncMock(return_value="Любит <Python>")) as llm:
            await init_db()
            target = SimpleNamespace(from_user=SimpleNamespace(id=20, is_bot=False, full_name="Имя"), sender_chat=None)
            message = SimpleNamespace(chat=SimpleNamespace(id=-100), reply_to_message=target,
                                      from_user=target.from_user, sender_chat=None, reply=AsyncMock())
            await profile.create_profile(message)
            llm.assert_not_awaited()
            self.assertIn("нет подходящих сообщений", message.reply.call_args.args[0])

            for chat_id, text in [(-100, "Пишу на Python"), (-200, "Секрет другого чата")]:
                await insert_message(chat_id=chat_id, user_id=20, username=None, display_name="Имя",
                                     text=text, reply_to_message_id=None, ts=int(time.time()))
            await profile.create_profile(message)
            self.assertEqual(llm.call_args.kwargs["model"], "test-model")
            self.assertNotIn("Секрет другого чата", str(llm.call_args))
            self.assertIn("&lt;Python&gt;", message.reply.call_args.args[0])
            self.assertIsNone(await profiles.get_profile(-200, 20))
            llm.return_value = "Новая заметка"
            await profile.create_profile(message)
            self.assertEqual((await profiles.get_profile(-100, 20))[1], "Новая заметка")
            llm.return_value = " "
            await profile.create_profile(message)
            self.assertIn("пустой ответ", message.reply.call_args.args[0])
            self.assertEqual((await profiles.get_profile(-100, 20))[1], "Новая заметка")
            llm.side_effect = profiles.LLMUnavailableError("test")
            with self.assertLogs(profile.logger, level="ERROR"):
                await profile.create_profile(message)
            self.assertIn("Попробуй позже", message.reply.call_args.args[0])

            await profiles.opt_out(-100, 20, "Имя")
            llm.reset_mock()
            await profile.create_profile(message)
            self.assertEqual(message.reply.call_args.args[0], profile.OPTED_OUT_MESSAGE)
            self.assertFalse(await profiles.rebuild_profile(-100, 20))
            llm.assert_not_awaited()

    async def test_invalid_target(self):
        with patch.object(profile, "rebuild_profile", AsyncMock()) as rebuild:
            for target in [SimpleNamespace(from_user=None), SimpleNamespace(
                from_user=SimpleNamespace(is_bot=True), sender_chat=None
            ), SimpleNamespace(from_user=SimpleNamespace(is_bot=False), sender_chat=object())]:
                message = SimpleNamespace(reply_to_message=target, reply=AsyncMock(),
                                          from_user=SimpleNamespace(id=20, is_bot=False), sender_chat=None)
                await profile.create_profile(message)
                self.assertIn("Ответь этой командой", message.reply.call_args.args[0])
            rebuild.assert_not_awaited()

    async def test_self_and_other_permissions(self):
        with patch.object(settings, "ADMIN_USER_IDS", [10]), patch.object(
            profile, "get_profile", AsyncMock(return_value=None)
        ) as get_profile, patch.object(profile, "rebuild_profile", AsyncMock(return_value=True)) as rebuild:
            for sender, target_id, allowed in [(20, None, True), (20, 20, True), (20, 30, False), (10, 30, True)]:
                target = None if target_id is None else SimpleNamespace(
                    from_user=SimpleNamespace(id=target_id, is_bot=False), sender_chat=None)
                message = SimpleNamespace(chat=SimpleNamespace(id=-100), reply_to_message=target,
                                          from_user=SimpleNamespace(id=sender, is_bot=False),
                                          sender_chat=None, reply=AsyncMock())
                rebuild.reset_mock()
                get_profile.reset_mock()
                await profile.create_profile(message)
                if allowed:
                    rebuild.assert_awaited_once_with(-100, target_id or sender)
                else:
                    rebuild.assert_not_awaited()
                    get_profile.assert_not_awaited()
                    self.assertIn("только свой профиль", message.reply.call_args.args[0])
            for user, sender_chat in [(None, None), (SimpleNamespace(is_bot=True), None),
                                      (SimpleNamespace(is_bot=False), object())]:
                message.from_user, message.sender_chat = user, sender_chat
                rebuild.reset_mock()
                await profile.create_profile(message)
                rebuild.assert_not_awaited()
                self.assertIn("от своего имени", message.reply.call_args.args[0])
