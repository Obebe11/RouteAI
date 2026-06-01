"""Команды/кнопки настройки: модель, аудио-вкладка, промт, температура, ключ."""

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..db import db
from ..keyboards import BTN_MODELS, BTN_SETTINGS
from ..models_cache import (
    CATEGORIES,
    CATEGORY_LABEL,
    RANDOM_MODEL_ID,
    RANDOM_MODEL_LABEL,
    category_counts,
    filter_by_category,
    has_vision,
    is_chat_category,
    uptime_emoji,
)
from ..openrouter import OpenRouterError
from ..runtime import client, models_cache, user_api_key
from ..session import clear_passphrase, get_session, set_passphrase
from .common import active_session


class KeyForm(StatesGroup):
    waiting = State()


class Form(StatesGroup):
    """Состояния ожидания ввода значения после нажатия кнопки."""
    system = State()
    temp = State()
    password = State()

router = Router()

_PAGE_SIZE = 8
_TEMP_PRESETS = (0.3, 0.7, 1.0, 1.3)
# Эмодзи-метки пресетов температуры для наглядности на кнопках.
_TEMP_EMOJI = {0.3: "🎯", 0.7: "⚖️", 1.0: "🎨", 1.3: "🎲"}

TEMP_EXPLAIN = (
    "🌡 <b>Температура</b> — насколько ответы предсказуемые или творческие.\n"
    "• <b>0–0.4</b> 🎯 точные, чёткие, повторяемые (факты, код, инструкции)\n"
    "• <b>0.5–0.9</b> ⚖️ золотая середина для обычного общения\n"
    "• <b>1.0–2.0</b> 🎨 больше фантазии и разнообразия, но менее предсказуемо\n"
    "(идеи, тексты, креатив)"
)

SYSTEM_EXPLAIN = (
    "✏️ <b>Системный промт</b> — постоянная инструкция модели: кто она и как "
    "должна отвечать. Задаётся один раз и действует на весь разговор.\n\n"
    "Примеры:\n"
    "• «Отвечай кратко и только по-русски»\n"
    "• «Ты опытный программист на Python»\n"
    "• «Объясняй простыми словами, как для новичка»"
)


def _temp_word(value: float) -> str:
    if value < 0.5:
        return "🎯 точные"
    if value < 1.0:
        return "⚖️ сбалансированные"
    return "🎨 творческие"

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
    eye = " 👁" if has_vision(m) else ""
    return f"{emoji} {m['name']}{eye}{suffix}"


async def _all_models(target: Message) -> list[dict] | None:
    """Текущий кэш моделей; разовая дозагрузка, если кэш пуст."""
    models = models_cache.snapshot()
    if models:
        return models
    try:
        return await models_cache.get()
    except OpenRouterError as exc:
        await target.answer(f"Не удалось получить список моделей: {exc}")
        return None


# ---- экран выбора категории ---------------------------------------------


