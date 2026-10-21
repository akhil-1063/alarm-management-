"""Shared helpers for API modules: access checks, patient lists, summaries."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import (
    Alert,
    Device,
    DoctorPatientAssignment,
    RiskAssessment,
    SensorReading,
    User,
    utcnow,
)
from backend.services.device_manager import is_device_online
from backend.services.sensor_validation import validate_reading


def ensure_patient_visible(db: Session, actor: User, patient_user_id: int) -> User:
    """Server-side data isolation: patient sees self, doctor sees assigned, admin sees all."""
    patient = db.get(User, patient_user_id)
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")
    if actor.role == "admin":
        return patient
    if actor.role == "patient":
        if actor.id != patient.id:
            raise HTTPException(status_code=403, detail="You may only access your own records")
        return patient
    if actor.role == "doctor":
        assigned = db.execute(
            select(DoctorPatientAssignment).where(
                DoctorPatientAssignment.doctor_user_id == actor.id,
                DoctorPatientAssignment.patient_user_id == patient_user_id,
                DoctorPatientAssignment.is_active.is_(True),
            )
        ).first()
        if not assigned:
            raise HTTPException(status_code=403, detail="This patient is not assigned to you")
        return patient
    raise HTTPException(status_code=403, detail="Insufficient permissions")


def visible_patient_ids(db: Session, actor: User) -> list[int] | None:
    """None means unrestricted (admin). Otherwise list of patient user IDs."""
    if actor.role == "admin":
        return None
    if actor.role == "patient":
        return [actor.id]
    rows = db.execute(
        select(DoctorPatientAssignment.patient_user_id).where(
            DoctorPatientAssignment.doctor_user_id == actor.id,
            DoctorPatientAssignment.is_active.is_(True),
        )
    ).all()
    return [r[0] for r in rows]


def latest_reading(db: Session, patient_user_id: int) -> SensorReading | None:
    return db.execute(
        select(SensorReading)
        .where(SensorReading.patient_user_id == patient_user_id)
        .order_by(SensorReading.received_at.desc())
        .limit(1)
    ).scalars().first()


def latest_risk(db: Session, patient_user_id: int) -> RiskAssessment | None:
    return db.execute(
        select(RiskAssessment)
        .where(RiskAssessment.patient_user_id == patient_user_id)
        .order_by(RiskAssessment.assessed_at.desc())
        .limit(1)
    ).scalars().first()


def device_for_patient(db: Session, patient_user_id: int) -> Device | None:
    return db.execute(
        select(Device).where(Device.patient_user_id == patient_user_id)
    ).scalars().first()


def build_patient_list_item(db: Session, patient: User) -> dict:
    reading = latest_reading(db, patient.id)
    risk = latest_risk(db, patient.id)
    device = device_for_patient(db, patient.id)
    alerts = db.execute(
        select(Alert).where(
            Alert.patient_user_id == patient.id,
            Alert.status.in_(["active", "acknowledged", "under_review"]),
        )
    ).scalars().all()

    now = utcnow()
    stale = True
    quality = None
    if reading:
        stale = (now - reading.received_at).total_seconds() > settings.stale_seconds
        quality = reading.signal_quality

    profile = patient.patient_profile

    # Priority: never present "no usable data" as "low/normal".
    usable_reading = bool(reading and reading.is_valid and (
        reading.heart_rate is not None or reading.spo2 is not None
    ))
    if not reading:
        priority = "insufficient_data"
    elif not usable_reading:
        priority = "insufficient_data"
    else:
        priority = risk.risk_category if risk else "low"
    priority_label = {
        "critical": "Critical",
        "high": "High",
        "moderate": "Medium",
        "low": "Low",
        "insufficient_data": "Insufficient Data",
    }.get(priority, priority.title())

    return {
        "user_id": patient.id,
        "full_name": patient.full_name,
        "email": patient.email,
        "ward": profile.ward if profile else None,
        "bed_label": profile.bed_label if profile else None,
        "assigned_device_uid": device.device_uid if device else None,
        "device_online": is_device_online(device) if device else False,
        "latest_hr": reading.heart_rate if reading and reading.is_valid else None,
        "latest_spo2": reading.spo2 if reading and reading.is_valid else None,
        "latest_ambient_temp": reading.ambient_temperature if reading else None,
        "latest_humidity": reading.ambient_humidity if reading else None,
        "latest_systolic_bp": reading.systolic_bp if reading else None,
        "latest_diastolic_bp": reading.diastolic_bp if reading else None,
        "bp_simulated": bool(reading.bp_simulated) if reading else False,
        "finger_present": reading.finger_present if reading else None,
        "buzzer_alert": bool(reading.buzzer_alert) if reading else False,
        "last_data_at": reading.received_at if reading else None,
        "signal_quality": quality,
        "data_stale": stale if reading else True,
        "risk_category": risk.risk_category if risk else "low",
        "priority": priority,
        "priority_label": priority_label,
        "active_alert_count": sum(1 for a in alerts if a.status in ("active", "acknowledged", "under_review")),
        "critical_alert_count": sum(1 for a in alerts if a.severity == "critical" and a.status in ("active", "acknowledged", "under_review")),
    }


def filter_readings(
    db: Session,
    patient_user_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 500,
    include_simulated: bool = False,
) -> list[SensorReading]:
    q = select(SensorReading).where(SensorReading.patient_user_id == patient_user_id)
    if start:
        q = q.where(SensorReading.received_at >= start)
    if end:
        q = q.where(SensorReading.received_at <= end)
    if not include_simulated:
        q = q.where(SensorReading.is_simulated.is_(False))
    q = q.order_by(SensorReading.received_at.desc()).limit(min(limit, 5000))
    return list(db.execute(q).scalars().all())


def recompute_risk_for_patient(db: Session, patient_user_id: int, is_simulated: bool = False):
    """Run validation + risk assessment + alerting for one patient after ingest."""
    from backend.services import alert_manager, risk_engine

    reading = latest_reading(db, patient_user_id)
    validation = None
    if reading:
        validation = validate_reading(
            heart_rate=reading.heart_rate,
            spo2=reading.spo2,
            ambient_temperature=reading.ambient_temperature,
            ambient_humidity=reading.ambient_humidity,
            signal_quality=reading.signal_quality,
            finger_detected=reading.finger_present if reading.finger_present is not None else True,
            systolic_bp=reading.systolic_bp,
            diastolic_bp=reading.diastolic_bp,
            bp_simulated=reading.bp_simulated,
        )
    result = risk_engine.assess_patient_risk(db, patient_user_id, reading, validation)
    assessment = RiskAssessment(
        patient_user_id=patient_user_id,
        risk_category=result.category,
        risk_score=result.score,
        reasons=risk_engine.assessment_to_json(result)[0],
        evidence=risk_engine.assessment_to_json(result)[1],
        module=result.module,
        is_simulated=is_simulated,
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    opened = alert_manager.evaluate_and_open_alerts(db, patient_user_id, result, is_simulated=is_simulated)
    return result, assessment, opened
