"""bcrypt hashing + verification via passlib."""
from __future__ import annotations

from passlib.context import CryptContext
from passlib.exc import UnknownHashError

# bcrypt cost factor 12 (~250ms). cybersecurity agent: tune if perf becomes issue.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """True iff `plain` matches `hashed`.

    Only an unrecognizable stored hash is treated as a (failed) auth attempt.
    Backend/config errors (e.g. a broken bcrypt↔passlib pairing) deliberately
    propagate — silently returning False there made every login look like
    "wrong password" and hid the real misconfiguration.
    """
    try:
        return _pwd_context.verify(plain, hashed)
    except (UnknownHashError, ValueError):
        # ValueError covers empty/malformed stored hashes (e.g. "" / "x").
        return False
