"""Основной обработчик: текст пользователя → ответ активной модели (стрим).

Разговор хранится только в памяти (сессии) и НЕ пишется в БД до /save.
"""

import asyncio
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from .. import config
from ..models_cache import RANDOM_MODEL_ID, pick_random_chat_model
from ..openrouter import OpenRouterError
from ..runtime import client, models_cache, user_api_key
from ..session import Session
from ..utils import clean_response, split_message, trim_history
from .common import active_session

router = Router()

# Не чаще одного редактирования сообщения раз в EDIT_INTERVAL секунд (rate limit TG).
EDIT_INTERVAL = 1.3
TG_LIMIT = 4096


def _build_messages(session: Session) -> list[dict]:
    msgs: list[dict] = []
    if session.system_prompt:
        msgs.append({"role": "system", "content": session.system_prompt})
    msgs.extend(session.messages)
    return trim_history(msgs, config.HISTORY_MAX_MESSAGES)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    user_id = message.from_user.id
    session = await active_session(user_id)

    session.add("user", message.text)
    payload = _build_messages(session)

    # «Случайная модель» — выбираем реальную чат-модель на каждый запрос.
    model = session.model
    if model == RANDOM_MODEL_ID:
        chosen = pick_random_chat_model(models_cache.snapshot())
        model = chosen["id"] if chosen else config.DEFAULT_MODEL

    key = await user_api_key(user_id)
    placeholder = await message.answer("…")
    await message.bot.send_chat_action(message.chat.id, "typing")

    acc = ""
    last_edit = 0.0
    last_shown = ""

    async def flush(force: bool = False) -> None:
        nonlocal last_edit, last_shown
        now = time.monotonic()
        if not force and now - last_edit < EDIT_INTERVAL:
            return
        shown = acc[:TG_LIMIT] or "…"
        if shown == last_shown:
            return
        try:
            await placeholder.edit_text(shown, parse_mode=None)
            last_shown = shown
            last_edit = now
        except TelegramBadRequest:
            pass

    try:
        async for chunk in client.chat_stream(
            model, payload, api_key=key, temperature=session.temperature
        ):
            acc += chunk
            await flush()
    except OpenRouterError as exc:
        # Откатываем последнее сообщение пользователя, чтобы не копить мусор.
        session.messages.pop()
        await placeholder.edit_text(f"⚠️ Ошибка модели:\n{exc}")
        return
    except Exception as exc:  # noqa: BLE001 — не роняем бота на одном запросе
        session.messages.pop()
        await placeholder.edit_text(f"⚠️ Непредвиденная ошибка: {exc}")
        return

    acc = clean_response(acc)
    if not acc.strip():
        session.messages.pop()
        await placeholder.edit_text(
            "Модель вернула пустой ответ. Возможно, она перегружена — "
            "попробуйте ещё раз или смените модель через «Модели»."
        )
        return

    session.add("assistant", acc)

    parts = split_message(acc, TG_LIMIT)
    try:
        await placeholder.edit_text(parts[0], parse_mode=None)
    except TelegramBadRequest:
        pass
    for extra in parts[1:]:
        await asyncio.sleep(0.4)
        await message.answer(extra, parse_mode=None)
