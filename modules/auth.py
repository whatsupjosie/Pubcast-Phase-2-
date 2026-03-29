"""
modules/auth.py — JWT authentication and role-based access control.

Exports:
    create_access_token(data: dict) -> str
    decode_access_token(token: str) -> dict | None
    verify_password(plain: str, hashed: str) -> bool
    get_password_hash(plain: str) -> str
    role_gte(user_role: str, min_role: str) -> bool
    ROLE_ORDER: dict[str, int]
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role hierarchy — higher number = more permissions
# ---------------------------------------------------------------------------
ROLE_ORDER: Dict[str, int] = {
    "guest": 0,
    "viewer": 1,
    "mod": 2,
    "admin": 3,
    "owner": 4,
}

# ---------------------------------------------------------------------------
# Config — pulled from env so secrets never live in source
# ---------------------------------------------------------------------------
_SECRET_KEY: str = os.environ.get("PUBCAST_JWT_SECRET", "change-me-in-production-please")
_ALGORITHM: str = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.environ.get("PUBCAST_JWT_EXPIRE_MINUTES", "1440"))  # 24h default

if _SECRET_KEY == "change-me-in-production-please":
    logger.warning(
        "PUBCAST_JWT_SECRET is not set — using insecure default. "
        "Set PUBCAST_JWT_SECRET in your environment before going live."
    )

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain-text password matches the stored bcrypt hash."""
    try:
        return _pwd_context.verify(plain, hashed)
    except Exception as exc:
        logger.warning("Password verification error: %s", exc)
        return False


def get_password_hash(plain: str) -> str:
    """Return a bcrypt hash of the given plain-text password."""
    return _pwd_context.hash(plain)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(data: Dict[str, Any], *, expires_minutes: Optional[int] = None) -> str:
    """
    Encode ``data`` into a signed JWT.

    The token includes an ``exp`` claim based on ``expires_minutes``
    (defaults to ``PUBCAST_JWT_EXPIRE_MINUTES``).
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes if expires_minutes is not None else _ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, _SECRET_KEY, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode and verify a JWT.

    Returns the payload dict on success, or None if the token is missing,
    expired, or has an invalid signature.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        return payload
    except JWTError as exc:
        logger.debug("JWT decode failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------

def role_gte(user_role: str, min_role: str) -> bool:
    """Return True if ``user_role`` has at least ``min_role`` privileges."""
    user_level = ROLE_ORDER.get(user_role, -1)
    min_level = ROLE_ORDER.get(min_role, 999)
    return user_level >= min_level
