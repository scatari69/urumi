"""Run with the project dependencies: python -m unittest discover -s tests."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# Configure before importing modules that construct Settings; never use real services.
os.environ.update(BOT_TOKEN="123456:test", OPENROUTER_API_KEY="test",
                  ADMIN_PASSWORD="test", DB_PATH=f"{tempfile.gettempdir()}/urumi-forwards-test.db")

from aiogram.types import Message, User
from aiogram.dispatcher.event.bases import SkipHandler
from bot.handlers import chat, logger
from core.prompts import FORWARD_FORMAT_NOTE, base_system_prompt


def message(**kwargs):
    return Message.model_validate({
        "message_id": 1, "date": 1,
        "chat": {"id": -100, "type": "supergroup"},
        "from": {"id": 10, "is_bot": False, "first_name": "Переславший"},
        **kwargs,
    })


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_origins_and_storage(self):
        origins = [
            ({"type": "user", "sender_user": {"id": 20, "is_bot": False,
              "first_name": "Автор"}}, "Автор"),
            ({"type": "hidden_user", "sender_user_name": "Скрытый"}, "Скрытый"),
            ({"type": "chat", "sender_chat": {"id": -200, "type": "supergroup",
              "title": "Группа"}, "author_signature": "Редактор"}, "Группа (Редактор)"),
            ({"type": "channel", "chat": {"id": -300, "type": "channel",
              "title": "Канал"}, "message_id": 7, "author_signature": "Редактор"},
             "Канал (Редактор)"),
        ]
        for origin, name in origins:
            with self.subTest(origin=origin["type"]):
                forwarded = message(text="Чужой текст", forward_origin={"date": 1, **origin})
                expected = f"[переслано, автор: {name}] Чужой текст"
                self.assertEqual(logger.extract_text(forwarded), expected)
                with patch.object(logger, "insert_message", new_callable=AsyncMock) as insert:
                    with self.assertRaises(SkipHandler):
                        await logger.log_message(forwarded)
                    self.assertEqual(insert.call_args.kwargs["text"], expected)
                    self.assertEqual(insert.call_args.kwargs["user_id"], 10)
        self.assertEqual(logger.extract_text(message(text="Свой текст")), "Свой текст")
        photo = message(photo=[{"file_id": "p", "file_unique_id": "p", "width": 1,
                                "height": 1}], forward_origin={"date": 1, **origins[0][0]})
        self.assertEqual(logger.extract_text(photo), "[переслано, автор: Автор] [фото]")
        self.assertEqual(logger.extract_text(photo.model_copy(update={"caption": "Подпись"})),
                         "[переслано, автор: Автор] Подпись")

    async def test_reply_context_without_history(self):
        forwarded = message(text="Я переехал", forward_origin={
            "type": "hidden_user", "date": 1, "sender_user_name": "Автор"})
        current = message(text="@testbot что думаешь?", reply_to_message=forwarded)
        bot = AsyncMock()
        bot.me.return_value = User(id=99, is_bot=True, first_name="Бот", username="testbot")
        with (patch.object(chat, "get_chat_settings", AsyncMock(return_value={"context_messages": "0"})),
              patch.object(chat, "resolve_current", AsyncMock(return_value=None)),
              patch.object(chat, "_fetch_history", AsyncMock(return_value=[])),
              patch.object(chat, "_fetch_notes", AsyncMock(return_value=[])),
              patch.object(chat, "_acquire_reply_slot", return_value=True),
              patch.object(chat.llm_client, "chat", AsyncMock(return_value="")) as llm):
            await chat.reply_in_chat(current, bot)
        payload = llm.call_args.args[0]
        self.assertIn(FORWARD_FORMAT_NOTE, payload[0]["content"])
        self.assertIn("[переслано, автор: Автор] Я переехал", payload[1]["content"])
        self.assertIn("@testbot что думаешь?", payload[1]["content"])
        self.assertIn(FORWARD_FORMAT_NOTE, base_system_prompt({"system_prompt": "Свои правила"}))
