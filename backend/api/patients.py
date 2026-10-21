"""Patient-facing and clinician-facing patient record routes."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user, require_admin_or_doctor
from backend.models import Alert, AlertAcknowledgement, DoctorNote, RiskAssessment, SensorReading, User, utcnow
from backend.schemas import DoctorNoteCreate, DoctorNoteOut, PatientListItem, ReadingOut
from backend.api.shared import (
    build_patient_list_item,
    device_for_patient,
    ensure_patient_visible,
    filter_readings,
    latest_reading,
    latest_risk,
    recompute_risk_for_patient,
    visible_patient_ids,
)
from backend.services.device_manager import is_device_online

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("", response_model=list[PatientListItem])
def list_patients(
    search: str | None = None,
    risk: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ids = visible_patient_ids(db, user)
    q = select(User).where(User.role == "patient", User.is_active.is_(True))
    if ids is not None:
        if not ids:
            return []
        q = q.where(User.id.in_(ids))
    if search:
        like = f"%{search.strip()}%"
        q = q.where((User.full_name.ilike(like)) | (User.email.ilike(like)))
    patients = db.execute(q.order_by(User.full_name)).scalars().all()
    items = [build_patient_list_item(db, p) for p in patients]
    if risk:
        items = [i for i in items if i["risk_category"] == risk]
    return items


@router.get("/{patient_id}", response_model=PatientListItem)
def get_patient(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ensure_patient_visible(db, user, patient_id)
    patient = db.get(User, patient_id)
    return build_patient_list_item(db, patient)


@router.get("/{patient_id}/readings")
def get_readings(
    patient_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(default=300, le=5000),
    include_simulated: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ensure_patient_visible(db, user, patient_id)
    readings = filter_readings(db, patient_id, start=start, end=end, limit=limit, include_simulated=include_simulated)
    latest = readings[0] if readings else None
    stale = True
    if latest:
        stale = (utcnow() - latest.received_at).total_seconds() > 60
    return {
        "readings": [ReadingOut.model_validate(r).model_dump() for r in readings],
        "count": len(readings),
        "stale": stale,
        "disclaimer": "Gaps in the chart indicate missing or invalid data — lines are not drawn across gaps.",
    }


@router.get("/{patient_id}/alerts")
def get_patient_alerts(
    patient_id: int,
    status_filter: str | None = None,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ensure_patient_visible(db, user, patient_id)
    q = select(Alert).where(Alert.patient_user_id == patient_id)
    if status_filter:
        q = q.where(Alert.status == status_filter)
    rows = db.execute(q.order_by(Alert.created_at.desc()).limit(limit)).scalars().all()
    return {
        "alerts": [
            {
                **{c: getattr(a, c) for c in (
                    "id", "patient_user_id", "assigned_doctor_user_id", "category", "severity",
                    "title", "message", "evidence", "status", "created_at", "acknowledged_at",
                    "resolved_at", "resolution_notes", "dedupe_key", "is_simulated")},
            }
            for a in rows
        ]
    }


@router.get("/{patient_id}/risk-history")
def risk_history(
    patient_id: int,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ensure_patient_visible(db, user, patient_id)
    rows = db.execute(
        select(RiskAssessment)
        .where(RiskAssessment.patient_user_id == patient_id)
        .order_by(RiskAssessment.assessed_at.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "assessments": [
            {
                "id": r.id,
                "assessed_at": r.assessed_at,
                "risk_category": r.risk_category,
                "risk_score": r.risk_score,
                "reasons": json.loads(r.reasons) if r.reasons else [],
                "evidence": json.loads(r.evidence) if r.evidence else {},
                "module": r.module,
                "is_simulated": r.is_simulated,
            }
            for r in rows
        ]
    }


@router.post("/{patient_id}/reassess")
def reassess(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Re-run validation + risk assessment for a patient using stored readings.

    Used by dashboards when data has gone stale, so risk reflects current
    freshness without waiting for the next ESP32 ingest.
    """
    ensure_patient_visible(db, user, patient_id)
    result, assessment, opened = recompute_risk_for_patient(db, patient_id)
    return {
        "risk_category": result.category,
        "risk_score": result.score,
        "reasons": result.reasons,
        "assessed_at": assessment.assessed_at,
        "alerts_opened": [a.id for a in opened],
        "disclaimer": "Prototype rule-based software priority — not a clinical diagnosis.",
    }


