"""Small TOTP helpers for OpenAI MFA secrets."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
import time
from urllib.parse import parse_qs, urlparse


def normalize_totp_secret(value: str) -> str:
    raw = (value or "").strip()
    if raw.lower().startswith("otpauth://"):
        qs = parse_qs(urlparse(raw).query)
        raw = (qs.get("secret") or [""])[0]
    secret = re.sub(r"[^A-Za-z2-7]", "", raw).upper()
    if not secret:
        raise ValueError("missing totp secret")
    # Validate base32 early, but store without padding/spaces.
    try:
        base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    except binascii.Error as exc:
        raise ValueError("invalid totp secret") from exc
    return secret


def totp_code(secret: str, *, for_time: float | None = None, period: int = 30, digits: int = 6) -> str:
    secret = normalize_totp_secret(secret)
    counter = int((time.time() if for_time is None else for_time) // period)
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def verify_totp_code(secret: str, code: str, *, at_time: float | None = None, window: int = 1) -> bool:
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6:
        return False
    now = time.time() if at_time is None else at_time
    return any(totp_code(secret, for_time=now + 30 * step) == code for step in range(-window, window + 1))


def mask_totp_secret(secret: str) -> str:
    secret = (secret or "").strip()
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"
