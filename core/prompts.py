DEFAULT_SYSTEM_PROMPT = "Ты участник группового чата. Отвечай коротко и по делу."

DEFAULT_SUMMARY_PROMPT = (
    "Сделай пересказ беседы: краткий дайджест тем, кто что обсуждал, "
    "в чём мнения разошлись и какие решения приняли. Пиши по-русски, без разметки."
)


DEFAULT_PROFILE_PROMPT = (
    "Обнови заметку об участнике чата по его сообщениям. "
    "Опиши: стиль общения, интересы, роль в чате, над чем иронизирует.\n"
    "Формат: 3-5 коротких пунктов списка, до 600 символов, по-русски.\n"
    "Не выдумывай фактов, которых нет в сообщениях.\n"
    "Не сохраняй контакты, адреса и другие персональные данные.\n"
    "Если новые наблюдения противоречат старым, оставляй новые.\n"
    "Верни только текст заметки, без пояснений."
)


def base_system_prompt(values: dict[str, str]) -> str:
    """Persona prompt without any mood fragment.

    Moods apply to chat replies only; summaries and profile updates use this as-is.
    """
    return values.get("system_prompt") or DEFAULT_SYSTEM_PROMPT


def summary_prompt(values: dict[str, str]) -> str:
    return values.get("summary_prompt") or DEFAULT_SUMMARY_PROMPT


def profile_prompt(values: dict[str, str]) -> str:
    return values.get("profile_prompt") or DEFAULT_PROFILE_PROMPT
