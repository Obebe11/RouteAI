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
from ..models_cache import (
    CATEGORIES,
    CATEGORY_LABEL,
    category_counts,
    filter_by_category,
    has_vision,
    is_chat_category,
    uptime_emoji,
)
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
    rows = [
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
    await _show_categories(call.message, edit=False)
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
