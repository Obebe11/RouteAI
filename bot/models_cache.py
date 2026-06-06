"""Кэш списка актуальных бесплатных моделей OpenRouter с обогащением uptime."""

import asyncio
import json
import time

from .openrouter import OpenRouterClient, OpenRouterError

# Параллелизм запросов uptime, чтобы не упереться в rate-limit free-аккаунта.
_UPTIME_CONCURRENCY = 6
# Сколько дней модель считается «новой» и помечается 🆕.
NEW_MODEL_DAYS = 7
_NEW_MODEL_TTL = NEW_MODEL_DAYS * 86400


class ModelsCache:
    def __init__(self, client: OpenRouterClient, ttl_hours: float, first_seen_path: str = ""):
        self._client = client
        self._ttl = ttl_hours * 3600
        self._models: list[dict] = []
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        self._first_seen_path = first_seen_path
        # {model_id: unix timestamp первого появления}
        self._first_seen: dict[str, float] = self._load_first_seen()

    def _load_first_seen(self) -> dict[str, float]:
        if not self._first_seen_path:
            return {}
        try:
            with open(self._first_seen_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_first_seen(self) -> None:
        if not self._first_seen_path:
            return
        try:
            with open(self._first_seen_path, "w", encoding="utf-8") as f:
                json.dump(self._first_seen, f)
        except OSError:
            pass

    def _mark_new(self, models: list[dict]) -> None:
        """Отмечает models как is_new и обновляет first_seen.

        🆕 ставится только моделям, которые появились ПОСЛЕ того как список уже
        был известен (has_prior). На первом запуске (пустой JSON) все модели
        инициализируются «старой» датой и не получают метку.
        """
        now = time.time()
        has_prior = bool(self._first_seen)
        changed = False
        for m in models:
            mid = m["id"]
            if mid not in self._first_seen:
                # Первый запуск → отмечаем как «давно известные», не NEW.
                # Уже был список → модель новая, ставим текущее время.
                self._first_seen[mid] = now if has_prior else (now - _NEW_MODEL_TTL - 1)
                changed = True
            m["is_new"] = has_prior and (now - self._first_seen[mid]) < _NEW_MODEL_TTL
        if changed:
            self._save_first_seen()

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
            self._mark_new(models)
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


# Мета-модель OpenRouter «случайная»: сама маршрутизирует на случайную
# бесплатную модель. Закреплена вверху списка выбора.
RANDOM_MODEL_ID = "openrouter/free"
RANDOM_MODEL_LABEL = "🎲 Случайная модель"


# Категории моделей: (ключ, подпись для кнопки/заголовка). Порядок = порядок показа.
CATEGORIES: list[tuple[str, str]] = [
    ("text", "💬 Текстовые"),
    ("image", "🖼 Генерация картинок"),
    ("other", "📦 Прочие"),
]
CATEGORY_LABEL = dict(CATEGORIES)


def model_category(m: dict) -> str:
    """Категория модели по её модальностям вывода.

    Любая модель с текстовым выводом (включая vision/мультимодальные) попадает
    в «Текстовые»; vision дополнительно помечается эмодзи 👁 в списке.
    """
    out = set(m.get("output_modalities") or ["text"])
    if "audio" in out:
        return "audio"
    if "image" in out:
        return "image"
    if "text" in out:
        return "text"
    return "other"


def has_vision(m: dict) -> bool:
    """Модель умеет принимать изображения на вход (распознавание фото)."""
    return "image" in (m.get("input_modalities") or ["text"])


def is_chat_category(key: str) -> bool:
    """Категории, с которыми реально работает текстовый чат."""
    return key == "text"


def filter_by_category(models: list[dict], key: str) -> list[dict]:
    return [m for m in models if model_category(m) == key]


def is_image_model(model_id: str, models: list[dict]) -> bool:
    """True if the model outputs images but not text."""
    for m in models:
        if m["id"] == model_id:
            out = set(m.get("output_modalities") or ["text"])
            return "image" in out and "text" not in out
    return False


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
