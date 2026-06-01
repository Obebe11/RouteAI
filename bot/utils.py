"""Вспомогательные функции для Telegram-бота."""

_MD2_SPECIAL = set("_*[]()~`>#+-=|{}.!")


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Режет текст на куски <= limit символов по границам абзацев/строк/слов."""
    if not text:
        return []

    def hard_split(s: str) -> list[str]:
        return [s[i:i + limit] for i in range(0, len(s), limit)]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current)
        current = ""

    # Сначала по абзацам, внутри — по строкам, внутри — по словам.
    paragraphs = text.split("\n\n")
    for pi, para in enumerate(paragraphs):
        piece = para + ("\n\n" if pi < len(paragraphs) - 1 else "")
        if len(piece) <= limit:
            if len(current) + len(piece) > limit:
                flush()
            current += piece
            continue
        # Абзац слишком большой — дробим по строкам.
        lines = piece.split("\n")
        for li, line in enumerate(lines):
            ln = line + ("\n" if li < len(lines) - 1 else "")
            if len(ln) <= limit:
                if len(current) + len(ln) > limit:
                    flush()
                current += ln
                continue
            # Строка слишком большая — по словам.
            for word in ln.split(" "):
                token = word + " "
                if len(token) > limit:
                    flush()
                    for sub in hard_split(word):
                        chunks.append(sub)
                    continue
                if len(current) + len(token) > limit:
                    flush()
                current += token

    flush()
    return chunks


def escape_markdown_v2(text: str) -> str:
    """Экранирует спецсимволы для Telegram parse_mode=MarkdownV2."""
    return "".join("\\" + ch if ch in _MD2_SPECIAL else ch for ch in text)


def trim_history(messages: list[dict], max_messages: int = 20) -> list[dict]:
    """Сохраняет system-сообщения + последние max_messages обычных сообщений."""
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return system + rest[-max_messages:]
