"""Общие помощники для хендлеров."""

from ..db import db
from ..session import Session, get_session


async def active_session(user_id: int) -> Session:
    """Текущая временная сессия пользователя (создаётся при первом обращении)."""
    await db.ensure_user(user_id)
    return get_session(user_id)
