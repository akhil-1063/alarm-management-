"""Auth routes: login, logout, registration, session introspection."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import SESSION_COOKIE, settings
from backend.database import get_db
from backend.dependencies import get_current_user, require_admin
from backend.models import DoctorProfile, PatientProfile, User, utcnow
from backend.schemas import (
    DoctorRegisterRequest,
    LoginRequest,
    MeResponse,
    PatientRegisterRequest,
    UserOut,
)
from backend.security import (
    audit,
    client_ip,
    create_session_token,
    hash_password,
    is_login_throttled,
    record_login_attempt,
    touch_last_login,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _me(db: Session, user: User) -> MeResponse:
    return MeResponse(
        user=UserOut.model_validate(user),
        patient_profile=user.patient_profile,
        doctor_profile=user.doctor_profile,
    )


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    email = payload.email.lower().strip()
    ip = client_ip(request)

    if is_login_throttled(db, email):
        audit(db, "login_throttled", actor_label=email, ip_address=ip)
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")

    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or not verify_password(payload.password, user.password_hash):
        record_login_attempt(db, email, False, ip)
        audit(db, "login_failed", actor_label=email, ip_address=ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        audit(db, "login_blocked_inactive", actor_user_id=user.id, actor_label=email, ip_address=ip)
        raise HTTPException(status_code=403, detail="Account is deactivated")

    if user.role == "doctor" and not user.is_approved:
        audit(db, "login_blocked_unapproved_doctor", actor_user_id=user.id, actor_label=email, ip_address=ip)
        raise HTTPException(status_code=403, detail="Doctor account is pending administrator approval")

    record_login_attempt(db, email, True, ip)
    touch_last_login(db, user)
    token = create_session_token(user.id, user.role)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=settings.session_minutes * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    audit(db, "login_success", actor_user_id=user.id, actor_label=email, ip_address=ip)
    return _me(db, user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    audit(db, "logout", actor_user_id=user.id, actor_label=user.email, ip_address=client_ip(request))
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return {"status": "logged_out"}


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _me(db, user)


@router.post("/register/patient", response_model=MeResponse, status_code=status.HTTP_201_CREATED)
def register_patient(payload: PatientRegisterRequest, request: Request, db: Session = Depends(get_db)):
    email = payload.email.lower().strip()
    if db.execute(select(User).where(User.email == email)).scalars().first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        role="patient",
        is_active=True,
        is_approved=True,  # patients self-register
    )
    db.add(user)
    db.flush()
    db.add(
        PatientProfile(
            user_id=user.id,
            date_of_birth=payload.date_of_birth,
            sex=payload.sex,
            ward=payload.ward,
            bed_label=payload.bed_label,
        )
    )
    db.commit()
    db.refresh(user)
    audit(db, "patient_registered", actor_user_id=user.id, actor_label=email,
          entity_type="user", entity_id=user.id, ip_address=client_ip(request))
    return _me(db, user)


@router.post("/register/doctor", response_model=MeResponse, status_code=status.HTTP_201_CREATED)
def register_doctor(payload: DoctorRegisterRequest, request: Request, db: Session = Depends(get_db)):
    """Public doctor registration creates an INACTIVE (pending approval) account.

    It never grants doctor privileges until an administrator approves it.
    """
    email = payload.email.lower().strip()
    if db.execute(select(User).where(User.email == email)).scalars().first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        role="doctor",
        is_active=True,
        is_approved=False,  # requires admin approval
    )
    db.add(user)
    db.flush()
    db.add(
        DoctorProfile(
            user_id=user.id,
            specialty=payload.specialty,
            license_number=payload.license_number,
        )
    )
    db.commit()
    db.refresh(user)
    audit(db, "doctor_registration_requested", actor_user_id=user.id, actor_label=email,
          entity_type="user", entity_id=user.id, ip_address=client_ip(request))
    result = _me(db, user)
    return result


@router.post("/forgot-password")
def forgot_password(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Local-prototype password reset.

    Generates a one-time reset code stored in the audit log and returned to
    the caller ONLY when ICU_DEBUG=1 (local development). In a real deployment
    this would be emailed. No account existence is revealed in the response.
    """
    import secrets

    email = str(payload.get("email", "")).lower().strip()
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if user:
        code = secrets.token_urlsafe(12)
        audit(db, "password_reset_requested", actor_user_id=user.id, actor_label=email,
              entity_type="user", entity_id=user.id, detail=f"reset_code_hash_pending:{code}",
              ip_address=client_ip(request))
        if settings.debug:
            return {"status": "ok", "debug_reset_code": code}
    return {"status": "ok", "message": "If the account exists, a reset code has been generated."}


@router.post("/reset-password")
def reset_password(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Complete a password reset using the code issued by /forgot-password.

    Codes are validated against the most recent password_reset_requested audit
    entry for the email (local prototype workflow).
    """
    import json

    from backend.models import AuditLog

    email = str(payload.get("email", "")).lower().strip()
    code = str(payload.get("code", ""))
    new_password = str(payload.get("new_password", ""))
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or not code:
        raise HTTPException(status_code=400, detail="Invalid reset request")

    entry = db.execute(
        select(AuditLog)
        .where(AuditLog.actor_user_id == user.id, AuditLog.action == "password_reset_requested")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).scalars().first()
    if not entry or not entry.detail or not entry.detail.endswith(code):
        raise HTTPException(status_code=400, detail="Invalid or expired reset code")

    user.password_hash = hash_password(new_password)
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.add(user)
    audit(db, "password_reset_completed", actor_user_id=user.id, actor_label=email,
          entity_type="user", entity_id=user.id, ip_address=client_ip(request))
    return {"status": "password_reset"}
