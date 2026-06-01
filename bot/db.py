"""Слой доступа к данным (SQLite через aiosqlite)."""

import os

import aiosqlite

from . import config
from .crypto import decrypt, decrypt_with, encrypt, encrypt_with

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    openrouter_key TEXT,
    active_chat_id INTEGER
);

CREATE TABLE IF NOT EXISTS chats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    system_prompt TEXT    NOT NULL DEFAULT '',
    temperature   REAL    NOT NULL DEFAULT 0.7,
    locked        INTEGER NOT NULL DEFAULT 0,
    salt          TEXT,
    verifier      TEXT,
    created_at     TEXT   NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
"""


class DB:
    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        # Ограничиваем доступ к файлу БД только владельцем (rw-------).
        if not os.path.exists(self._path):
            open(self._path, "a").close()
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        # Доращиваем колонки на старых БД (CREATE IF NOT EXISTS их не добавит).
        cur = await self._conn.execute("PRAGMA table_info(chats)")
        cols = {r["name"] for r in await cur.fetchall()}
        for name, ddl in (
            ("locked", "ALTER TABLE chats ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"),
            ("salt", "ALTER TABLE chats ADD COLUMN salt TEXT"),
            ("verifier", "ALTER TABLE chats ADD COLUMN verifier TEXT"),
        ):
            if name not in cols:
                await self._conn.execute(ddl)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "DB не подключена"
        return self._conn

    # ---- users -----------------------------------------------------------

    async def ensure_user(self, user_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,)
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        return await cur.fetchone()

    async def get_user_key(self, user_id: int) -> str | None:
        """Расшифрованный личный ключ пользователя, либо None."""
        user = await self.get_user(user_id)
        return decrypt(user["openrouter_key"]) if user else None

    async def set_user_key(self, user_id: int, key: str | None) -> None:
        stored = encrypt(key) if key else None
        await self.conn.execute(
            "UPDATE users SET openrouter_key = ? WHERE user_id = ?", (stored, user_id)
        )
        await self.conn.commit()

    async def set_active_chat(self, user_id: int, chat_id: int) -> None:
        await self.conn.execute(
            "UPDATE users SET active_chat_id = ? WHERE user_id = ?",
            (chat_id, user_id),
        )
        await self.conn.commit()

    # ---- chats -----------------------------------------------------------

    async def create_chat(self, user_id: int, title: str, model: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO chats(user_id, title, model) VALUES (?, ?, ?)",
            (user_id, title, model),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_chats(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM chats WHERE user_id = ? ORDER BY id", (user_id,)
        )
        return list(await cur.fetchall())

    async def get_chat(self, chat_id: int, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)
        )
        return await cur.fetchone()

    async def get_active_chat(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT c.* FROM chats c "
            "JOIN users u ON u.active_chat_id = c.id "
            "WHERE u.user_id = ?",
            (user_id,),
        )
        return await cur.fetchone()

    async def update_chat_field(self, chat_id: int, field: str, value) -> None:
        if field not in {"title", "model", "system_prompt", "temperature"}:
            raise ValueError(f"Недопустимое поле: {field}")
        await self.conn.execute(
            f"UPDATE chats SET {field} = ? WHERE id = ?", (value, chat_id)
        )
        await self.conn.commit()

    async def save_session(
        self,
        user_id: int,
        title: str,
        model: str,
        system_prompt: str,
        temperature: float,
        messages: list[dict],
        chat_id: int | None = None,
        fernet=None,
        salt: str | None = None,
        verifier: str | None = None,
    ) -> int:
        """Сохранить разговор как чат. Если chat_id задан — перезаписать его.

        Если передан fernet — чат шифруется пользовательским паролем (locked),
        иначе общим ключом сервера.
        """
        locked = 1 if fernet is not None else 0
        if chat_id is not None:
            await self.conn.execute(
                "UPDATE chats SET title=?, model=?, system_prompt=?, temperature=?, "
                "locked=?, salt=?, verifier=? WHERE id=? AND user_id=?",
                (title, model, system_prompt, temperature, locked, salt, verifier,
                 chat_id, user_id),
            )
            await self.conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        else:
            cur = await self.conn.execute(
                "INSERT INTO chats(user_id, title, model, system_prompt, temperature, "
                "locked, salt, verifier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, title, model, system_prompt, temperature, locked, salt, verifier),
            )
            chat_id = cur.lastrowid
        enc = (lambda t: encrypt_with(fernet, t)) if fernet is not None else encrypt
        await self.conn.executemany(
            "INSERT INTO messages(chat_id, role, content) VALUES (?, ?, ?)",
            [(chat_id, m["role"], enc(m["content"])) for m in messages],
        )
        await self.conn.commit()
        return chat_id

    async def delete_chat(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await self.conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        await self.conn.commit()

    # ---- messages --------------------------------------------------------

    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await self.conn.execute(
            "INSERT INTO messages(chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, role, encrypt(content)),
        )
        await self.conn.commit()

    async def get_messages(self, chat_id: int, fernet=None) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        )
        dec = (lambda v: decrypt_with(fernet, v)) if fernet is not None else decrypt
        return [
            {"role": r["role"], "content": dec(r["content"]) or ""}
            for r in await cur.fetchall()
        ]

    async def clear_messages(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()


db = DB(config.DB_PATH)
