import asyncio
import json
import logging
import time

import httpx

from core.config import settings
from core.db import delete_setting, get_setting, set_setting

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 3
BACKOFF_START_SECONDS = 2.0
MAX_CONCURRENCY = 2

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
QUOTA_EXHAUSTED_STATUS_CODES = {402, 403}
MODEL_NOT_FOUND_STATUS_CODES = {404}


MODELS_URL = f"{OPENROUTER_BASE_URL}/models"
MODELS_CACHE_TTL_SECONDS = 3600
MODELS_CACHE_KEY = "models_cache"
MODELS_FETCH_TIMEOUT_SECONDS = 30.0

_models_cache: list[dict] | None = None
_models_cached_at: float = 0.0


class LLMQuotaError(Exception):
    """Raised when OpenRouter reports quota/access exhaustion (402/403)."""


class LLMModelsUnavailable(Exception):
    """Raised when the model list cannot be fetched and nothing is cached."""


class LLMModelNotFound(Exception):
    """Raised on 404 — the model id is unknown, or dropped off the free tier."""


class LLMUnavailableError(Exception):
    """Raised when a retryable error (429/5xx) still fails after MAX_ATTEMPTS."""


def resolve_model(values: dict[str, str], key: str) -> str:
    """Per-task model from settings, defaulting to the configured one."""
    return values.get(key) or settings.MODEL


def parse_fallbacks(values: dict[str, str]) -> list[str]:
    raw = values.get("fallback_models") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalize_model(raw: dict) -> dict:
    pricing = raw.get("pricing") or {}
    architecture = raw.get("architecture") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or raw.get("id"),
        "context_length": raw.get("context_length"),
        "pricing": {
            "prompt": pricing.get("prompt"),
            "completion": pricing.get("completion"),
        },
        "architecture": {"modality": architecture.get("modality")},
    }


def is_free_model(model: dict) -> bool:
    """Free means a zero prompt price. Compared as a float: OpenRouter sends prices
    as strings, and not every free model carries the ':free' suffix."""
    try:
        return float(model["pricing"]["prompt"]) == 0.0
    except (KeyError, TypeError, ValueError):
        return False


async def _fetch_models() -> list[dict]:
    headers = {
        "HTTP-Referer": "https://github.com/urumi-the-bot",
        "X-Title": "Urumi",
    }
    if settings.OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {settings.OPENROUTER_API_KEY}"

    async with httpx.AsyncClient(timeout=MODELS_FETCH_TIMEOUT_SECONDS) as client:
        response = await client.get(MODELS_URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    return [_normalize_model(item) for item in payload.get("data", []) if item.get("id")]


async def _load_persisted_models() -> tuple[list[dict], float] | None:
    raw = await get_setting(MODELS_CACHE_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return payload["models"], float(payload["fetched_at"])
    except (ValueError, TypeError, KeyError):
        logger.warning("Discarding malformed %s", MODELS_CACHE_KEY)
        return None


async def list_models(force: bool = False) -> list[dict]:
    """OpenRouter's model list, cached in memory for an hour and mirrored into the
    settings table so the cache survives a restart."""
    global _models_cache, _models_cached_at

    now = time.time()

    if not force and _models_cache is not None and now - _models_cached_at < MODELS_CACHE_TTL_SECONDS:
        return _models_cache

    if not force:
        persisted = await _load_persisted_models()
        if persisted and now - persisted[1] < MODELS_CACHE_TTL_SECONDS:
            _models_cache, _models_cached_at = persisted
            return _models_cache

    try:
        models = await _fetch_models()
    except Exception as exc:
        logger.warning("Failed to fetch OpenRouter models: %s", exc)
        # A stale list beats no list at all.
        stale = await _load_persisted_models()
        if stale:
            _models_cache, _models_cached_at = stale
            return _models_cache
        if _models_cache is not None:
            return _models_cache
        raise LLMModelsUnavailable(f"{type(exc).__name__}: {exc}") from exc

    _models_cache, _models_cached_at = models, now
    await set_setting(MODELS_CACHE_KEY, json.dumps({"fetched_at": now, "models": models}))
    logger.info("Fetched %d models from OpenRouter", len(models))
    return models


async def invalidate_models_cache() -> None:
    global _models_cache, _models_cached_at
    _models_cache = None
    _models_cached_at = 0.0
    await delete_setting(MODELS_CACHE_KEY)


class LLMClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self.request_count = 0
        self.error_count = 0
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_request_at: float | None = None
        self.last_used_model: str | None = None
        self.last_used_at: float | None = None

    def status(self) -> dict:
        return {
            "request_count": self.request_count,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_request_at": self.last_request_at,
            "last_used_model": self.last_used_model,
            "last_used_at": self.last_used_at,
            "default_model": settings.MODEL,
            "started": self._client is not None,
        }

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=OPENROUTER_BASE_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://github.com/urumi-the-bot",
                "X-Title": "Urumi",
            },
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        fallbacks: list[str] | None = None,
    ) -> str:
        """Send a completion request. The model is always explicit — never read from config.

        If the primary model is out of quota (402/403), unknown (404), or still failing
        after retries (429/5xx), the fallbacks are tried in order.
        """
        if self._client is None:
            raise RuntimeError("LLMClient.start() must be called before chat()")
        if not model:
            raise ValueError("chat() requires an explicit model id")

        candidates: list[str] = []
        for candidate in [model, *(fallbacks or [])]:
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        for index, candidate in enumerate(candidates):
            is_last = index == len(candidates) - 1
            try:
                answer = await self._attempt(candidate, messages, temperature, max_tokens)
            except (LLMQuotaError, LLMModelNotFound, LLMUnavailableError) as exc:
                if is_last:
                    raise
                logger.warning(
                    "Model %s unavailable (%s), falling back to %s",
                    candidate, type(exc).__name__, candidates[index + 1],
                )
                continue

            self.last_used_model = candidate
            self.last_used_at = time.time()
            if index:
                logger.warning(
                    "Request served by fallback model %s (primary %s failed)", candidate, model
                )
            return answer

        raise AssertionError("unreachable")

    async def _attempt(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None,
        max_tokens: int | None,
    ) -> str:
        payload: dict = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        self.request_count += 1
        self.last_request_at = time.time()

        try:
            async with self._semaphore:
                response = await self._post_with_retries(payload)

            data = response.json()
            return data["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            self.error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_error_at = time.time()
            raise

    async def _post_with_retries(self, payload: dict) -> httpx.Response:
        delay = BACKOFF_START_SECONDS

        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = await self._client.post("/chat/completions", json=payload)

            if response.status_code in QUOTA_EXHAUSTED_STATUS_CODES:
                raise LLMQuotaError(
                    f"OpenRouter quota/access error {response.status_code}: {response.text}"
                )

            if response.status_code in MODEL_NOT_FOUND_STATUS_CODES:
                raise LLMModelNotFound(
                    f"OpenRouter model not found {response.status_code}: {response.text}"
                )

            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt < MAX_ATTEMPTS:
                    logger.warning(
                        "OpenRouter request failed with %d (attempt %d/%d), retrying in %.0fs",
                        response.status_code,
                        attempt,
                        MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise LLMUnavailableError(
                    f"OpenRouter request still failing with {response.status_code} "
                    f"after {MAX_ATTEMPTS} attempts: {response.text}"
                )

            response.raise_for_status()
            return response

        raise AssertionError("unreachable")


llm_client = LLMClient()
