"""Общие помощники для хендлеров."""

from ..db import db
from ..session import Session, get_session, prompt_from_dict, prompt_to_dict


async def active_session(user_id: int) -> Session:
    """Текущая сессия пользователя с загруженной из БД библиотекой промтов."""
    await db.ensure_user(user_id)
    session = get_session(user_id)
    if not session.prompts_loaded:
        data = await db.load_prompts(user_id)
        if data:
            session.prompts = [prompt_from_dict(d) for d in data]
        else:
            # Первый раз — сохраняем дефолтную библиотеку (с пресетом формата).
            await db.save_prompts(user_id, [prompt_to_dict(p) for p in session.prompts])
        session.prompts_loaded = True
    return session


async def persist_prompts(user_id: int, session: Session) -> None:
    """Сохранить библиотеку промтов пользователя в БД (после изменения)."""
    await db.save_prompts(user_id, [prompt_to_dict(p) for p in session.prompts])
