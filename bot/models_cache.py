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

    def snapshot(self) -> list[dict]:
        """Мгновенно вернуть текущий кэш без обращения к сети."""
        return self._models

    async def refresh(self) -> None:
        """Принудительно обновить кэш (для прелоада и ночного обновления)."""
        await self.get(force=True)

    def has(self, model_id: str) -> bool:
        return any(m["id"] == model_id for m in self._models)


# Псевдо-модель «случайная»: закреплена вверху списка. При отправке сообщения
# подменяется на случайную бесплатную чат-модель.
RANDOM_MODEL_ID = "openrouter/free"
RANDOM_MODEL_LABEL = "🎲 Случайная модель"


def pick_random_chat_model(models: list[dict]) -> dict | None:
    """Случайная бесплатная модель, пригодная для текстового чата."""
    import random

    chat = [m for m in models if is_chat_category(model_category(m))]
    return random.choice(chat) if chat else None


# Категории моделей: (ключ, подпись для кнопки/заголовка). Порядок = порядок показа.
CATEGORIES: list[tuple[str, str]] = [
    ("text", "💬 Текстовые"),
    ("multimodal", "👁 Мультимодальные"),
    ("audio", "🎵 Музыка / Аудио"),
    ("image", "🖼 Генерация картинок"),
    ("other", "📦 Прочие"),
]
CATEGORY_LABEL = dict(CATEGORIES)


def model_category(m: dict) -> str:
    """Категория модели по её модальностям ввода/вывода."""
    out = set(m.get("output_modalities") or ["text"])
    inp = set(m.get("input_modalities") or ["text"])
    if "audio" in out:
        return "audio"
    if "image" in out:
        return "image"
    if "text" in out:
        # Текст на выходе: чисто текстовый вход → «текстовые», иначе мультимодальные.
        return "multimodal" if inp - {"text"} else "text"
    return "other"


def has_vision(m: dict) -> bool:
    """Модель умеет принимать изображения на вход (распознавание фото)."""
    return "image" in (m.get("input_modalities") or ["text"])


def is_chat_category(key: str) -> bool:
    """Категории, с которыми реально работает текстовый чат."""
    return key in ("text", "multimodal")


def filter_by_category(models: list[dict], key: str) -> list[dict]:
    return [m for m in models if model_category(m) == key]


def category_counts(models: list[dict]) -> dict[str, int]:
    counts = {k: 0 for k, _ in CATEGORIES}
    for m in models:
        counts[model_category(m)] = counts.get(model_category(m), 0) + 1
    return counts


def uptime_emoji(uptime: float | None) -> str:
    """Цветовой индикатор аптайма: 🟢 ≥95, 🟡 80–95, 🔴 <80, ⚪ неизвестно."""
    if uptime is None:
        return "⚪"
    if uptime >= 95:
        return "🟢"
    if uptime >= 80:
        return "🟡"
    return "🔴"
