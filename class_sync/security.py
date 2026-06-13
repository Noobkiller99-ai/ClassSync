from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.fernet import Fernet


def _key() -> bytes:
    configured = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if configured:
        return configured.encode("utf-8")
    secret = os.getenv("SECRET_KEY", "dev-secret-change-me")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_json(value: dict[str, Any]) -> str:
    return Fernet(_key()).encrypt(json.dumps(value).encode("utf-8")).decode("utf-8")


def decrypt_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    return json.loads(Fernet(_key()).decrypt(value.encode("utf-8")).decode("utf-8"))
