"""Основной обработчик: текст/фото пользователя → ответ модели (стрим).

Разговор хранится только в памяти (сессии) и НЕ пишется в БД до /save.
"""

import asyncio
import base64
import time
from io import BytesIO

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from .. import config
from ..openrouter import OpenRouterError
from ..runtime import client, user_api_key
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


async def _respond(message: Message, session: Session, user_content) -> None:
    """Добавить сообщение пользователя в сессию и отдать ответ модели стримом."""
    user_id = message.from_user.id
    session.add("user", user_content)
    payload = _build_messages(session)

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
            session.model, payload, api_key=key, temperature=session.temperature
        ):
            acc += chunk
            await flush()
    except OpenRouterError as exc:
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


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    session = await active_session(message.from_user.id)
    await _respond(message, session, message.text)


def _strip_image(content) -> str:
    """Текстовая выжимка из мультимодального content (без base64-картинки)."""
    if isinstance(content, list):
        text = next(
            (i.get("text") for i in content
             if isinstance(i, dict) and i.get("type") == "text"),
            "",
        )
        return f"[изображение] {text}".strip()
    return content


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    session = await active_session(message.from_user.id)

    # Скачиваем самое крупное превью фото В ПАМЯТЬ (на диск не пишем).
    photo = message.photo[-1]
    buf = BytesIO()
    await message.bot.download(photo, destination=buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    buf.close()
    data_url = f"data:image/jpeg;base64,{b64}"

    # Приватность: сразу удаляем сообщение с фото из чата.
    try:
        await message.delete()
    except Exception:
        pass

    caption = (message.caption or "").strip() or "Что на этом изображении?"
    content = [
        {"type": "text", "text": caption},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    await _respond(message, session, content)

    # Приватность: убираем base64-картинку из памяти сессии после ответа,
    # оставляя только текстовую пометку (в RAM картинка больше не висит).
    for m in session.messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            m["content"] = _strip_image(m["content"])
