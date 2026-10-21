"""FastAPI dependencies: session auth, role guards, device auth."""
from __future__ import annotations

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from backend.config import DEVICE_HEADER, SESSION_COOKIE, settings
from backend.database import get_db
from backend.models import Device, User
from backend.security import client_ip, read_session_token


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> User:
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    data = read_session_token(session)
    if not data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid")
    user = db.get(User, data.get("uid"))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account inactive or missing")
    if user.role == "doctor" and not user.is_approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Doctor account pending approval")
    return user


def require_roles(*roles: str):
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user

    return checker


require_admin = require_roles("admin")
require_doctor = require_roles("doctor")
require_admin_or_doctor = require_roles("admin", "doctor")


def authenticate_device(
    request: Request,
    db: Session = Depends(get_db),
    x_device_token: str | None = Header(default=None, alias=DEVICE_HEADER),
) -> Device:
    """Independent device authentication for the ingestion endpoint.

    Accepts the token either as the X-Device-Token header or `device_token`
    query parameter (some ESP32 HTTP clients handle headers awkwardly).
    """
    token = x_device_token or request.query_params.get("device_token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing device credential")
    device = db.query(Device).filter(Device.device_token == token, Device.is_active.is_(True)).first()
    if not device:
        raise HTTPException(status_code=401, detail="Invalid device credential")
    return device


def audit_ip(request: Request) -> str | None:
    return client_ip(request)
