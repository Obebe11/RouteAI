"""Кэш списка актуальных бесплатных моделей OpenRouter с обогащением uptime."""

import asyncio
import time

from .openrouter import OpenRouterClient, OpenRouterError

# Параллелизм запросов uptime, чтобы не упереться в rate-limit free-аккаунта.
_UPTIME_CONCURRENCY = 6


class ModelsCache:
    def __init__(self, client: OpenRouterClient, ttl_hours: float):
        self._client = client
        self._ttl = ttl_hours * 3600
        self._models: list[dict] = []
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, force: bool = False) -> list[dict]:
        """Список free-моделей (с uptime, отсортирован по убыванию), обновляя по TTL."""
        age = time.monotonic() - self._fetched_at
        if not (force or not self._models or age > self._ttl):
            return self._models
        async with self._lock:
            # Повторная проверка после получения лока.
            age = time.monotonic() - self._fetched_at
            if not (force or not self._models or age > self._ttl):
                return self._models
            try:
                models = await self._client.list_free_models()
            except OpenRouterError:
                if not self._models:
                    raise
                return self._models
            await self._enrich_uptime(models)
            models.sort(key=lambda m: (m.get("uptime") is None, -(m.get("uptime") or 0)))
            self._models = models
            self._fetched_at = time.monotonic()
        return self._models

    async def _enrich_uptime(self, models: list[dict]) -> None:
        sem = asyncio.Semaphore(_UPTIME_CONCURRENCY)

        async def one(m: dict) -> None:
            async with sem:
                m["uptime"] = await self._client.free_endpoint_uptime(m["id"])

        await asyncio.gather(*(one(m) for m in models), return_exceptions=True)

    def has(self, model_id: str) -> bool:
        return any(m["id"] == model_id for m in self._models)


def is_audio_model(m: dict) -> bool:
    """Модель генерирует аудио (а не текст) — для отдельной вкладки."""
    out = m.get("output_modalities") or ["text"]
    return "audio" in out and "text" not in out


def filter_models(models: list[dict], audio: bool) -> list[dict]:
    """audio=True → только аудио-модели; иначе — текстовые (чат)."""
    if audio:
        return [m for m in models if is_audio_model(m)]
    return [m for m in models if not is_audio_model(m)]


def uptime_emoji(uptime: float | None) -> str:
    """Цветовой индикатор аптайма: 🟢 ≥95, 🟡 80–95, 🔴 <80, ⚪ неизвестно."""
    if uptime is None:
        return "⚪"
    if uptime >= 95:
        return "🟢"
    if uptime >= 80:
        return "🟡"
    return "🔴"
