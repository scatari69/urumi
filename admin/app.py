import logging
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin.auth import (NotAuthenticated, check_password, clear_session_cookie,
                        is_authenticated, issue_token, require_auth, set_session_cookie)
from core.config import settings
from core.db import (delete_chat, get_chat, get_chat_settings, get_db, list_chats,
                     register_chat, save_chat_settings, set_chat_active, setting_bool,
                     setting_value)
from core.llm import (LLMModelsUnavailable, invalidate_models_cache, is_free_model,
                      list_models, llm_client, resolve_model)
from core.moods import (ADMIN_ONLY_KEY, SOURCE_ADMIN, TTL_MINUTES_KEY, CannotDeleteDefault,
                        compose_system_prompt, delete_mood, get_mood, list_moods, log_switch,
                        recent_switches, resolve_current, set_current, set_default, upsert_mood)
from core.profiles import delete_profile, list_profiles, rebuild_profile, save_note
from core.prompts import base_system_prompt
from core.summaries import NotEnoughMessages, generate_summary

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

PAGE_SIZE = 50
SETTINGS_KEYS = (
    "system_prompt",
    "summary_prompt",
    "chat_model",
    "summary_model",
    "profile_model",
    "fallback_models",
    "temperature",
    "context_messages",
    "random_reply_chance",
    "mood_admin_only",
    "mood_ttl_minutes",
    "enabled",
)

CHECKBOX_KEYS = {"enabled", "mood_admin_only"}

MODEL_FIELDS = (
    ("chat_model", "chat_model", "ответы в чате — важнее скорость"),
    ("summary_model", "summary_model", "пересказы — важнее длинный контекст"),
    ("profile_model", "profile_model", "заметки об участниках — важнее длинный контекст"),
)

app = FastAPI(title="urumi-the-bot admin")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated) -> Response:
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


async def _known_chat(chat_id: int) -> dict:
    chat = await get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail=f"Chat {chat_id} is not known to this bot.")
    return chat


async def _nav(chat_id: int) -> dict:
    """Context every full-page (non-fragment) template needs for the chat switcher."""
    return {"chat_id": chat_id, "chats": await list_chats()}


def _per_million(price: str | None) -> str:
    """OpenRouter quotes per-token prices as strings; show cost per 1M tokens."""
    try:
        value = float(price) * 1_000_000
    except (TypeError, ValueError):
        return "—"
    if value == 0:
        return "$0"
    return f"${value:,.2f}" if value >= 0.01 else f"${value:.4f}"


def _vendor_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else "other"