@router.get("/{patient_id}/notes", response_model=list[DoctorNoteOut])
def list_notes(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ensure_patient_visible(db, user, patient_id)
    rows = db.execute(
        select(DoctorNote).where(DoctorNote.patient_user_id == patient_id).order_by(DoctorNote.created_at.desc())
    ).scalars().all()
    return rows


@router.post("/{patient_id}/notes", response_model=DoctorNoteOut, status_code=201)
def create_note(
    patient_id: int,
    payload: DoctorNoteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    ensure_patient_visible(db, user, patient_id)
    note = DoctorNote(patient_user_id=patient_id, doctor_user_id=user.id, note_text=payload.note_text)
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.get("/{patient_id}/export")
def export_csv(
    patient_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Authorized CSV export of raw stored readings (gaps preserved, never interpolated)."""
    ensure_patient_visible(db, user, patient_id)
    readings = filter_readings(db, patient_id, start=start, end=end, limit=5000, include_simulated=True)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "reading_id", "received_at_utc", "device_id", "heart_rate_bpm", "spo2_percent",
        "ambient_temperature_c", "ambient_humidity_percent", "signal_quality", "is_valid",
        "validity_notes", "is_simulated", "firmware_version",
    ])
    for r in readings:
        writer.writerow([
            r.id, r.received_at.isoformat(), r.device_id, r.heart_rate, r.spo2,
            r.ambient_temperature, r.ambient_humidity, r.signal_quality,
            int(r.is_valid), r.validity_notes or "", int(r.is_simulated), r.firmware_version or "",
        ])
    buf.seek(0)
    filename = f"patient_{patient_id}_readings.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{patient_id}/summary")
def patient_summary(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ensure_patient_visible(db, user, patient_id)
    patient = db.get(User, patient_id)
    reading = latest_reading(db, patient_id)
    risk = latest_risk(db, patient_id)
    device = device_for_patient(db, patient_id)
    since = utcnow() - timedelta(hours=24)
    alerts_24h = db.execute(
        select(Alert).where(Alert.patient_user_id == patient_id, Alert.created_at >= since)
    ).scalars().all()
    valid_count = db.execute(
        select(SensorReading).where(
            SensorReading.patient_user_id == patient_id,
            SensorReading.is_valid.is_(True),
            SensorReading.is_simulated.is_(False),
            SensorReading.received_at >= since,
        )
    ).scalars().all()
    return {
        "patient": {"user_id": patient.id, "full_name": patient.full_name, "email": patient.email},
        "profile": patient.patient_profile,
        "device": {
            "device_uid": device.device_uid if device else None,
            "name": device.name if device else None,
            "online": is_device_online(device) if device else False,
            "last_seen_at": device.last_seen_at if device else None,
            "ip_address": device.ip_address if device else None,
        },
        "latest": {
            "heart_rate": reading.heart_rate if reading and reading.is_valid else None,
            "spo2": reading.spo2 if reading and reading.is_valid else None,
            "ambient_temperature": reading.ambient_temperature if reading else None,
            "ambient_humidity": reading.ambient_humidity if reading else None,
            "signal_quality": reading.signal_quality if reading else None,
            "received_at": reading.received_at if reading else None,
        },
        "risk": {
            "category": risk.risk_category if risk else "low",
            "score": risk.risk_score if risk else 0.0,
            "reasons": json.loads(risk.reasons) if risk and risk.reasons else [],
            "assessed_at": risk.assessed_at if risk else None,
        },
        "alerts_24h": len(alerts_24h),
        "valid_readings_24h": len(valid_count),
        "notice": "This prototype is not a substitute for clinical assessment or emergency care.",
    }
