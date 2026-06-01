"""Временные (in-memory) сессии разговора.

По умолчанию разговор НЕ сохраняется в БД — живёт только здесь и теряется
при перезапуске бота. Сохранение в именованный чат делается командой /save.
"""

from dataclasses import dataclass, field

from . import config

# Мягкий предел числа сообщений, удерживаемых в памяти на сессию.
_MEM_CAP = 200

# Встроенный пресет-промт о форматировании (markdown) и языке ответа.
# Модель пишет обычный markdown, бот конвертирует его в Telegram MarkdownV2.
TG_FORMAT_PROMPT = (
    "Always reply in the SAME LANGUAGE the user writes in. "
    "Format your answer with standard Markdown so it looks clean in a chat: "
    "**bold**, *italic*, ~~strikethrough~~, `inline code`, fenced ``` code blocks ```, "
    "bullet lists with '- ', numbered lists with '1. ', and [text](url) for links. "
    "Do NOT use tables or HTML tags — they are not supported here. "
    "Keep messages concise and easy to read on a phone screen."
)
TG_FORMAT_NAME = "📱 Формат Telegram + язык"


@dataclass
class Prompt:
    """Системный промт в библиотеке: можно включать/выключать по отдельности."""
    text: str
    active: bool = True
    preset: bool = False  # встроенный (нельзя удалить, можно вкл/выкл и править)
    name: str = ""

    def label(self) -> str:
        base = self.name or (self.text[:40] + ("…" if len(self.text) > 40 else ""))
        return ("✅ " if self.active else "⬜ ") + base


def default_prompts() -> list[Prompt]:
    return [Prompt(text=TG_FORMAT_PROMPT, active=True, preset=True, name=TG_FORMAT_NAME)]


@dataclass
class Session:
    model: str = config.DEFAULT_MODEL
    # Библиотека промтов: каждый со своим флагом active.
    prompts: list["Prompt"] = field(default_factory=default_prompts)
    temperature: float = 0.7
    messages: list[dict] = field(default_factory=list)
    # Если сессия загружена из сохранённого чата — его id и название.
    saved_chat_id: int | None = None
    saved_title: str | None = None

    def add(self, role: str, content) -> None:
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > _MEM_CAP:
            self.messages = self.messages[-_MEM_CAP:]

    def is_empty(self) -> bool:
        return not self.messages

    def custom_text(self) -> str:
        """Активные ПОЛЬЗОВАТЕЛЬСКИЕ промты (без пресетов) — для подкрепления."""
        return "\n\n".join(p.text for p in self.prompts if p.active and not p.preset)

    def effective_system(self) -> str:
        """Итоговый системный промт: все активные промты, объединённые."""
        return "\n\n".join(p.text for p in self.prompts if p.active)


_sessions: dict[int, Session] = {}

# Пароли пользователей для шифрования сохранённых чатов. Хранятся ТОЛЬКО в
# памяти процесса, в БД не пишутся и пропадают при перезапуске бота.
_passphrases: dict[int, str] = {}


def set_passphrase(user_id: int, passphrase: str) -> None:
    _passphrases[user_id] = passphrase


def clear_passphrase(user_id: int) -> None:
    _passphrases.pop(user_id, None)


def get_passphrase(user_id: int) -> str | None:
    return _passphrases.get(user_id)


def get_session(user_id: int) -> Session:
    s = _sessions.get(user_id)
    if s is None:
        s = Session()
        _sessions[user_id] = s
    return s


def reset_session(user_id: int) -> Session:
    """Начать новый пустой временный разговор."""
    s = Session()
    _sessions[user_id] = s
    return s


def load_into_session(
    user_id: int,
    chat_id: int,
    title: str,
    model: str,
    system_prompt: str,
    temperature: float,
    messages: list[dict],
) -> Session:
    """Загрузить сохранённый чат в текущую сессию."""
    prompts = default_prompts()
    if system_prompt:
        prompts.append(Prompt(text=system_prompt, active=True, name="Из сохранённого чата"))
    s = Session(
        model=model,
        prompts=prompts,
        temperature=temperature,
        messages=list(messages),
        saved_chat_id=chat_id,
        saved_title=title,
    )
    _sessions[user_id] = s
    return s
