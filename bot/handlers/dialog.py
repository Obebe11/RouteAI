"""Основной обработчик: текст/фото пользователя → ответ модели (стрим).

Разговор хранится только в памяти (сессии) и НЕ пишется в БД до /save.
"""

import asyncio
import base64
import html
import logging
import time
from io import BytesIO

import telegramify_markdown
from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from .. import config
from ..models_cache import is_image_model
from ..openrouter import OpenRouterError
from ..runtime import client, models_cache, user_api_key
from ..session import Session
from ..utils import clean_response, split_message, trim_history
from .common import active_session

router = Router()
log = logging.getLogger("routeai.dialog")

# Не чаще одного редактирования сообщения раз в EDIT_INTERVAL секунд (rate limit TG).
EDIT_INTERVAL = 1.3
TG_LIMIT = 4096


def _reinforce_user(msg: dict, system_prompt: str) -> dict:
    """Вернуть КОПИЮ сообщения пользователя с напоминанием системной инструкции.

    Многие бесплатные модели слабо следуют роли system, поэтому дублируем
    инструкцию прямо в текст последнего запроса (только в исходящем payload,
    сохранённую историю не трогаем).
    """
    prefix = f"(Важно: следуй этой инструкции во всех ответах — {system_prompt})\n\n"
    content = msg["content"]
    if isinstance(content, str):
        return {"role": "user", "content": prefix + content}
    if isinstance(content, list):
        new_items, injected = [], False
        for item in content:
            if not injected and isinstance(item, dict) and item.get("type") == "text":
                new_items.append({"type": "text", "text": prefix + item.get("text", "")})
                injected = True
            else:
                new_items.append(item)
        if not injected:
            new_items.insert(0, {"type": "text", "text": prefix.strip()})
        return {"role": "user", "content": new_items}
    return msg


def _friendly_error(exc: OpenRouterError) -> str:
    """Понятное сообщение об ошибке вместо сырого JSON от OpenRouter."""
    s = str(exc)
    code = s.split(":", 1)[0].strip()
    if code == "429":
        return (
            "⏳ Модель сейчас перегружена (превышен лимит бесплатных запросов).\n"
            "• Попробуйте ещё раз через минуту,\n"
            "• или смените модель в «🤖 Модели»,\n"
            "• или подключите свой ключ OpenRouter (/setkey) — он снимает общие лимиты."
        )
    if code in ("401", "403"):
        return "🔑 Проблема с ключом OpenRouter. Проверьте /setkey или вернитесь на общий ключ."
    if code == "402":
        return "💳 На ключе OpenRouter закончились средства. Смените модель или ключ."
    if code in ("404", "400"):
        return "🚫 Эта модель сейчас недоступна. Выберите другую в «🤖 Модели»."
    return "⚠️ Модель вернула ошибку. Попробуйте ещё раз или смените модель в «🤖 Модели»."


def _build_messages(session: Session) -> list[dict]:
    msgs: list[dict] = []
    system = session.effective_system()
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(session.messages)
    msgs = trim_history(msgs, config.HISTORY_MAX_MESSAGES)
    # Подкрепляем ПОЛЬЗОВАТЕЛЬСКУЮ инструкцию (персону) в последнем запросе —
    # слабые модели плохо держат её из роли system. TG-формат не дублируем.
    custom = session.custom_text()
    if custom:
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                msgs[i] = _reinforce_user(msgs[i], custom)
                break
    return msgs


