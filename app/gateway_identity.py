"""Identity asserted by the private gateway in production."""
import hmac
import os
import re
from hashlib import sha256

KEY_HEADER = "X-Rag-Gateway-Key"
USER_HEADER = "X-Rag-User"
_USER = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_KEY = re.compile(r"[a-f0-9]{64}")


def configured_key() -> str | None:
    mode = os.getenv("RAG_DEPLOYMENT", "local")
    if mode == "local":
        return None
    key = os.getenv("RAG_GATEWAY_KEY", "")
    if mode != "production" or not _KEY.fullmatch(key):
        raise RuntimeError("Production requires a 64-character hexadecimal RAG_GATEWAY_KEY")
    return key


def gateway_user(headers, key: str) -> str | None:
    proof = headers.get(KEY_HEADER, "")
    user = headers.get(USER_HEADER, "")
    if not _USER.fullmatch(user) or not _KEY.fullmatch(proof) or not hmac.compare_digest(proof, key):
        return None
    return user


def owner_id(user: str) -> str:
    # Stable across gateway-key rotation and safe as a filesystem component.
    return sha256(user.encode("ascii")).hexdigest()
