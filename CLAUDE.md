# Project
Telegram bot for a single group. Python 3.12, aiogram 3.x, aiosqlite, FastAPI + Jinja2 + htmx for the admin panel.
LLM access via OpenRouter (OpenAI-compatible endpoint), model id read from settings, default google/gemma-3-27b-it:free.

# Rules
- Config only via pydantic-settings + .env. Never hardcode tokens.
- Everything async. No sync I/O inside handlers.
- Layout: bot/ (handlers), core/ (config, db, llm), admin/ (FastAPI). Migrations live in core/db.py as SQL strings.
- Logging via the logging module, level from config.
- Do not add dependencies without a reason. No alembic, no SQLAlchemy — raw SQL through aiosqlite.
