"""Общие помощники для хендлеров."""

from aiogram.types import Message

from ..db import db
from ..models_cache import RANDOM_MODEL_ID, RANDOM_MODEL_LABEL
from ..runtime import models_cache
from ..session import Session, get_session, prompt_from_dict, prompt_to_dict


async def active_session(user_id: int) -> Session:
    """Сессия пользователя с загруженными из БД настройками (промты/модель/темп.)."""
    await db.ensure_user(user_id)
    session = get_session(user_id)
    if not session.prompts_loaded:
        data = await db.load_prompts(user_id)
        if data:
            session.prompts = [prompt_from_dict(d) for d in data]
        else:
            # Первый раз — сохраняем дефолтную библиотеку (с пресетом формата).
            await db.save_prompts(user_id, [prompt_to_dict(p) for p in session.prompts])
        # Сохранённые модель и температура пользователя.
        user = await db.get_user(user_id)
        if user is not None:
            if user["pref_model"]:
                session.model = user["pref_model"]
            if user["pref_temperature"] is not None:
                session.temperature = user["pref_temperature"]
        session.prompts_loaded = True
    return session


async def persist_prompts(user_id: int, session: Session) -> None:
    """Сохранить библиотеку промтов пользователя в БД (после изменения)."""
    await db.save_prompts(user_id, [prompt_to_dict(p) for p in session.prompts])


async def ensure_model_still_free(message: Message, session: Session) -> None:
    """Если выбранная модель перестала быть бесплатной — переключить на «случайную».

    Защита от случайной тарификации: пользователь мог выбрать модель давно,
    а позже OpenRouter снял с неё бесплатный статус. Список моделей содержит
    только актуальные бесплатные, поэтому сверяем session.model с ним перед
    каждым запросом и автоматически снимаем устаревший выбор.
    """
    if session.model == RANDOM_MODEL_ID:
        return
    snapshot = models_cache.snapshot()
    if not snapshot or any(m["id"] == session.model for m in snapshot):
        return
    old_model = session.model
    session.model = RANDOM_MODEL_ID
    await db.save_pref_model(message.from_user.id, RANDOM_MODEL_ID)
    await message.answer(
        f"⚠️ Модель <code>{old_model}</code> больше не бесплатна и была заменена на "
        f"{RANDOM_MODEL_LABEL}. Выберите другую в «🤖 Модели»."
    )
