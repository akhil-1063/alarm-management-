"""Administrator routes: account management, approvals, audit logs, settings, health."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.database import get_db, engine
from backend.dependencies import require_admin
from backend.config import settings as app_settings
from backend.models import (
    Alert,
    ApplicationSetting,
    AuditLog,
    Device,
    DoctorPatientAssignment,
    DoctorProfile,
    PatientProfile,
    SensorReading,
    User,
    utcnow,
)
from backend.schemas import AuditLogOut, SettingsUpdateRequest, UserOut
from backend.security import audit, hash_password
from backend.services.device_manager import is_device_online

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/overview")
def admin_overview(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    now = utcnow()
    total_patients = db.execute(select(func.count()).select_from(User).where(User.role == "patient")).scalar() or 0
    total_doctors = db.execute(select(func.count()).select_from(User).where(User.role == "doctor")).scalar() or 0
    pending_doctors = db.execute(
        select(func.count()).select_from(User).where(User.role == "doctor", User.is_approved.is_(False))
    ).scalar() or 0
    total_devices = db.execute(select(func.count()).select_from(Device)).scalar() or 0
    devices = db.execute(select(Device)).scalars().all()
    online = sum(1 for d in devices if is_device_online(d, now))
    active_alerts = db.execute(
        select(func.count()).select_from(Alert).where(Alert.status.in_(["active", "acknowledged"]))
    ).scalar() or 0
    recent_events = db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(25)
    ).scalars().all()
    readings_total = db.execute(select(func.count()).select_from(SensorReading)).scalar() or 0
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        db_status = "ok"
    except Exception as exc:  # pragma: no cover
        db_status = f"error: {exc}"

    return {
        "total_patients": total_patients,
        "total_doctors": total_doctors,
        "pending_doctor_approvals": pending_doctors,
        "total_devices": total_devices,
        "devices_online": online,
        "devices_offline": total_devices - online,
        "active_alerts": active_alerts,
        "readings_total": readings_total,
        "database_status": db_status,
        "server_time": now,
        "recent_events": [
            {
                "id": e.id,
                "actor_label": e.actor_label,
                "action": e.action,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "detail": e.detail,
                "created_at": e.created_at,
            }
            for e in recent_events
        ],
    }


@router.get("/users")
def list_users(role: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    q = select(User)
    if role:
        q = q.where(User.role == role)
    rows = db.execute(q.order_by(User.created_at.desc()).limit(500)).scalars().all()
    return {
        "users": [
            {
                "id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role,
                "is_active": u.is_active, "is_approved": u.is_approved,
                "created_at": u.created_at, "last_login_at": u.last_login_at,
            }
            for u in rows
        ]
    }


@router.post("/doctors/{doctor_id}/approve")
def approve_doctor(doctor_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    doctor = db.get(User, doctor_id)
    if not doctor or doctor.role != "doctor":
        raise HTTPException(status_code=404, detail="Doctor not found")
    doctor.is_approved = True
    db.add(doctor)
    audit(db, "doctor_approved", actor_user_id=user.id, actor_label=user.email,
          entity_type="user", entity_id=doctor.id,
          ip_address=request.client.host if request.client else None)
    return {"status": "approved", "doctor_id": doctor.id}


@router.post("/doctors/{doctor_id}/reject")
def reject_doctor(doctor_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    doctor = db.get(User, doctor_id)
    if not doctor or doctor.role != "doctor":
        raise HTTPException(status_code=404, detail="Doctor not found")
    doctor.is_active = False
    doctor.is_approved = False
    db.add(doctor)
    audit(db, "doctor_rejected", actor_user_id=user.id, actor_label=user.email,
          entity_type="user", entity_id=doctor.id)
    return {"status": "rejected", "doctor_id": doctor.id}


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    target.is_active = False
    db.add(target)
    audit(db, "user_deactivated", actor_user_id=user.id, actor_label=user.email,
          entity_type="user", entity_id=target.id)
    return {"status": "deactivated", "user_id": user_id}


@router.post("/users/{user_id}/activate")
def activate_user(user_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target.is_active = True
    db.add(target)
    audit(db, "user_activated", actor_user_id=user.id, actor_label=user.email,
          entity_type="user", entity_id=target.id)
    return {"status": "activated", "user_id": user_id}


@router.post("/users/create", status_code=201)
def create_user(payload: dict, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Admin-controlled account creation (e.g. issuing a doctor or patient account)."""
    email = str(payload.get("email", "")).lower().strip()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", ""))
    full_name = str(payload.get("full_name", "")).strip()
    if role not in {"admin", "doctor", "patient"}:
        raise HTTPException(status_code=400, detail="Role must be admin, doctor or patient")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not email or not full_name:
        raise HTTPException(status_code=400, detail="email and full_name are required")
    if db.execute(select(User).where(User.email == email)).scalars().first():
        raise HTTPException(status_code=409, detail="Email already in use")

    new_user = User(
        email=email, password_hash=hash_password(password), full_name=full_name,
        role=role, is_active=True,
        is_approved=(role != "doctor"),  # doctors still need explicit approval
    )
    db.add(new_user)
    db.flush()
    if role == "patient":
        db.add(PatientProfile(user_id=new_user.id))
    elif role == "doctor":
        db.add(DoctorProfile(user_id=new_user.id, specialty=payload.get("specialty")))
    db.commit()
    db.refresh(new_user)
    audit(db, "user_created_by_admin", actor_user_id=user.id, actor_label=user.email,
          entity_type="user", entity_id=new_user.id, detail=f"role={role} email={email}",
          ip_address=request.client.host if request.client else None)
    return {"status": "created", "user_id": new_user.id, "email": email, "role": role,
            "is_approved": new_user.is_approved}


