import logging
import time

from core.config import settings
from core.db import get_chat_settings, get_db, insert_summary, setting_value
from core.llm import llm_client, parse_fallbacks, resolve_model
from core.prompts import base_system_prompt, summary_prompt

logger = logging.getLogger(__name__)

MIN_MESSAGES = 10
CHUNK_CHAR_LIMIT = 12000
DEFAULT_TEMPERATURE = 0.7

MAP_HEADER = "Фрагмент переписки:"
REDUCE_HEADER = (
    "Ниже — частичные пересказы фрагментов одной беседы. "
    "Сведи их в один общий пересказ по тем же требованиям:"
)


class NotEnoughMessages(Exception):
    def __init__(self, count: int) -> None:
        super().__init__(f"only {count} messages in window")
        self.count = count


async def fetch_window(chat_id: int, period_start: int) -> list[tuple[str | None, str | None]]:
    # No upper bound: message.ts comes from Telegram's clock, so a just-arrived
    # message can read slightly ahead of the local one and would be dropped.
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT display_name, text
            FROM messages
            WHERE chat_id = ? AND ts >= ?
            ORDER BY id
            """,
            (chat_id, period_start),
        )
        return await cursor.fetchall()


def split_into_chunks(lines: list[str], limit: int = CHUNK_CHAR_LIMIT) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1

    if current:
        chunks.append("\n".join(current))
    return chunks


async def _summarize(system_content: str, prompt: str, header: str, body: str,
                     temperature: float, model: str, fallbacks: list[str]) -> str:
    llm_messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": f"{prompt}\n\n{header}\n{body}"},
    ]
    answer = await llm_client.chat(
        llm_messages, model=model, temperature=temperature, fallbacks=fallbacks
    )
    return answer.strip()


async def generate_summary(chat_id: int, hours: int) -> str:
    """Summarize the last `hours` of chat and store the result.

    Raises NotEnoughMessages when the window is too thin, and propagates LLM errors.
    """
    period_end = int(time.time())
    period_start = period_end - hours * 3600

    rows = await fetch_window(chat_id, period_start)
    if len(rows) < MIN_MESSAGES:
        raise NotEnoughMessages(len(rows))

    values = await get_chat_settings(chat_id)
    system_content = base_system_prompt(values)
    prompt = summary_prompt(values)
    temperature = setting_value(values, "temperature", DEFAULT_TEMPERATURE, float)
    model = resolve_model(values, "summary_model")
    fallbacks = parse_fallbacks(values)

    lines = [f"{display_name or 'unknown'}: {text}" for display_name, text in rows]
    chunks = split_into_chunks(lines)
    logger.info(
        "Summarizing %d messages over %dh for chat %s in %d chunk(s)",
        len(rows), hours, chat_id, len(chunks),
    )

    if len(chunks) == 1:
        text = await _summarize(
            system_content, prompt, MAP_HEADER, chunks[0], temperature, model, fallbacks
        )
    else:
        partials = [
            await _summarize(system_content, prompt, MAP_HEADER, chunk, temperature, model, fallbacks)
            for chunk in chunks
        ]
        text = await _summarize(
            system_content, prompt, REDUCE_HEADER, "\n\n".join(partials), temperature, model,
            fallbacks,
        )

    if not text:
        logger.warning("LLM returned an empty summary for chat %s", chat_id)
        return ""

    await insert_summary(
        chat_id=chat_id,
        period_start=period_start,
        period_end=period_end,
        text=text,
        created_at=int(time.time()),
    )
    return text
