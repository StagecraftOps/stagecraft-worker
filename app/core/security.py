import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_FERNET_KDF_LABEL = b"stagecraft-token-encryption-v1:"

def _get_fernet() -> Fernet:
    if settings.TOKEN_ENCRYPTION_KEY:
        return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
    key_bytes = hashlib.sha256(_FERNET_KDF_LABEL + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_bytes))

def decrypt_token(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()
