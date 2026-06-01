"""Общие синглтоны времени выполнения."""

from . import config
from .models_cache import ModelsCache
from .openrouter import OpenRouterClient

client = OpenRouterClient(config.DEFAULT_OPENROUTER_KEY)
models_cache = ModelsCache(client, config.MODELS_CACHE_TTL_HOURS)


async def user_api_key(user_id: int) -> str | None:
    """Личный ключ пользователя (расшифрованный), либо None."""
    from .db import db

    return await db.get_user_key(user_id)