@router.get("/audit-logs", response_model=list[AuditLogOut])
def audit_logs(limit: int = 200, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 1000))).scalars().all()
    return rows


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = db.execute(select(ApplicationSetting)).scalars().all()
    from backend.config import settings as cfg
    defaults = {
        "hr_low": cfg.hr_low, "hr_high": cfg.hr_high,
        "hr_critical_low": cfg.hr_critical_low, "hr_critical_high": cfg.hr_critical_high,
        "spo2_low": cfg.spo2_low, "spo2_critical_low": cfg.spo2_critical_low,
        "abnormal_persist_count": cfg.abnormal_persist_count,
    }
    overrides = {r.key: {"value": r.value, "description": r.description, "updated_at": r.updated_at} for r in rows}
    return {"defaults": defaults, "overrides": overrides}


@router.post("/settings")
def update_settings(payload: SettingsUpdateRequest, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    allowed = {
        "hr_low", "hr_high", "hr_critical_low", "hr_critical_high",
        "spo2_low", "spo2_critical_low", "abnormal_persist_count",
    }
    if payload.key not in allowed:
        raise HTTPException(status_code=400, detail=f"Setting must be one of: {sorted(allowed)}")
    try:
        float(payload.value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Value must be numeric")
    existing = db.get(ApplicationSetting, payload.key)
    if existing:
        existing.value = payload.value
        db.add(existing)
    else:
        db.add(ApplicationSetting(key=payload.key, value=payload.value,
                                  description="Configured via admin settings"))
    audit(db, "setting_updated", actor_user_id=user.id, actor_label=user.email,
          entity_type="application_setting", entity_id=payload.key, detail=payload.value)
    return {"status": "updated", "key": payload.key, "value": payload.value}


@router.get("/pending-doctors")
def pending_doctors(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rows = db.execute(
        select(User).where(User.role == "doctor", User.is_approved.is_(False)).order_by(User.created_at.desc())
    ).scalars().all()
    return {
        "doctors": [
            {"id": u.id, "email": u.email, "full_name": u.full_name, "created_at": u.created_at,
             "specialty": u.doctor_profile.specialty if u.doctor_profile else None,
             "license_number": u.doctor_profile.license_number if u.doctor_profile else None}
            for u in rows
        ]
    }


@router.get("/system-health")
def system_health(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    now = utcnow()
    last_hour = now - timedelta(hours=1)
    readings_hour = db.execute(
        select(func.count()).select_from(SensorReading).where(SensorReading.received_at >= last_hour)
    ).scalar() or 0
    from backend.database import SessionLocal
    sizes = {}
    try:
        import os
        from backend.config import settings as cfg
        if cfg.database_url.startswith("sqlite:///"):
            path = cfg.database_url.replace("sqlite:///", "", 1)
            if os.path.exists(path):
                sizes["database_bytes"] = os.path.getsize(path)
    except Exception:
        pass
    return {
        "status": "ok",
        "server_time": now,
        "readings_last_hour": readings_hour,
        "stale_seconds_threshold": app_settings.stale_seconds,
        "device_offline_seconds_threshold": app_settings.device_offline_seconds,
        "storage": sizes,
    }