def _group_by_vendor(models: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for model in models:
        groups.setdefault(_vendor_of(model["id"]), []).append(model)
    for items in groups.values():
        items.sort(key=lambda m: m["id"])
    return sorted(groups.items())


async def _picker_context(chat_id: int, field: str, q: str, free_only: bool, selected: str,
                          hint: str = "") -> dict:
    """Build one model-picker context, degrading to a plain text input on failure."""
    base = {
        "chat_id": chat_id,
        "field": field,
        "hint": hint,
        "q": q,
        "free_only": free_only,
        "selected": selected,
        "default_model": settings.MODEL,
    }

    try:
        models = await list_models()
    except LLMModelsUnavailable as exc:
        logger.warning("Model list unavailable, falling back to text input: %s", exc)
        return {**base, "groups": [], "error": str(exc), "selected_model": None, "total": 0}

    selected_model = next((m for m in models if m["id"] == selected), None)

    shown = [m for m in models if is_free_model(m)] if free_only else list(models)
    if q:
        needle = q.strip().lower()
        shown = [m for m in shown if needle in m["id"].lower() or needle in (m["name"] or "").lower()]

    # Never drop the current selection from the list, or saving the form would silently
    # switch the model to whatever happens to be first.
    if selected and selected_model is not None and all(m["id"] != selected for m in shown):
        shown.insert(0, selected_model)

    return {
        **base,
        "groups": _group_by_vendor(shown),
        "error": None,
        "selected_model": selected_model,
        "total": len(shown),
    }


def _hint_for(field: str) -> str:
    return next((hint for name, _label, hint in MODEL_FIELDS if name == field), "")


async def _all_pickers(chat_id: int, values: dict[str, str]) -> list[dict]:
    return [
        await _picker_context(chat_id, field, "", False, values.get(field, ""), hint)
        for field, _label, hint in MODEL_FIELDS
    ]


def _like_escape(term: str) -> str:
    """Escape LIKE wildcards so a search for '100%' does not match everything."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fmt_ts(value: float | int | None) -> str:
    if not value:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


templates.env.filters["ts"] = _fmt_ts
templates.env.filters["per_million"] = _per_million


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form("")) -> Response:
    if not check_password(password):
        logger.warning("Failed admin login from %s", request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный пароль."}, status_code=401
        )

    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, issue_token())
    return response


@app.get("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


@app.get("/", dependencies=[Depends(require_auth)])
async def root() -> Response:
    chats = await list_chats()
    active = next((c for c in chats if c["active"]), None)
    target = active or (chats[0] if chats else None)
    if target is None:
        return RedirectResponse("/chats", status_code=303)
    return RedirectResponse(f"/c/{target['chat_id']}/", status_code=303)


# --- Chat management: which groups the bot is allowed to act in at all. Not nested
# under /c/{chat_id} — this page is about the list itself, not one chat's settings.

@app.get("/chats", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def chats_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "chats.html", {"chats": await list_chats(), "flash": None})


async def _chats_fragment(request: Request, flash: str | None = None) -> Response:
    return templates.TemplateResponse(
        request, "_chats_list.html", {"chats": await list_chats(), "flash": flash}
    )


@app.post("/chats/add", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def chat_add(request: Request, chat_id: str = Form(""), title: str = Form("")) -> Response:
    """Manual fallback for the rare case the automatic my_chat_member registration was
    missed (e.g. the bot was offline for more than Telegram's update retention)."""
    chat_id = chat_id.strip()
    if not chat_id.lstrip("-").isdigit():
        return await _chats_fragment(request, "chat_id должен быть целым числом.")

    await register_chat(int(chat_id), title.strip() or None)
    await set_chat_active(int(chat_id), True)
    logger.info("Admin manually added chat %s", chat_id)
    return await _chats_fragment(request, f"Чат {chat_id} добавлен и активирован.")


@app.post("/chats/{chat_id}/activate", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def chat_activate(request: Request, chat_id: int) -> Response:
    await set_chat_active(chat_id, True)
    logger.info("Admin activated chat %s", chat_id)
    return await _chats_fragment(request, f"Чат {chat_id} активирован.")


@app.post("/chats/{chat_id}/deactivate", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def chat_deactivate(request: Request, chat_id: int) -> Response:
    await set_chat_active(chat_id, False)
    logger.info("Admin deactivated chat %s", chat_id)
    return await _chats_fragment(request, f"Чат {chat_id} деактивирован.")


@app.post("/chats/{chat_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def chat_delete(request: Request, chat_id: int) -> Response:
    """Removes the chat and its settings. History/profiles/summaries/mood switches are
    left in place — they age out via HISTORY_TTL_HOURS like any other retained data."""
    await delete_chat(chat_id)
    logger.info("Admin deleted chat %s", chat_id)
    return await _chats_fragment(request, f"Чат {chat_id} удалён из списка.")


# --- Per-chat pages, all nested under /c/{chat_id}.

@app.get("/c/{chat_id}/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def dashboard(request: Request, chat_id: int) -> Response:
    await _known_chat(chat_id)
    since = int(time.time()) - 24 * 3600

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND ts >= ?", (chat_id, since)
        )
        message_count = (await cursor.fetchone())[0]

        cursor = await db.execute(
            """
            SELECT user_id, MAX(display_name), COUNT(*) AS n
            FROM messages
            WHERE chat_id = ? AND ts >= ?
            GROUP BY user_id
            ORDER BY n DESC
            LIMIT 10
            """,
            (chat_id, since),
        )
        top_users = await cursor.fetchall()

        cursor = await db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
        total_messages = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM user_profiles WHERE chat_id = ?", (chat_id,))
        profile_count = (await cursor.fetchone())[0]

    values = await get_chat_settings(chat_id)
    status = llm_client.status()
    status["models"] = [(label, resolve_model(values, field)) for field, label, _ in MODEL_FIELDS]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **await _nav(chat_id),
            "message_count": message_count,
            "total_messages": total_messages,
            "profile_count": profile_count,
            "top_users": top_users,
            "status": status,
            "enabled": (values.get("enabled") or "1").lower() in {"1", "true", "on", "yes"},
            "mood": await resolve_current(values),
            "switches": await recent_switches(chat_id, 10),
        },
    )


@app.get("/c/{chat_id}/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def settings_page(request: Request, chat_id: int) -> Response:
    await _known_chat(chat_id)
    values = await get_chat_settings(chat_id)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            **await _nav(chat_id),
            "values": values,
            "saved": False,
            "default_model": settings.MODEL,
            "pickers": await _all_pickers(chat_id, values),
            "status": llm_client.status(),
        },
    )


@app.post("/c/{chat_id}/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def settings_save(request: Request, chat_id: int) -> Response:
    await _known_chat(chat_id)
    form = await request.form()
    values = {
        key: str(form.get(key, "")).strip()
        for key in SETTINGS_KEYS
        if key not in CHECKBOX_KEYS
    }
    # an unchecked checkbox is simply absent from the form
    for key in CHECKBOX_KEYS:
        values[key] = "1" if form.get(key) else "0"

    await save_chat_settings(chat_id, values)
    logger.info("Admin updated settings for chat %s: %s", chat_id, ", ".join(sorted(values)))

    fresh = await get_chat_settings(chat_id)
    return templates.TemplateResponse(
        request,
        "_settings_form.html",
        {
            "chat_id": chat_id,
            "values": fresh,
            "saved": True,
            "default_model": settings.MODEL,
            "pickers": await _all_pickers(chat_id, fresh),
            "status": llm_client.status(),
        },
    )


def _selected_from(source, field: str) -> str:
    """The select posts its value under the field's own name (chat_model=...),
    so look there first and fall back to a plain 'model' param."""
    return str(source.get(field) or source.get("model") or "")


@app.get("/c/{chat_id}/settings/models", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def models_picker(request: Request, chat_id: int, field: str = "chat_model", q: str = "",
                        free_only: str = "") -> Response:
    selected = _selected_from(request.query_params, field)
    context = await _picker_context(chat_id, field, q, bool(free_only), selected, _hint_for(field))
    return templates.TemplateResponse(request, "_model_picker.html", {"p": context})


@app.post("/c/{chat_id}/settings/models/refresh", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def models_refresh(request: Request, chat_id: int) -> Response:
    form = await request.form()
    field = str(form.get("field") or "chat_model")
    q = str(form.get("q") or "")
    free_only = bool(form.get("free_only"))
    selected = _selected_from(form, field)

    await invalidate_models_cache()
    try:
        await list_models(force=True)
        logger.info("Admin refreshed the OpenRouter model list")
    except LLMModelsUnavailable:
        pass  # _picker_context renders the warning and the text-input fallback

    context = await _picker_context(chat_id, field, q, free_only, selected, _hint_for(field))
    return templates.TemplateResponse(request, "_model_picker.html", {"p": context})


@app.get("/c/{chat_id}/settings/models/info", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def model_info(request: Request, chat_id: int, field: str = "chat_model") -> Response:
    model = _selected_from(request.query_params, field)
    try:
        models = await list_models()
        selected_model = next((m for m in models if m["id"] == model), None)
    except LLMModelsUnavailable:
        selected_model = None

    return templates.TemplateResponse(
        request,
        "_model_info.html",
        {"p": {"chat_id": chat_id, "field": field, "selected_model": selected_model, "selected": model,
               "default_model": settings.MODEL}},
    )


@app.post("/c/{chat_id}/settings/models/test", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def model_test(request: Request, chat_id: int) -> Response:
    """Probe one model. Does not persist it, and deliberately passes no fallbacks so
    the probe reports this model's own failure instead of silently succeeding."""
    form = await request.form()
    field = str(form.get("field") or "chat_model")
    target = _selected_from(form, field) or settings.MODEL
    probe = [{"role": "user", "content": "Ответь одним словом: работает?"}]

    started = time.monotonic()
    try:
        reply = await llm_client.chat(probe, model=target, temperature=0.0, max_tokens=32)
        result = {"ok": True, "reply": reply.strip(), "error": None}
    except Exception as exc:
        logger.warning("Model probe failed for %s: %s", target, exc)
        result = {"ok": False, "reply": None, "error": f"{type(exc).__name__}: {exc}"}

    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    result["model"] = target
    result["field"] = field
    result["chat_id"] = chat_id
    return templates.TemplateResponse(request, "_model_test.html", {"p": result})


@app.get("/c/{chat_id}/messages", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def messages_page(request: Request, chat_id: int, q: str = "", user_id: str = "",
                        page: int = 1) -> Response:
    await _known_chat(chat_id)
    page = max(page, 1)

    where = ["chat_id = ?"]
    params: list = [chat_id]
    if q:
        where.append("text LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(q)}%")
    if user_id.strip().lstrip("-").isdigit():
        where.append("user_id = ?")
        params.append(int(user_id))

    clause = " AND ".join(where)

    async with get_db() as db:
        cursor = await db.execute(f"SELECT COUNT(*) FROM messages WHERE {clause}", params)
        total = (await cursor.fetchone())[0]
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(page, pages)

        cursor = await db.execute(
            f"""
            SELECT id, user_id, display_name, username, text, ts
            FROM messages
            WHERE {clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = await cursor.fetchall()

        cursor = await db.execute(
            """
            SELECT user_id, MAX(display_name), COUNT(*)
            FROM messages WHERE chat_id = ?
            GROUP BY user_id ORDER BY COUNT(*) DESC
            """,
            (chat_id,),
        )
        known_users = await cursor.fetchall()

    return templates.TemplateResponse(
        request,
        "messages.html",
        {
            **await _nav(chat_id),
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "q": q,
            "user_id": user_id,
            "known_users": known_users,
        },
    )


@app.get("/c/{chat_id}/profiles", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def profiles_page(request: Request, chat_id: int) -> Response:
    await _known_chat(chat_id)
    rows = await list_profiles(chat_id)
    return templates.TemplateResponse(
        request, "profiles.html", {**await _nav(chat_id), "rows": rows, "flash": None}
    )


async def _profiles_fragment(request: Request, chat_id: int, flash: str | None = None) -> Response:
    rows = await list_profiles(chat_id)
    return templates.TemplateResponse(
        request, "_profiles_list.html", {"chat_id": chat_id, "rows": rows, "flash": flash}
    )


@app.post("/c/{chat_id}/profiles/{user_id}/notes", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def profile_edit(request: Request, chat_id: int, user_id: int, notes: str = Form("")) -> Response:
    rows = await list_profiles(chat_id)
    existing = next((r for r in rows if r[0] == user_id), None)
    display_name = existing[1] if existing else None

    await save_note(chat_id, user_id, display_name, notes.strip())
    logger.info("Admin edited notes for user %s in chat %s", user_id, chat_id)
    return await _profiles_fragment(request, chat_id, f"Заметка для {display_name or user_id} сохранена.")


@app.post("/c/{chat_id}/profiles/{user_id}/rebuild", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def profile_rebuild(request: Request, chat_id: int, user_id: int) -> Response:
    try:
        await rebuild_profile(chat_id, user_id)
        flash = f"Заметка для {user_id} перестроена."
    except Exception as exc:
        logger.exception("Admin rebuild failed for user %s in chat %s", user_id, chat_id)
        flash = f"Не удалось перестроить: {type(exc).__name__}: {exc}"
    return await _profiles_fragment(request, chat_id, flash)


@app.post("/c/{chat_id}/profiles/{user_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def profile_delete(request: Request, chat_id: int, user_id: int) -> Response:
    await delete_profile(chat_id, user_id)
    logger.info("Admin deleted profile for user %s in chat %s", user_id, chat_id)
    return await _profiles_fragment(request, chat_id, f"Профиль {user_id} удалён.")


async def _moods_context(chat_id: int, flash: str | None = None, preview: dict | None = None) -> dict:
    values = await get_chat_settings(chat_id)
    current = await resolve_current(values)
    return {
        "chat_id": chat_id,
        "moods": await list_moods(),
        "current": current["name"] if current else None,
        "flash": flash,
        "preview": preview,
        "admin_only": setting_bool(values, ADMIN_ONLY_KEY, False),
        "ttl_minutes": setting_value(values, TTL_MINUTES_KEY, 0, int),
    }


async def _moods_fragment(request: Request, chat_id: int, flash: str | None = None,
                          preview: dict | None = None) -> Response:
    return templates.TemplateResponse(
        request, "_moods_list.html", await _moods_context(chat_id, flash, preview)
    )


@app.get("/c/{chat_id}/moods", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def moods_page(request: Request, chat_id: int) -> Response:
    await _known_chat(chat_id)
    return templates.TemplateResponse(
        request, "moods.html", {**await _nav(chat_id), **await _moods_context(chat_id)}
    )


@app.post("/c/{chat_id}/moods/add", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def mood_add(request: Request, chat_id: int, name: str = Form(""), label: str = Form("")) -> Response:
    """The mood catalog is shared across every chat — adding one here makes it
    available (but not active) everywhere, same as editing or deleting one."""
    name = name.strip().lower()
    if not name:
        return await _moods_fragment(request, chat_id, "Имя не может быть пустым.")
    if await get_mood(name) is not None:
        return await _moods_fragment(request, chat_id, f"Настроение {name} уже существует.")

    existing = await list_moods()
    next_order = max((m["sort_order"] or 0) for m in existing) + 10 if existing else 10
    await upsert_mood(name, label.strip() or name, "", None, next_order)
    logger.info("Admin added mood %s", name)
    return await _moods_fragment(request, chat_id, f"Добавлено настроение {name}.")


@app.post("/c/{chat_id}/moods/{name}/save", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def mood_save(request: Request, chat_id: int, name: str, label: str = Form(""),
                    prompt_fragment: str = Form(""), temperature: str = Form(""),
                    sort_order: str = Form("")) -> Response:
    if await get_mood(name) is None:
        return await _moods_fragment(request, chat_id, f"Настроения {name} больше нет.")

    try:
        parsed_temperature = float(temperature) if temperature.strip() else None
    except ValueError:
        return await _moods_fragment(
            request, chat_id, f"temperature должно быть числом (получено {temperature!r})."
        )

    try:
        parsed_order = int(sort_order) if sort_order.strip() else None
    except ValueError:
        return await _moods_fragment(
            request, chat_id, f"sort_order должно быть целым (получено {sort_order!r})."
        )

    await upsert_mood(name, label.strip(), prompt_fragment, parsed_temperature, parsed_order)
    logger.info("Admin edited mood %s", name)
    return await _moods_fragment(request, chat_id, f"Настроение {name} сохранено.")


@app.post("/c/{chat_id}/moods/{name}/delete", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def mood_delete(request: Request, chat_id: int, name: str) -> Response:
    try:
        await delete_mood(name)
    except CannotDeleteDefault:
        return await _moods_fragment(
            request, chat_id, f"{name} — настроение по умолчанию, удалить нельзя."
        )

    logger.info("Admin deleted mood %s", name)
    return await _moods_fragment(request, chat_id, f"Настроение {name} удалено.")


@app.post("/c/{chat_id}/moods/{name}/default", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def mood_make_default(request: Request, chat_id: int, name: str) -> Response:
    if await get_mood(name) is None:
        return await _moods_fragment(request, chat_id, f"Настроения {name} больше нет.")
    await set_default(name)
    logger.info("Admin made mood %s the default", name)
    return await _moods_fragment(request, chat_id, f"{name} теперь по умолчанию.")


@app.post("/c/{chat_id}/moods/{name}/activate", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def mood_activate(request: Request, chat_id: int, name: str) -> Response:
    target = await get_mood(name)
    if target is None:
        return await _moods_fragment(request, chat_id, f"Настроения {name} больше нет.")

    values = await get_chat_settings(chat_id)
    current = await resolve_current(values)
    previous = current["name"] if current else None
    if previous == name:
        return await _moods_fragment(request, chat_id, f"Уже {name}.")

    await set_current(chat_id, name)
    await log_switch(chat_id, None, "admin", previous, name, SOURCE_ADMIN)
    return await _moods_fragment(request, chat_id, f"Включено настроение {name}.")


@app.post("/c/{chat_id}/moods/{name}/preview", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def mood_preview(request: Request, chat_id: int, name: str, prompt_fragment: str = Form("")) -> Response:
    """Assemble base + fragment exactly as chat.py would, using the unsaved textarea text."""
    values = await get_chat_settings(chat_id)
    base = base_system_prompt(values)
    assembled = compose_system_prompt(base, {"prompt_fragment": prompt_fragment})
    return await _moods_fragment(
        request, chat_id, None, {"name": name, "base": base, "assembled": assembled}
    )


async def _fetch_summaries(chat_id: int) -> list:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, period_start, period_end, text, created_at
            FROM summaries
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 50
            """,
            (chat_id,),
        )
        return await cursor.fetchall()


@app.get("/c/{chat_id}/summaries", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def summaries_page(request: Request, chat_id: int) -> Response:
    await _known_chat(chat_id)
    return templates.TemplateResponse(
        request, "summaries.html", {**await _nav(chat_id), "rows": await _fetch_summaries(chat_id), "flash": None}
    )


@app.post("/c/{chat_id}/summaries/generate", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def summaries_generate(request: Request, chat_id: int, hours: int = Form(24)) -> Response:
    hours = max(1, min(hours, settings.HISTORY_TTL_HOURS))
    try:
        text = await generate_summary(chat_id, hours)
        flash = f"Пересказ за {hours} ч. готов." if text else "Модель вернула пустой ответ."
    except NotEnoughMessages as exc:
        flash = f"Слишком мало сообщений ({exc.count})."
    except Exception as exc:
        logger.exception("Admin summary generation failed for chat %s", chat_id)
        flash = f"Ошибка: {type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request, "_summaries_list.html",
        {"chat_id": chat_id, "rows": await _fetch_summaries(chat_id), "flash": flash},
    )
