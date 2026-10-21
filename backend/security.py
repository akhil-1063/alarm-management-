"""Password hashing, session tokens, login throttling helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import AuditLog, LoginAttempt, User, utcnow

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto", pbkdf2_sha256__rounds=200_000)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="icu-session-v1")


def create_session_token(user_id: int, role: str) -> str:
    return _serializer().dumps({"uid": user_id, "role": role})


def read_session_token(token: str) -> dict | None:
    try:
        data = _serializer().loads(token, max_age=settings.session_minutes * 60)
        if isinstance(data, dict) and "uid" in data:
            return data
    except (BadSignature, SignatureExpired):
        return None
    return None


def generate_numeric_code(length: int = 8) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------

def _within_window() -> datetime:
    return utcnow() - timedelta(seconds=settings.login_window_seconds)


def is_login_throttled(db: Session, email: str) -> bool:
    row = db.execute(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.email == email,
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at >= _within_window(),
        )
    ).scalar()
    return bool(row and row >= settings.login_max_attempts)


def record_login_attempt(db: Session, email: str, success: bool, ip: str | None) -> None:
    db.add(LoginAttempt(email=email.lower(), success=success, ip_address=ip))
    if success:
        # clear failure history for this email
        db.query(LoginAttempt).filter(
            LoginAttempt.email == email.lower(), LoginAttempt.success.is_(False)
        ).delete()
    db.commit()


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def audit(
    db: Session,
    action: str,
    actor_user_id: int | None = None,
    actor_label: str = "system",
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    detail: str | None = None,
    ip_address: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            actor_label=actor_label,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail,
            ip_address=ip_address,
        )
    )
    db.commit()


def client_ip(request) -> str | None:  # request: fastapi.Request
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def touch_last_login(db: Session, user: User) -> None:
    user.last_login_at = utcnow()
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.add(user)
    db.commit()


def safe_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def constant_hash_matches(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return constant_time_equals(a, b)
