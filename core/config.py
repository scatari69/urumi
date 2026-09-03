from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str
    OPENROUTER_API_KEY: str
    MODEL: str = "google/gemma-3-27b-it:free"
    # Only a one-time bootstrap seed for the first chat on first run — after that,
    # which chats the bot serves is managed entirely via the admin panel's /chats page.
    GROUP_CHAT_ID: int | None = None
    ADMIN_USER_IDS: list[int] = []
    ADMIN_PASSWORD: str
    DB_PATH: str = "data/urumi.db"
    HISTORY_TTL_HOURS: int = 24
    LOG_LEVEL: str = "INFO"
    ADMIN_PORT: int = 8080


settings = Settings()
