"""Временные (in-memory) сессии разговора.

По умолчанию разговор НЕ сохраняется в БД — живёт только здесь и теряется
при перезапуске бота. Сохранение в именованный чат делается командой /save.
"""

from dataclasses import dataclass, field

from . import config

# Мягкий предел числа сообщений, удерживаемых в памяти на сессию.
_MEM_CAP = 200

# Встроенный системный промт о форматировании сообщений в Telegram и языке
# ответа. Включён по умолчанию (Session.tg_format = True). На английском.
TG_FORMAT_PROMPT = (
    "OUTPUT & LANGUAGE RULES (you are replying inside a Telegram chat):\n"
    "1. Always reply in the SAME LANGUAGE the user writes in.\n"
    "2. Telegram renders ONLY this formatting, written as HTML tags: "
    "<b>bold</b>, <i>italic</i>, <u>underline</u>, <s>strikethrough</s>, "
    "<tg-spoiler>spoiler</tg-spoiler>, <code>inline code</code>, "
    "<pre>code block</pre>, <blockquote>quote</blockquote>, and links as "
    "<a href=\"https://example.com\">text</a>.\n"
    "3. Use ONLY those HTML tags for formatting. Do NOT use Markdown "
    "(no **bold**, *italic*, _underline_, `code`, ``` fences, # headings, "
    "> quotes, or [text](url) links) — Telegram does NOT parse Markdown here "
    "and it will be shown as raw characters.\n"
    "4. Telegram does NOT support headings, tables, or nested lists. For a "
    "heading use a <b>bold line</b>; for lists start lines with • or 1.\n"
    "5. In ordinary text you MUST escape these characters as HTML entities: "
    "& as &amp;, < as &lt;, > as &gt; (do not escape inside real tags).\n"
    "6. Keep messages clean and easy to read on a phone."
)


@dataclass
class Session:
    model: str = config.DEFAULT_MODEL
    # Пользовательские системные промты (могут комбинироваться несколько).
    system_prompts: list[str] = field(default_factory=list)
    # Встроенный промт о форматировании Telegram — включён по умолчанию.
    tg_format: bool = True
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
        """Только пользовательские промты, объединённые (без TG-формата)."""
        return "\n\n".join(p for p in self.system_prompts if p)

    def effective_system(self) -> str:
        """Итоговый системный промт: TG-формат (если вкл) + пользовательские."""
        parts: list[str] = []
        if self.tg_format:
            parts.append(TG_FORMAT_PROMPT)
        parts.extend(p for p in self.system_prompts if p)
        return "\n\n".join(parts)


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
        system_prompts=[system_prompt] if system_prompt else [],
        temperature=temperature,
        messages=list(messages),
        saved_chat_id=chat_id,
        saved_title=title,
    )
    _sessions[user_id] = s
    return s
