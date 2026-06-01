"""Команды/кнопки настройки: модель, аудио-вкладка, промт, температура, ключ."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..db import db
from ..keyboards import BTN_MODELS, BTN_SETTINGS
from ..models_cache import filter_models, is_audio_model, uptime_emoji
from ..openrouter import OpenRouterError
from ..runtime import models_cache, user_api_key
from ..session import get_session
from .common import active_session

router = Router()

_PAGE_SIZE = 8
_TEMP_PRESETS = (0.3, 0.7, 1.0, 1.3)

KEY_GUIDE = (
    "🔑 <b>Как подключить свой ключ OpenRouter — пошагово:</b>\n\n"
    "1️⃣ Откройте <b>openrouter.ai</b> и войдите (Google / GitHub / email).\n"
    "2️⃣ Зайдите в раздел ключей: <b>openrouter.ai/keys</b>\n"
    "3️⃣ Нажмите <b>«Create Key»</b> и задайте имя.\n"
    "4️⃣ В поле <b>«Credit limit»</b> укажите небольшой лимит — например "
    "<b>0.001$</b> (для контроля расходов; бесплатным моделям его хватает).\n"
    "5️⃣ Скопируйте ключ (начинается с <code>sk-or-</code>) и пришлите боту:\n"
    "<code>/setkey sk-or-ваш-ключ</code>\n"
    "   — сообщение с ключом я сразу удалю.\n\n"
    "ℹ️ Вернуться на общий ключ бота: <code>/setkey -</code>"
)


# ---- меню выбора модели (вкладки: текст / аудио) -------------------------


def _model_label(m: dict) -> str:
    emoji = uptime_emoji(m.get("uptime"))
    up = m.get("uptime")
    suffix = f" {round(up)}%" if up is not None else ""
    return f"{emoji} {m['name']}{suffix}"


def _models_keyboard(models: list[dict], audio: bool, page: int) -> InlineKeyboardMarkup:
    a = 1 if audio else 0
    start = page * _PAGE_SIZE
    chunk = models[start:start + _PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(
            text=_model_label(m), callback_data=f"pickmodel:{a}:{i + start}"
        )]
        for i, m in enumerate(chunk)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="« Назад", callback_data=f"models:{a}:{page - 1}"))
    if start + _PAGE_SIZE < len(models):
        nav.append(InlineKeyboardButton(text="Вперёд »", callback_data=f"models:{a}:{page + 1}"))
    if nav:
        rows.append(nav)
    # Переключатель вкладки.
    if audio:
        rows.append([InlineKeyboardButton(text="💬 Текстовые модели", callback_data="models:0:0")])
    else:
        rows.append([InlineKeyboardButton(text="🎵 Аудио-модели", callback_data="models:1:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tab_caption(audio: bool, count: int) -> str:
    if audio:
        return (
            f"🎵 <b>Аудио-модели</b> ({count})\n"
            "Генерируют звук, а не текст — в обычном чате недоступны, "
            "вкладка для ознакомления.\n"
            "🟢 ≥95%  🟡 80–95%  🔴 <80%  ⚪ нет данных"
        )
    return (
        f"💬 <b>Текстовые модели</b> ({count}) — по аптайму ↓\n"
        "🟢 ≥95%  🟡 80–95%  🔴 <80%  ⚪ нет данных\n\n"
        "Выберите модель для текущего разговора:"
    )


async def _show_models(target: Message, audio: bool, page: int, edit: bool) -> None:
    try:
        all_models = await models_cache.get()
    except OpenRouterError as exc:
        await target.answer(f"Не удалось получить список моделей: {exc}")
        return
    models = filter_models(all_models, audio)
    if not models:
        text = "🎵 Сейчас бесплатных аудио-моделей нет." if audio \
            else "Бесплатных текстовых моделей сейчас не найдено."
        if edit:
            await target.edit_text(text)
        else:
            await target.answer(text)
        return
    caption = _tab_caption(audio, len(models))
    kb = _models_keyboard(models, audio, page)
    if edit:
        await target.edit_text(caption, reply_markup=kb)
    else:
        await target.answer(caption, reply_markup=kb)


@router.message(Command("models", "model"))
@router.message(F.text == BTN_MODELS)
async def cmd_models(message: Message) -> None:
    note = await message.answer("Загружаю список моделей и их аптайм…")
    await _show_models(note, audio=False, page=0, edit=True)


@router.callback_query(F.data.startswith("models:"))
async def cb_models_page(call: CallbackQuery) -> None:
    _, a, page = call.data.split(":")
    await _show_models(call.message, audio=(a == "1"), page=int(page), edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("pickmodel:"))
async def cb_pick_model(call: CallbackQuery) -> None:
    _, a, idx = call.data.split(":")
    audio = a == "1"
    all_models = await models_cache.get()
    models = filter_models(all_models, audio)
    idx = int(idx)
    if idx >= len(models):
        await call.answer("Список обновился, откройте «Модели» заново.", show_alert=True)
        return
    model = models[idx]
    session = get_session(call.from_user.id)
    session.model = model["id"]
    note = ""
    if is_audio_model(model):
        note = "\n⚠️ Это аудио-модель — обычный текстовый чат с ней работать не будет."
    await call.message.edit_text(
        f"{uptime_emoji(model.get('uptime'))} Модель → <code>{model['id']}</code>{note}"
    )
    await call.answer("Модель выбрана")


# ---- инлайн-меню настроек -----------------------------------------------


async def _settings_text(user_id: int) -> str:
    session = get_session(user_id)
    key = await user_api_key(user_id)
    key_state = "личный" if key else "общий бота"
    sys_prompt = session.system_prompt or "(не задан)"
    if len(sys_prompt) > 200:
        sys_prompt = sys_prompt[:200] + "…"
    title = session.saved_title or "временный (не сохранён)"
    return (
        f"<b>⚙️ Настройки разговора</b>\n"
        f"Состояние: {title}\n\n"
        f"🤖 Модель: <code>{session.model}</code>\n"
        f"🌡 Температура: {session.temperature}\n"
        f"🔑 Ключ: {key_state}\n"
        f"✏️ Системный промт: {sys_prompt}"
    )


def _settings_keyboard(active_temp: float) -> InlineKeyboardMarkup:
    temp_row = [
        InlineKeyboardButton(
            text=("• " if abs(active_temp - t) < 1e-6 else "") + str(t),
            callback_data=f"settemp:{t}",
        )
        for t in _TEMP_PRESETS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Сменить модель", callback_data="open_models")],
            temp_row,
            [
                InlineKeyboardButton(text="✏️ Системный промт", callback_data="hint_system"),
                InlineKeyboardButton(text="🔑 Свой ключ", callback_data="hint_key"),
            ],
        ]
    )


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message) -> None:
    session = await active_session(message.from_user.id)
    await message.answer(
        await _settings_text(message.from_user.id),
        reply_markup=_settings_keyboard(session.temperature),
    )


@router.callback_query(F.data == "open_models")
async def cb_open_models(call: CallbackQuery) -> None:
    await _show_models(call.message, audio=False, page=0, edit=False)
    await call.answer()


@router.callback_query(F.data.startswith("settemp:"))
async def cb_settemp(call: CallbackQuery) -> None:
    value = float(call.data.split(":", 1)[1])
    session = get_session(call.from_user.id)
    session.temperature = value
    await call.message.edit_text(
        await _settings_text(call.from_user.id),
        reply_markup=_settings_keyboard(value),
    )
    await call.answer(f"Температура → {value}")


@router.callback_query(F.data == "hint_system")
async def cb_hint_system(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(
        "✏️ Задать системный промт:\n<code>/system ваш текст</code>\n"
        "Очистить: <code>/system -</code>"
    )


@router.callback_query(F.data == "hint_key")
async def cb_hint_key(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(KEY_GUIDE, disable_web_page_preview=True)


# ---- текстовые команды настройки ----------------------------------------


@router.message(Command("system"))
async def cmd_system(message: Message) -> None:
    session = await active_session(message.from_user.id)
    text = message.text.partition(" ")[2].strip()
    if not text:
        cur = session.system_prompt or "(пусто)"
        await message.answer(
            f"Текущий системный промт:\n<code>{cur}</code>\n\n"
            "Чтобы задать: <code>/system ваш текст</code>\n"
            "Очистить: <code>/system -</code>"
        )
        return
    session.system_prompt = "" if text == "-" else text
    await message.answer(
        "Системный промт обновлён." if session.system_prompt else "Системный промт очищен."
    )


@router.message(Command("temp"))
async def cmd_temp(message: Message) -> None:
    session = await active_session(message.from_user.id)
    arg = message.text.partition(" ")[2].strip().replace(",", ".")
    if not arg:
        await message.answer(
            f"Текущая температура: {session.temperature}\n"
            "Задать: <code>/temp 0.7</code> (0.0–2.0)"
        )
        return
    try:
        value = float(arg)
        if not 0.0 <= value <= 2.0:
            raise ValueError
    except ValueError:
        await message.answer("Нужно число от 0.0 до 2.0.")
        return
    session.temperature = value
    await message.answer(f"Температура → {value}")


@router.message(Command("setkey"))
async def cmd_setkey(message: Message) -> None:
    key = message.text.partition(" ")[2].strip()
    await db.ensure_user(message.from_user.id)
    if not key or key == "-":
        await db.set_user_key(message.from_user.id, None)
        await message.answer("Личный ключ сброшен — используется общий ключ бота.")
        return
    await db.set_user_key(message.from_user.id, key)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("✅ Ваш OpenRouter-ключ сохранён. Сообщение с ключом удалено.")