def _categories_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    # Закреплённая вверху псевдо-модель «случайная».
    rows = [[InlineKeyboardButton(text=RANDOM_MODEL_LABEL, callback_data="pickrandom")]]
    rows += [
        [InlineKeyboardButton(text=f"{label} ({counts[key]})", callback_data=f"cat:{key}:0")]
        for key, label in CATEGORIES
        if counts.get(key, 0) > 0
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_categories(target: Message, edit: bool) -> None:
    models = await _all_models(target)
    if models is None:
        return
    counts = category_counts(models)
    if not any(counts.values()):
        text = "Бесплатных моделей сейчас не найдено, попробуйте позже."
        await (target.edit_text(text) if edit else target.answer(text))
        return
    caption = (
        f"🗂 <b>Категории моделей</b> (всего {len(models)})\n"
        "Выберите категорию, затем модель. Сортировка по аптайму:\n"
        "🟢 ≥95%  🟡 80–95%  🔴 &lt;80%  ⚪ нет данных"
    )
    kb = _categories_keyboard(counts)
    await (target.edit_text(caption, reply_markup=kb) if edit
           else target.answer(caption, reply_markup=kb))


# ---- экран моделей внутри категории -------------------------------------


def _category_keyboard(models: list[dict], key: str, page: int) -> InlineKeyboardMarkup:
    start = page * _PAGE_SIZE
    chunk = models[start:start + _PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(text=_model_label(m), callback_data=f"pick:{key}:{i + start}")]
        for i, m in enumerate(chunk)
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="« Назад", callback_data=f"cat:{key}:{page - 1}"))
    if start + _PAGE_SIZE < len(models):
        nav.append(InlineKeyboardButton(text="Вперёд »", callback_data=f"cat:{key}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="« Категории", callback_data="cats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_category(target: Message, key: str, page: int, edit: bool) -> None:
    models = await _all_models(target)
    if models is None:
        return
    cat = filter_by_category(models, key)
    if not cat:
        await (target.edit_text("В этой категории сейчас нет моделей.")
               if edit else target.answer("В этой категории сейчас нет моделей."))
        return
    label = CATEGORY_LABEL.get(key, key)
    hint = "" if is_chat_category(key) else \
        "\n⚠️ Модели этой категории не предназначены для текстового чата."
    vision = "\n👁 — модель распознаёт изображения (можно отправлять фото)." \
        if any(has_vision(m) for m in cat) else ""
    caption = (
        f"{label} ({len(cat)}) — по аптайму ↓{hint}{vision}\n\n"
        "Выберите модель для текущего разговора:"
    )
    kb = _category_keyboard(cat, key, page)
    await (target.edit_text(caption, reply_markup=kb) if edit
           else target.answer(caption, reply_markup=kb))


@router.message(Command("models", "model"))
@router.message(F.text == BTN_MODELS)
async def cmd_models(message: Message) -> None:
    await _show_categories(message, edit=False)


@router.callback_query(F.data == "cats")
async def cb_categories(call: CallbackQuery) -> None:
    await _show_categories(call.message, edit=True)
    await call.answer()


@router.callback_query(F.data == "pickrandom")
async def cb_pick_random(call: CallbackQuery) -> None:
    session = get_session(call.from_user.id)
    session.model = RANDOM_MODEL_ID
    await call.message.edit_text(
        f"{RANDOM_MODEL_LABEL} выбрана — на каждый запрос будет случайная "
        "бесплатная чат-модель."
    )
    await call.answer("Случайная модель")


@router.callback_query(F.data.startswith("cat:"))
async def cb_category(call: CallbackQuery) -> None:
    _, key, page = call.data.split(":")
    await _show_category(call.message, key, int(page), edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick_model(call: CallbackQuery) -> None:
    _, key, idx = call.data.split(":")
    cat = filter_by_category(models_cache.snapshot(), key)
    idx = int(idx)
    if idx >= len(cat):
        await call.answer("Список обновился, откройте «Модели» заново.", show_alert=True)
        return
    model = cat[idx]
    session = get_session(call.from_user.id)
    session.model = model["id"]
    note = "" if is_chat_category(key) else \
        "\n⚠️ Эта модель не для текстового чата — обычные сообщения работать не будут."
    await call.message.edit_text(
        f"{uptime_emoji(model.get('uptime'))} Модель → <code>{model['id']}</code>{note}"
    )
    await call.answer("Модель выбрана")


# ---- инлайн-меню настроек -----------------------------------------------


def _prompts_summary(session) -> str:
    if not session.system_prompts:
        return "(не заданы)"
    lines = []
    for i, p in enumerate(session.system_prompts, 1):
        short = p if len(p) <= 80 else p[:80] + "…"
        # Экранируем — текст промта может содержать < > & и сломать HTML-вёрстку.
        short = html.escape(short)
        lines.append(f"{i}. {short}")
    return "\n" + "\n".join(lines)


async def _settings_text(user_id: int) -> str:
    session = get_session(user_id)
    key = await user_api_key(user_id)
    key_state = "личный" if key else "общий бота"
    title = session.saved_title or "временный (не сохранён)"
    model_disp = (
        RANDOM_MODEL_LABEL if session.model == RANDOM_MODEL_ID
        else f"<code>{session.model}</code>"
    )
    tg = "вкл ✅" if session.tg_format else "выкл ❌"
    return (
        f"<b>⚙️ Настройки разговора</b>\n"
        f"Состояние: {title}\n\n"
        f"🤖 Модель: {model_disp}\n"
        f"🌡 Температура: {session.temperature} ({_temp_word(session.temperature)})\n"
        f"🔑 Ключ: {key_state}\n"
        f"📱 Формат Telegram + язык: {tg}\n"
        f"📝 Системные промты ({len(session.system_prompts)}): "
        f"{_prompts_summary(session)}\n"
        "<i>(можно добавить несколько промтов — они объединяются)</i>"
    )


def _settings_keyboard(active_temp: float) -> InlineKeyboardMarkup:
    temp_row = [
        InlineKeyboardButton(
            text=("• " if abs(active_temp - t) < 1e-6 else "")
            + f"{_TEMP_EMOJI[t]} {t}",
            callback_data=f"settemp:{t}",
        )
        for t in _TEMP_PRESETS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Сменить модель", callback_data="open_models")],
            temp_row,
            [InlineKeyboardButton(text="✏️ Своя температура", callback_data="ask_temp")],
            [
                InlineKeyboardButton(text="➕ Добавить промт", callback_data="ask_system"),
                InlineKeyboardButton(text="🗑 Очистить промты", callback_data="clear_system"),
            ],
            [InlineKeyboardButton(text="📱 Формат Telegram вкл/выкл", callback_data="toggle_tg")],
            [
                InlineKeyboardButton(text="🔒 Пароль", callback_data="ask_password"),
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
    await _show_categories(call.message, edit=False)
    await call.answer()


@router.callback_query(F.data.startswith("settemp:"))
async def cb_settemp(call: CallbackQuery) -> None:
    value = float(call.data.split(":", 1)[1])
    session = get_session(call.from_user.id)
    session.temperature = value
    try:
        await call.message.edit_text(
            await _settings_text(call.from_user.id),
            reply_markup=_settings_keyboard(value),
        )
    except TelegramBadRequest:
        # Повторный тап той же температуры — контент не изменился, это норм.
        pass
    await call.answer(f"Температура → {value}")


# ---- кнопочный ввод: системный промт / температура / пароль -------------


@router.callback_query(F.data == "ask_system")
async def cb_ask_system(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.system)
    await call.answer()
    await call.message.answer(
        SYSTEM_EXPLAIN + "\n\n"
        "➕ Отправьте текст промта одним сообщением — он <b>добавится</b> к "
        "уже заданным (их можно несколько, они объединяются).\n"
        "«<code>-</code>» — очистить все. /cancel — отмена."
    )


@router.callback_query(F.data == "clear_system")
async def cb_clear_system(call: CallbackQuery) -> None:
    session = get_session(call.from_user.id)
    session.system_prompts = []
    try:
        await call.message.edit_text(
            await _settings_text(call.from_user.id),
            reply_markup=_settings_keyboard(session.temperature),
        )
    except TelegramBadRequest:
        pass
    await call.answer("Промты очищены")


@router.callback_query(F.data == "toggle_tg")
async def cb_toggle_tg(call: CallbackQuery) -> None:
    session = get_session(call.from_user.id)
    session.tg_format = not session.tg_format
    try:
        await call.message.edit_text(
            await _settings_text(call.from_user.id),
            reply_markup=_settings_keyboard(session.temperature),
        )
    except TelegramBadRequest:
        pass
    await call.answer("Формат Telegram: " + ("вкл" if session.tg_format else "выкл"))


@router.callback_query(F.data == "ask_temp")
async def cb_ask_temp(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.temp)
    await call.answer()
    await call.message.answer(
        TEMP_EXPLAIN + "\n\n"
        "🌡 Отправьте число от <b>0.0</b> до <b>2.0</b> (например 0.8).\n"
        "/cancel — отмена."
    )


@router.callback_query(F.data == "ask_password")
async def cb_ask_password(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.password)
    await call.answer()
    await call.message.answer(
        "🔒 <b>Пароль для шифрования сохранённых чатов</b> (zero-knowledge).\n"
        "Отправьте пароль одним сообщением — его не знает даже сервер.\n"
        "«<code>-</code>» — сбросить пароль. /cancel — отмена."
    )


@router.message(StateFilter(Form.system, Form.temp, Form.password), Command("cancel"))
async def cancel_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Ничего не изменено.")


@router.message(StateFilter(Form.system), F.text & ~F.text.startswith("/"))
async def on_system_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    session = await active_session(message.from_user.id)
    text = message.text.strip()
    if text == "-":
        session.system_prompts = []
        await message.answer("✅ Все системные промты очищены.")
        return
    session.system_prompts.append(text)
    await message.answer(
        f"✅ Промт добавлен (всего {len(session.system_prompts)}). "
        "Можно добавить ещё через «➕ Добавить промт»."
    )


@router.message(StateFilter(Form.temp), F.text & ~F.text.startswith("/"))
async def on_temp_input(message: Message, state: FSMContext) -> None:
    session = await active_session(message.from_user.id)
    arg = message.text.strip().replace(",", ".")
    try:
        value = float(arg)
        if not 0.0 <= value <= 2.0:
            raise ValueError
    except ValueError:
        await message.answer("Нужно число от 0.0 до 2.0. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    session.temperature = value
    await message.answer(f"✅ Температура → {value} ({_temp_word(value)})")


@router.message(StateFilter(Form.password), F.text & ~F.text.startswith("/"))
async def on_password_input(message: Message, state: FSMContext) -> None:
    await state.clear()
    pwd = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    if pwd == "-":
        clear_passphrase(message.from_user.id)
        await message.answer(
            "🔓 Пароль сброшен. Новые /save шифруются общим ключом сервера."
        )
        return
    set_passphrase(message.from_user.id, pwd)
    await message.answer(
        "🔐 Пароль принят (только в памяти). Теперь /save шифрует чат им — "
        "без пароля его не прочитает никто, включая сервер."
    )


def _key_menu_keyboard(has_key: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="⌨️ Ввести ключ", callback_data="key_enter")]]
    if has_key:
        rows.append([InlineKeyboardButton(text="🚫 Отозвать ключ", callback_data="key_revoke")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_key_menu(target: Message, user_id: int) -> None:
    cur = await user_api_key(user_id)
    state_line = (
        "🔑 Сейчас используется: <b>ваш личный ключ</b>."
        if cur else "🔑 Сейчас используется: <b>общий ключ бота</b>."
    )
    await target.answer(
        KEY_GUIDE + "\n\n" + state_line,
        reply_markup=_key_menu_keyboard(bool(cur)),
        disable_web_page_preview=True,
    )


async def _apply_key(message: Message, user_id: int, key: str) -> None:
    """Проверить ключ и установить его, либо оставить общий, если невалиден."""
    note = await message.answer("🔍 Проверяю ключ…")
    valid = await client.validate_key(key)
    if valid:
        await db.set_user_key(user_id, key)
        await note.edit_text("✅ Ключ работает и сохранён — запросы пойдут через него.")
    else:
        await db.set_user_key(user_id, None)
        await note.edit_text(
            "❌ Ключ недействителен (OpenRouter его отклонил). "
            "Оставляю общий ключ бота. Проверьте ключ и попробуйте снова."
        )


@router.callback_query(F.data == "hint_key")
async def cb_hint_key(call: CallbackQuery) -> None:
    await call.answer()
    await _show_key_menu(call.message, call.from_user.id)


@router.callback_query(F.data == "key_enter")
async def cb_key_enter(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(KeyForm.waiting)
    await call.answer()
    await call.message.answer(
        "⌨️ Пришлите ваш ключ OpenRouter одним сообщением "
        "(начинается с <code>sk-or-</code>).\n"
        "Я проверю его и удалю сообщение с ключом.\n\n"
        "Отмена — /cancel",
    )


@router.callback_query(F.data == "key_revoke")
async def cb_key_revoke(call: CallbackQuery) -> None:
    await db.set_user_key(call.from_user.id, None)
    await call.answer("Ключ отозван")
    await call.message.answer("🚫 Личный ключ отозван — используется общий ключ бота.")


@router.message(StateFilter(KeyForm.waiting), Command("cancel"))
async def cmd_key_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Ключ не изменён.")


@router.message(StateFilter(KeyForm.waiting), F.text & ~F.text.startswith("/"))
async def on_key_input(message: Message, state: FSMContext) -> None:
    key = message.text.strip()
    await state.clear()
    # Удаляем сообщение с ключом, чтобы оно не осталось в чате.
    try:
        await message.delete()
    except Exception:
        pass
    await db.ensure_user(message.from_user.id)
    await _apply_key(message, message.from_user.id, key)


# ---- текстовые команды настройки ----------------------------------------


@router.message(Command("system"))
async def cmd_system(message: Message, state: FSMContext) -> None:
    session = await active_session(message.from_user.id)
    text = message.text.partition(" ")[2].strip()
    if not text:
        # Без аргумента — запускаем кнопочный ввод.
        await state.set_state(Form.system)
        cur = html.escape(session.custom_text() or "(пусто)")
        await message.answer(
            SYSTEM_EXPLAIN + "\n\n"
            f"Сейчас ({len(session.system_prompts)} шт.): <code>{cur}</code>\n\n"
            "➕ Отправьте текст — он добавится. «<code>-</code>» — очистить все. /cancel — отмена."
        )
        return
    if text == "-":
        session.system_prompts = []
        await message.answer("✅ Все системные промты очищены.")
        return
    session.system_prompts.append(text)
    await message.answer(f"✅ Промт добавлен (всего {len(session.system_prompts)}).")


@router.message(Command("temp"))
async def cmd_temp(message: Message, state: FSMContext) -> None:
    session = await active_session(message.from_user.id)
    arg = message.text.partition(" ")[2].strip().replace(",", ".")
    if not arg:
        await state.set_state(Form.temp)
        await message.answer(
            TEMP_EXPLAIN + "\n\n"
            f"Сейчас: {session.temperature} ({_temp_word(session.temperature)})\n"
            "🌡 Отправьте число от 0.0 до 2.0. /cancel — отмена."
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
    await message.answer(f"✅ Температура → {value}")


@router.message(Command("setkey"))
async def cmd_setkey(message: Message) -> None:
    key = message.text.partition(" ")[2].strip()
    await db.ensure_user(message.from_user.id)
    if key == "-":
        await db.set_user_key(message.from_user.id, None)
        await message.answer("🚫 Личный ключ отозван — используется общий ключ бота.")
        return
    if not key:
        # Без аргумента — показываем инструкцию и кнопки.
        await _show_key_menu(message, message.from_user.id)
        return
    # Ключ передан прямо в команде — удаляем сообщение и проверяем.
    try:
        await message.delete()
    except Exception:
        pass
    await _apply_key(message, message.from_user.id, key)
