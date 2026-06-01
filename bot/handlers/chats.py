"""Управление разговором: новый временный, сохранение, список сохранённых."""

import html
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import crypto
from ..db import db
from ..keyboards import BTN_HELP, BTN_NEW, BTN_SAVE, BTN_SAVED, main_menu
from .settings import Form
from ..session import (
    clear_passphrase,
    get_passphrase,
    get_session,
    load_into_session,
    reset_session,
    set_passphrase,
)

router = Router()

_HELP = (
    "<b>RouterAi</b> — чат с бесплатными моделями OpenRouter.\n\n"
    "💡 По умолчанию разговор <b>временный</b> и нигде не сохраняется. "
    "Чтобы сохранить его в именованный чат — команда /save.\n\n"
    "<b>Разговор</b>\n"
    "/new — начать новый временный разговор\n"
    "/save &lt;название&gt; — сохранить текущий разговор\n"
    "/saved — список сохранённых чатов (загрузить/удалить)\n"
    "/reset — очистить текущий разговор\n"
    "/password &lt;пароль&gt; — шифровать сохранённые чаты своим паролем "
    "🔒 (его не знает даже сервер); /password - сбросить\n\n"
    "<b>Настройки</b>\n"
    "/models — выбрать модель (🟢≥95% 🟡80–95% 🔴&lt;80% по аптайму)\n"
    "/system &lt;текст&gt; — системный промт (как модель себя ведёт)\n"
    "/temp &lt;0–2&gt; — температура (точность ↔ творческость ответов)\n"
    "/setkey &lt;ключ&gt; — свой OpenRouter-ключ\n\n"
    "Просто напишите сообщение — отвечает активная модель.\n"
    "Внизу есть кнопки для быстрого доступа к действиям.\n\n"
    "📣 Новости и анонсы — в моём "
    "<a href=\"https://t.me/obebi4\">Telegram-канале</a>.\n"
    "💻 Исходный код проекта открыт на "
    "<a href=\"https://github.com/Obebe11/RouteAI\">GitHub</a>."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await db.ensure_user(message.from_user.id)
    reset_session(message.from_user.id)
    await message.answer(_HELP, reply_markup=main_menu(), disable_web_page_preview=True)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP, reply_markup=main_menu(), disable_web_page_preview=True)


@router.message(Command("new"))
@router.message(F.text == BTN_NEW)
async def cmd_new(message: Message) -> None:
    # /new сразу стирает текущий разговор без подтверждения.
    reset_session(message.from_user.id)
    await message.answer("🆕 Новый разговор. Прошлый стёрт (сохраняйте важное через /save).")


# ---- сохранение ----------------------------------------------------------


@router.message(Command("password"))
async def cmd_password(message: Message, state: FSMContext) -> None:
    arg = message.text.partition(" ")[2].strip()
    if not arg:
        # Без аргумента — кнопочный ввод (как с ключом).
        await state.set_state(Form.password)
        await message.answer(
            "🔒 <b>Пароль для шифрования сохранённых чатов</b> (zero-knowledge).\n"
            "Отправьте пароль одним сообщением — его не знает даже сервер.\n"
            "«<code>-</code>» — сбросить пароль. /cancel — отмена."
        )
        return
    # Пароль передан прямо в команде — удаляем сообщение.
    try:
        await message.delete()
    except Exception:
        pass
    if arg == "-":
        clear_passphrase(message.from_user.id)
        await message.answer(
            "🔓 Пароль сброшен (из памяти). Новые /save шифруются общим ключом сервера."
        )
        return
    set_passphrase(message.from_user.id, arg)
    await message.answer(
        "🔐 Пароль принят (только в памяти). Теперь /save шифрует чат им — "
        "без него его не прочитает никто, включая владельца сервера."
    )


