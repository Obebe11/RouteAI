"""Временные (in-memory) сессии разговора.

По умолчанию разговор НЕ сохраняется в БД — живёт только здесь и теряется
при перезапуске бота. Сохранение в именованный чат делается командой /save.
"""

from dataclasses import dataclass, field

from . import config

# Мягкий предел числа сообщений, удерживаемых в памяти на сессию.
_MEM_CAP = 200


@dataclass
class Session:
    model: str = config.DEFAULT_MODEL
    system_prompt: str = ""
    temperature: float = 0.7
    messages: list[dict] = field(default_factory=list)
    # Если сессия загружена из сохранённого чата — его id и название.
    saved_chat_id: int | None = None
    saved_title: str | None = None

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        if len(self.messages) > _MEM_CAP:
            self.messages = self.messages[-_MEM_CAP:]

    def is_empty(self) -> bool:
        return not self.messages


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
    s = Session(
        model=model,
        system_prompt=system_prompt,
        temperature=temperature,
        messages=list(messages),
        saved_chat_id=chat_id,
        saved_title=title,
    )
    _sessions[user_id] = s
    return s