async def _respond(message: Message, session: Session, user_content) -> None:
    """Добавить сообщение пользователя в сессию и отдать ответ модели стримом."""
    user_id = message.from_user.id
    session.add("user", user_content)
    payload = _build_messages(session)

    if config.DEBUG:
        sys_msgs = [m for m in payload if m["role"] == "system"]
        log.info(
            "[DEBUG] user=%s model=%s active_prompts=%d system_len=%d roles=%s",
            user_id, session.model, sum(p.active for p in session.prompts),
            len(sys_msgs[0]["content"]) if sys_msgs else 0,
            [m["role"] for m in payload],
        )
        if sys_msgs:
            log.info("[DEBUG] system content: %r", sys_msgs[0]["content"][:1000])

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

    attempt = 0
    while True:
        try:
            async for chunk in client.chat_stream(
                session.model, payload, api_key=key, temperature=session.temperature
            ):
                acc += chunk
                await flush()
            break
        except OpenRouterError as exc:
            # 429 (rate-limit) до начала ответа — транзиентно, тихо повторяем.
            if str(exc).startswith("429") and not acc and attempt < 2:
                attempt += 1
                try:
                    await placeholder.edit_text(
                        f"⏳ Модель занята, повтор {attempt}/2…", parse_mode=None
                    )
                except TelegramBadRequest:
                    pass
                await asyncio.sleep(2.5)
                continue
            session.messages.pop()
            await placeholder.edit_text(_friendly_error(exc), parse_mode=None)
            return
        except Exception as exc:  # noqa: BLE001 — не роняем бота на одном запросе
            session.messages.pop()
            await placeholder.edit_text(f"⚠️ Непредвиденная ошибка: {exc}", parse_mode=None)
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

    # Финальный рендер: модель пишет обычный markdown, конвертируем его в
    # корректный Telegram MarkdownV2 (символьное форматирование). Если что-то
    # пойдёт не так — отправляем обычным текстом, чтобы сообщение точно ушло.
    md2 = None
    try:
        md2 = telegramify_markdown.markdownify(acc)
    except Exception:  # noqa: BLE001
        md2 = None

    if md2 and len(md2) <= TG_LIMIT:
        try:
            await placeholder.edit_text(md2, parse_mode=ParseMode.MARKDOWN_V2)
            return
        except TelegramBadRequest:
            pass

    # Фолбэк: обычный текст (с разбивкой длинных ответов).
    parts = split_message(acc, TG_LIMIT)
    try:
        await placeholder.edit_text(parts[0], parse_mode=None)
    except TelegramBadRequest:
        pass
    for extra in parts[1:]:
        await asyncio.sleep(0.4)
        await message.answer(extra, parse_mode=None)


@router.message(Command("debug"))
async def cmd_debug(message: Message) -> None:
    """Показать, что РЕАЛЬНО уходит в модель для текущей сессии."""
    session = await active_session(message.from_user.id)

    lines = ["<b>🔍 Отладка сессии</b>", ""]
    lines.append(f"🤖 Модель: <code>{html.escape(session.model)}</code>")
    lines.append(f"📝 Промтов: {len(session.prompts)} (активных "
                 f"{sum(p.active for p in session.prompts)}):")
    for i, p in enumerate(session.prompts, 1):
        mark = "✅" if p.active else "⬜"
        name = html.escape(p.name or (p.text[:40] + ("…" if len(p.text) > 40 else "")))
        lines.append(f"  {mark} {i}. {name}")

    eff = session.effective_system()
    custom = session.custom_text()
    lines.append("")
    lines.append("<b>Системное сообщение, уходящее в модель:</b>")
    lines.append(f"<code>{html.escape(eff[:1500]) or '(пусто)'}</code>")
    lines.append("")
    lines.append("<b>Подкрепление в последнем запросе (custom):</b>")
    lines.append(f"<code>{html.escape(custom[:500]) or '(нет)'}</code>")

    await message.answer("\n".join(lines))


async def _respond_image(message: Message, session: Session) -> None:
    """Call image generation API and send the resulting photo."""
    prompt = message.text
    key = await user_api_key(message.from_user.id)
    placeholder = await message.answer("🎨 Генерирую изображение…")
    await message.bot.send_chat_action(message.chat.id, "upload_photo")
    try:
        url = await client.image_generate(session.model, prompt, api_key=key)
    except OpenRouterError as exc:
        await placeholder.edit_text(_friendly_error(exc), parse_mode=None)
        return
    except Exception as exc:  # noqa: BLE001
        await placeholder.edit_text(f"⚠️ Ошибка генерации: {exc}", parse_mode=None)
        return
    try:
        await placeholder.delete()
    except Exception:  # noqa: BLE001
        pass
    caption = prompt if len(prompt) <= 200 else prompt[:197] + "…"
    await message.answer_photo(url, caption=caption)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    session = await active_session(message.from_user.id)
    if is_image_model(session.model, models_cache.snapshot()):
        await _respond_image(message, session)
    else:
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

    # Скачиваем самое крупное превью фото В ПАМЯТЬ. На диск/сервер не пишем,
    # сообщение в чате НЕ удаляем — оно остаётся у пользователя.
    photo = message.photo[-1]
    buf = BytesIO()
    await message.bot.download(photo, destination=buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    buf.close()
    data_url = f"data:image/jpeg;base64,{b64}"

    caption = (message.caption or "").strip() or "Что на этом изображении?"
    content = [
        {"type": "text", "text": caption},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    await _respond(message, session, content)

    for m in session.messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            m["content"] = _strip_image(m["content"])
