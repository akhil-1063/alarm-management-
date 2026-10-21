"""Doctor dashboard routes."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import require_admin, require_admin_or_doctor, require_doctor
from backend.models import (
    Alert,
    AuditLog,
    DoctorPatientAssignment,
    DoctorProfile,
    SensorReading,
    User,
    utcnow,
)
from backend.schemas import PatientListItem
from backend.api.shared import build_patient_list_item

router = APIRouter(prefix="/doctors", tags=["doctors"])


@router.get("/patients", response_model=list[PatientListItem])
def my_patients(
    search: str | None = None,
    risk: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    """Patients assigned to the logged-in doctor (admins see all assigned patients)."""
    if user.role == "admin":
        q = (
            select(DoctorPatientAssignment)
            .join(User, User.id == DoctorPatientAssignment.patient_user_id)
            .where(User.is_active.is_(True))
        )
    else:
        q = select(DoctorPatientAssignment).where(
            DoctorPatientAssignment.doctor_user_id == user.id,
            DoctorPatientAssignment.is_active.is_(True),
        )
    assignments = db.execute(q).scalars().all()
    items: list[PatientListItem] = []
    for a in assignments:
        patient = db.get(User, a.patient_user_id)
        if not patient:
            continue
        items.append(build_patient_list_item(db, patient))
    if search:
        s = search.strip().lower()
        items = [i for i in items if s in i["full_name"].lower() or s in i["email"].lower()]
    if risk:
        items = [i for i in items if i["risk_category"] == risk]
    return items


@router.get("/assignments")
def list_assignments(db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    q = select(DoctorPatientAssignment)
    if user.role != "admin":
        q = q.where(DoctorPatientAssignment.doctor_user_id == user.id)
    rows = db.execute(q.order_by(DoctorPatientAssignment.created_at.desc())).scalars().all()
    return {
        "assignments": [
            {
                "id": r.id,
                "doctor_user_id": r.doctor_user_id,
                "patient_user_id": r.patient_user_id,
                "is_active": r.is_active,
                "created_at": r.created_at,
                "doctor_name": (db.get(User, r.doctor_user_id).full_name if db.get(User, r.doctor_user_id) else None),
                "patient_name": (db.get(User, r.patient_user_id).full_name if db.get(User, r.patient_user_id) else None),
            }
            for r in rows
        ]
    }


@router.post("/assignments", status_code=201)
def create_assignment(
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Admin-only: assign a patient to a doctor."""
    doctor_id = int(payload.get("doctor_user_id", 0))
    patient_id = int(payload.get("patient_user_id", 0))
    doctor = db.get(User, doctor_id)
    patient = db.get(User, patient_id)
    if not doctor or doctor.role != "doctor":
        raise HTTPException(status_code=404, detail="Doctor not found")
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")

    existing = db.execute(
        select(DoctorPatientAssignment).where(
            DoctorPatientAssignment.doctor_user_id == doctor_id,
            DoctorPatientAssignment.patient_user_id == patient_id,
        )
    ).scalars().first()
    if existing:
        existing.is_active = True
        db.add(existing)
        db.commit()
        db.refresh(existing)
        assignment_id = existing.id
    else:
        row = DoctorPatientAssignment(
            doctor_user_id=doctor_id, patient_user_id=patient_id, assigned_by=user.id, is_active=True
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assignment_id = row.id

    db.add(AuditLog(
        actor_user_id=user.id, actor_label=user.email,
        action="doctor_patient_assigned", entity_type="assignment", entity_id=assignment_id,
        detail=f"doctor={doctor_id} patient={patient_id}",
    ))
    db.commit()
    return {"status": "assigned", "id": assignment_id}


@router.delete("/assignments/{assignment_id}")
def remove_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    row = db.get(DoctorPatientAssignment, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    row.is_active = False
    db.add(row)
    db.add(AuditLog(
        actor_user_id=user.id, actor_label=user.email,
        action="doctor_patient_unassigned", entity_type="assignment", entity_id=assignment_id,
        detail=f"doctor={row.doctor_user_id} patient={row.patient_user_id}",
    ))
    db.commit()
    return {"status": "unassigned"}


@router.get("/overview")
def doctor_overview(db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    """Aggregate counters for the doctor dashboard."""
    from backend.api.shared import visible_patient_ids

    ids = visible_patient_ids(db, user) or []
    if user.role == "admin":
        from sqlalchemy import func

        patient_count = db.execute(
            select(func.count()).select_from(User).where(User.role == "patient", User.is_active.is_(True))
        ).scalar() or 0
        risk_rows = {}
    else:
        patient_count = len(ids)
        risk_rows = {}

    from backend.api.shared import latest_risk, latest_reading
    from backend.config import settings

    counts = {"low": 0, "moderate": 0, "high": 0, "critical": 0}
    stale_count = 0
    unack = 0
    crit_unack = 0
    now = utcnow()
    patient_ids = ids if ids is not None else []
    if user.role == "admin":
        patient_ids = [r[0] for r in db.execute(select(User.id).where(User.role == "patient", User.is_active.is_(True))).all()]

    for pid in patient_ids:
        risk = latest_risk(db, pid)
        cat = risk.risk_category if risk else "low"
        counts[cat] = counts.get(cat, 0) + 1
        reading = latest_reading(db, pid)
        if not reading or (now - reading.received_at).total_seconds() > settings.stale_seconds:
            stale_count += 1

    alerts = db.execute(
        select(Alert).where(Alert.status.in_(["active", "acknowledged"]))
    ).scalars().all()
    if user.role == "doctor":
        alerts = [a for a in alerts if a.patient_user_id in set(patient_ids)]
    unack = sum(1 for a in alerts if a.status == "active")
    crit_unack = sum(1 for a in alerts if a.status == "active" and a.severity == "critical")

    recent = db.execute(
        select(SensorReading)
        .where(SensorReading.received_at >= now - timedelta(hours=1))
        .order_by(SensorReading.received_at.desc())
        .limit(50)
    ).scalars().all()
    if user.role == "doctor":
        recent = [r for r in recent if r.patient_user_id in set(patient_ids)]

    return {
        "total_patients": patient_count,
        "risk_counts": counts,
        "stale_patients": stale_count,
        "unacknowledged_alerts": unack,
        "critical_unacknowledged": crit_unack,
        "recent_readings": [
            {
                "patient_user_id": r.patient_user_id,
                "received_at": r.received_at,
                "heart_rate": r.heart_rate,
                "spo2": r.spo2,
                "signal_quality": r.signal_quality,
                "is_valid": r.is_valid,
                "is_simulated": r.is_simulated,
            }
            for r in recent[:20]
        ],
    }
