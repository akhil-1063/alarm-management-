"""Analytics summaries, alert-frequency reports, device uptime, CSV export."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.config import settings
from backend.models import Alert, Device, DoctorNote, SensorReading, User, utcnow
from backend.schemas import AnalyticsSummary
from backend.api.shared import ensure_patient_visible, filter_readings, visible_patient_ids

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary", response_model=AnalyticsSummary)
def summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    now = utcnow()
    total_patients = db.execute(
        select(func.count()).select_from(User).where(User.role == "patient", User.is_active.is_(True))
    ).scalar() or 0
    total_doctors = db.execute(
        select(func.count()).select_from(User).where(User.role == "doctor", User.is_active.is_(True))
    ).scalar() or 0
    total_devices = db.execute(select(func.count()).select_from(Device).where(Device.is_active.is_(True))).scalar() or 0

    devices = db.execute(select(Device).where(Device.is_active.is_(True))).scalars().all()
    from backend.services.device_manager import is_device_online
    devices_online = sum(1 for d in devices if is_device_online(d, now))

    active_alerts = db.execute(
        select(func.count()).select_from(Alert).where(Alert.status.in_(["active", "acknowledged"]))
    ).scalar() or 0
    critical = db.execute(
        select(func.count()).select_from(Alert)
        .where(Alert.severity == "critical", Alert.status.in_(["active", "acknowledged"]))
    ).scalar() or 0

    hour_ago = now - timedelta(hours=1)
    readings_hour = db.execute(
        select(func.count()).select_from(SensorReading)
        .where(SensorReading.received_at >= hour_ago, SensorReading.is_simulated.is_(False))
    ).scalar() or 0
    readings_total = db.execute(
        select(func.count()).select_from(SensorReading).where(SensorReading.is_simulated.is_(False))
    ).scalar() or 0

    # Risk breakdown from latest assessment per patient
    from backend.api.shared import latest_risk

    patient_ids = [r[0] for r in db.execute(select(User.id).where(User.role == "patient", User.is_active.is_(True))).all()]
    breakdown = {"low": 0, "moderate": 0, "high": 0, "critical": 0}
    for pid in patient_ids:
        r = latest_risk(db, pid)
        cat = r.risk_category if r else "low"
        breakdown[cat] = breakdown.get(cat, 0) + 1

    return AnalyticsSummary(
        total_patients=total_patients,
        total_doctors=total_doctors,
        total_devices=total_devices,
        devices_online=devices_online,
        active_alerts=active_alerts,
        critical_alerts=critical,
        readings_last_hour=readings_hour,
        readings_total=readings_total,
        risk_breakdown=breakdown,
        generated_at=now,
    )


@router.get("/alerts-frequency")
def alert_frequency(
    hours: int = Query(default=24, le=720),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    since = utcnow() - timedelta(hours=hours)
    rows = db.execute(
        select(Alert.category, Alert.severity, func.count())
        .where(Alert.created_at >= since)
        .group_by(Alert.category, Alert.severity)
    ).all()
    return {
        "window_hours": hours,
        "since": since,
        "breakdown": [{"category": c, "severity": s, "count": n} for c, s, n in rows],
    }


@router.get("/device-uptime")
def device_uptime(
    hours: int = Query(default=24, le=720),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    since = utcnow() - timedelta(hours=hours)
    devices = db.execute(select(Device)).scalars().all()
    out = []
    for d in devices:
        readings = db.execute(
            select(func.count(), func.min(SensorReading.received_at), func.max(SensorReading.received_at))
            .select_from(SensorReading)
            .where(SensorReading.device_id == d.id, SensorReading.received_at >= since)
        ).one()
        total_minutes = hours * 60
        if readings[1] and readings[2]:
            span_minutes = max(1, int((readings[2] - readings[1]).total_seconds() // 60))
        else:
            span_minutes = 0
        out.append({
            "device_id": d.id,
            "device_uid": d.device_uid,
            "name": d.name,
            "readings_in_window": readings[0] or 0,
            "first_reading": readings[1],
            "last_reading": readings[2],
            "active_span_minutes": span_minutes,
            "window_minutes": total_minutes,
            "last_seen_at": d.last_seen_at,
            "is_online": d.last_seen_at is not None and (utcnow() - d.last_seen_at).total_seconds() < settings.device_offline_seconds,
            "assigned_patient_user_id": d.patient_user_id,
        })
    return {"window_hours": hours, "devices": out}


@router.get("/export")
def export_readings(
    patient_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ensure_patient_visible(db, user, patient_id)
    readings = filter_readings(db, patient_id, start=start, end=end, limit=5000, include_simulated=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "reading_id", "received_at_utc", "device_id", "heart_rate_bpm", "spo2_percent",
        "ambient_temperature_c", "ambient_humidity_percent", "signal_quality", "is_valid",
        "validity_notes", "is_simulated", "firmware_version",
    ])
    for r in readings:
        w.writerow([
            r.id, r.received_at.isoformat(), r.device_id, r.heart_rate, r.spo2,
            r.ambient_temperature, r.ambient_humidity, r.signal_quality,
            int(r.is_valid), r.validity_notes or "", int(r.is_simulated), r.firmware_version or "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="patient_{patient_id}_export.csv"'},
    )


@router.get("/monitoring-summary")
def monitoring_summary(
    patient_id: int,
    hours: int = Query(default=24, le=720),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-patient monitoring summary (validity-aware, no gap interpolation)."""
    ensure_patient_visible(db, user, patient_id)
    since = utcnow() - timedelta(hours=hours)
    rows = filter_readings(db, patient_id, start=since, limit=5000, include_simulated=True)
    real = [r for r in rows if not r.is_simulated]
    valid = [r for r in real if r.is_valid]
    hr_vals = [r.heart_rate for r in valid if r.heart_rate is not None]
    sp_vals = [r.spo2 for r in valid if r.spo2 is not None]
    alert_rows = db.execute(
        select(Alert).where(Alert.patient_user_id == patient_id, Alert.created_at >= since)
    ).scalars().all()
    notes = db.execute(
        select(func.count()).select_from(DoctorNote)
        .where(DoctorNote.patient_user_id == patient_id, DoctorNote.created_at >= since)
    ).scalar() or 0
    return {
        "patient_id": patient_id,
        "window_hours": hours,
        "total_readings": len(rows),
        "real_readings": len(real),
        "simulated_readings": len(rows) - len(real),
        "valid_real_readings": len(valid),
        "invalid_or_unusable": len(real) - len(valid),
        "hr_min": min(hr_vals) if hr_vals else None,
        "hr_max": max(hr_vals) if hr_vals else None,
        "hr_avg": round(sum(hr_vals) / len(hr_vals), 1) if hr_vals else None,
        "spo2_min": min(sp_vals) if sp_vals else None,
        "spo2_avg": round(sum(sp_vals) / len(sp_vals), 1) if sp_vals else None,
        "alerts_in_window": len(alert_rows),
        "critical_alerts": sum(1 for a in alert_rows if a.severity == "critical"),
        "doctor_notes_added": notes,
        "note": "Statistics computed from stored readings only; gaps are not interpolated.",
    }