@router.message(Command("save"))
@router.message(F.text == BTN_SAVE)
async def cmd_save(message: Message) -> None:
    session = get_session(message.from_user.id)
    if session.is_empty():
        await message.answer("Пока нечего сохранять — разговор пуст.")
        return

    title = ""
    if message.text and message.text.startswith("/save"):
        title = message.text.partition(" ")[2].strip()
    if not title:
        title = session.saved_title or f"Чат {datetime.now():%d.%m %H:%M}"

    # Если задан пароль — шифруем чат ключом из него (zero-knowledge).
    passphrase = get_passphrase(message.from_user.id)
    fernet = salt = verifier = None
    if passphrase:
        salt = crypto.new_salt()
        fernet = crypto.derive_fernet(passphrase, salt)
        verifier = crypto.make_verifier(fernet)

    was_update = session.saved_chat_id is not None
    chat_id = await db.save_session(
        message.from_user.id,
        title,
        session.model,
        session.custom_text(),
        session.temperature,
        session.messages,
        chat_id=session.saved_chat_id,
        fernet=fernet,
        salt=salt,
        verifier=verifier,
    )
    session.saved_chat_id = chat_id
    session.saved_title = title
    verb = "обновлён" if was_update else "сохранён"
    lock_note = " 🔒 под паролем" if passphrase else ""
    await message.answer(
        f"💾 Разговор {verb} как «{html.escape(title)}»{lock_note} "
        f"({len(session.messages)} сообщ.). Открыть позже — /saved."
    )


# ---- список сохранённых --------------------------------------------------


def _saved_keyboard(chats: list, active_id: int | None) -> InlineKeyboardMarkup:
    rows = []
    for c in chats:
        mark = "✅ " if c["id"] == active_id else ""
        lock = "🔒 " if c["locked"] else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{mark}{lock}{c['title']} · {c['model'].split('/')[-1]}",
                callback_data=f"load:{c['id']}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"del:{c['id']}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("saved", "chats"))
@router.message(F.text == BTN_SAVED)
async def cmd_saved(message: Message) -> None:
    await db.ensure_user(message.from_user.id)
    chats = await db.list_chats(message.from_user.id)
    if not chats:
        await message.answer(
            "Сохранённых чатов нет. Сохраните текущий разговор командой /save."
        )
        return
    active = get_session(message.from_user.id).saved_chat_id
    await message.answer(
        "Сохранённые чаты — нажмите, чтобы загрузить, 🗑 чтобы удалить:",
        reply_markup=_saved_keyboard(chats, active),
    )


@router.callback_query(F.data.startswith("load:"))
async def cb_load(call: CallbackQuery) -> None:
    chat_id = int(call.data.split(":", 1)[1])
    chat = await db.get_chat(chat_id, call.from_user.id)
    if not chat:
        await call.answer("Чат не найден.", show_alert=True)
        return

    fernet = None
    if chat["locked"]:
        passphrase = get_passphrase(call.from_user.id)
        if not passphrase:
            await call.answer()
            await call.message.answer(
                f"🔒 Чат «{html.escape(chat['title'])}» защищён паролем. Введите его "
                "командой <code>/password ваш-пароль</code> и снова откройте чат."
            )
            return
        fernet = crypto.derive_fernet(passphrase, chat["salt"])
        if not crypto.check_verifier(fernet, chat["verifier"]):
            await call.answer()
            await call.message.answer(
                "❌ Неверный пароль для этого чата. Задайте правильный через "
                "<code>/password ...</code> и попробуйте снова."
            )
            return

    messages = await db.get_messages(chat_id, fernet=fernet)
    load_into_session(
        call.from_user.id,
        chat_id,
        chat["title"],
        chat["model"],
        chat["system_prompt"],
        chat["temperature"],
        messages,
    )
    chats = await db.list_chats(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=_saved_keyboard(chats, chat_id))
    await call.answer(f"Загружен «{chat['title']}»")
    await call.message.answer(
        f"📂 Загружен чат «{html.escape(chat['title'])}» ({len(messages)} сообщ.). "
        "Продолжайте — /save обновит его."
    )


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery) -> None:
    chat_id = int(call.data.split(":", 1)[1])
    chat = await db.get_chat(chat_id, call.from_user.id)
    if not chat:
        await call.answer("Уже удалён.", show_alert=True)
        return
    await db.delete_chat(chat_id)
    # Если удалили чат, привязанный к текущей сессии — отвязываем.
    session = get_session(call.from_user.id)
    if session.saved_chat_id == chat_id:
        session.saved_chat_id = None
        session.saved_title = None
    chats = await db.list_chats(call.from_user.id)
    if chats:
        await call.message.edit_reply_markup(
            reply_markup=_saved_keyboard(chats, session.saved_chat_id)
        )
    else:
        await call.message.edit_text("Сохранённых чатов больше нет.")
    await call.answer(f"Удалён «{chat['title']}»")


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    session = get_session(message.from_user.id)
    session.messages.clear()
    await message.answer("Текущий разговор очищен (история сообщений сброшена).")
