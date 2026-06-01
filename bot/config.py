"""Конфигурация из .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env)")
    return val


BOT_TOKEN: str = _require("BOT_TOKEN")
# Дефолтный ключ — общий для всех, кто не задал свой через /setkey.
DEFAULT_OPENROUTER_KEY: str = _require("OPENROUTER_API_KEY")
# Секрет для шифрования данных в БД (ключи юзеров, тексты сообщений).
# Сгенерировать: python -c "import secrets; print(secrets.token_urlsafe(48))"
ENCRYPTION_KEY: str = _require("ENCRYPTION_KEY")

DB_PATH: str = os.getenv("DB_PATH", "orbot.db")
# TTL кэша моделей (часы). Основное обновление — ночным планировщиком;
# этот TTL лишь страхует от устаревания, если планировщик не сработал.
MODELS_CACHE_TTL_HOURS: float = float(os.getenv("MODELS_CACHE_TTL_HOURS", "25"))
# Сколько последних не-system сообщений отправлять модели.
HISTORY_MAX_MESSAGES: int = int(os.getenv("HISTORY_MAX_MESSAGES", "20"))
# Модель по умолчанию для новых чатов. openrouter/free — мета-модель,
# которая всегда доступна и маршрутизирует только на бесплатные модели.
DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "openrouter/free")

# Режим отладки: подробные логи payload (для тест-инстанса). НЕ включать в проде
# (политика no-logs). Команда /debug доступна всегда.
DEBUG: bool = os.getenv("DEBUG", "0") in ("1", "true", "True", "yes")
