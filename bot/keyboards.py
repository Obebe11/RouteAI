"""Клавиатуры бота: постоянное меню снизу и метки кнопок."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

# Метки кнопок постоянной клавиатуры. Используются и как фильтры в хендлерах.
BTN_MODELS = "🤖 Модели"
BTN_SAVED = "💬 Сохранённые"
BTN_NEW = "➕ Новый чат"
BTN_SAVE = "💾 Сохранить"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "ℹ️ Помощь"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODELS), KeyboardButton(text=BTN_SAVED)],
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_SAVE)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напишите сообщение или выберите действие…",
    )
