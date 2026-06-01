"""Шифрование чувствительных данных в состоянии покоя (at rest).

Шифруем ключи OpenRouter пользователей и тексты сохранённых сообщений.
Используется Fernet (AES-128-CBC + HMAC-SHA256) — аутентифицированное
симметричное шифрование. Ключ выводится из секрета ENCRYPTION_KEY (.env).
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import config

# Префикс-маркер шифротекста — отличает зашифрованные значения от легаси
# (plaintext) данных, оставшихся до включения шифрования.
_PREFIX = "enc::"

# Параметры scrypt для вывода ключа из пользовательского пароля.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _fernet() -> Fernet:
    # 32-байтный ключ из секрета. Секрет должен быть длинным и случайным.
    digest = hashlib.sha256(config.ENCRYPTION_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text: str) -> str:
    """Зашифровать строку. Возвращает строку с маркером _PREFIX."""
    token = _fernet().encrypt(text.encode()).decode()
    return _PREFIX + token


def decrypt(value: str | None) -> str | None:
    """Расшифровать. Значения без маркера считаются легаси-plaintext и
    возвращаются как есть (плавная миграция со старой незашифрованной БД)."""
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        return value  # легаси plaintext
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        # Неверный ENCRYPTION_KEY или повреждённые данные — не падаем.
        return None


# ---- шифрование пользовательским паролем (zero-knowledge) ----------------
#
# Ключ выводится из пароля пользователя через scrypt и НИГДЕ не хранится.
# Без пароля расшифровать сохранённый чат невозможно даже владельцу сервера.


def new_salt() -> str:
    """Случайная соль (hex) для привязки к конкретному сохранённому чату."""
    return os.urandom(16).hex()


def derive_fernet(passphrase: str, salt_hex: str) -> Fernet:
    kdf = Scrypt(salt=bytes.fromhex(salt_hex), length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    key = kdf.derive(passphrase.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def make_verifier(f: Fernet) -> str:
    """Токен для проверки правильности пароля при загрузке."""
    return f.encrypt(b"ok").decode()


def check_verifier(f: Fernet, verifier: str) -> bool:
    try:
        return f.decrypt(verifier.encode()) == b"ok"
    except InvalidToken:
        return False


def encrypt_with(f: Fernet, text: str) -> str:
    return _PREFIX + f.encrypt(text.encode()).decode()


def decrypt_with(f: Fernet, value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        return value
    try:
        return f.decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return None
