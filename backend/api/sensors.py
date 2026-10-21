"""Sensor ingestion endpoint (device-authenticated) + live data polling."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import authenticate_device, get_current_user
from backend.models import Device, RiskAssessment, User, utcnow
from backend.schemas import IngestReading, IngestResponse
from backend.api.shared import ensure_patient_visible, latest_reading, recompute_risk_for_patient
from backend.services.ingest_service import store_ingest

router = APIRouter(tags=["sensors"])


def _parse_device_timestamp(value: str | None) -> datetime | None:
    from backend.services.ingest_service import parse_device_timestamp

    return parse_device_timestamp(value)


async def _broadcast(patient_id: int) -> None:
    """Push an update to connected dashboards after a new reading is stored."""
    try:
        from backend.main import broadcast_patient

        await broadcast_patient(patient_id)
    except Exception:
        pass  # live push is best-effort; polling remains the fallback


@router.post("/ingest", response_model=IngestResponse)
def ingest(
    payload: IngestReading,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    device: Device = Depends(authenticate_device),
):
    """Receive one sensor reading from a registered ESP32.

    The patient is resolved SERVER-SIDE from the device's own assignment.
    A patient_id field in the payload is ignored — devices cannot choose which
    patient record they write into.
    """
    if not device.is_active:
        device.last_ingest_at = utcnow()
        device.last_ingest_status = "rejected"
        device.last_error = "Device is deactivated on the server."
        db.add(device)
        db.commit()
        raise HTTPException(status_code=403, detail="Device is deactivated")

    result = store_ingest(db, device, payload, is_simulated=False)

    if result.status == "stored_unassigned":
        return IngestResponse(
            status="stored_unassigned",
            message="Device is not assigned to a patient yet. Assign it from the Devices page; "
                    "the reading was discarded so it cannot contaminate another patient's history.",
        )

    background_tasks.add_task(_broadcast, device.patient_user_id)
    return IngestResponse(
        status="stored",
        reading_id=result.reading.id if result.reading else None,
        message="ok" if result.is_valid else f"stored with validation notes: {result.notes}",
    )


@router.get("/live/{patient_id}")
def live_snapshot(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Lightweight live poll payload for dashboards (readings + risk + alerts)."""
    ensure_patient_visible(db, user, patient_id)
    reading = latest_reading(db, patient_id)
    risk = db.execute(
        select(RiskAssessment)
        .where(RiskAssessment.patient_user_id == patient_id)
        .order_by(RiskAssessment.assessed_at.desc())
        .limit(1)
    ).scalars().first()
    from backend.models import Alert
    alerts = db.execute(
        select(Alert)
        .where(Alert.patient_user_id == patient_id, Alert.status.in_(["active", "acknowledged", "under_review"]))
        .order_by(Alert.created_at.desc())
        .limit(20)
    ).scalars().all()
    from backend.config import settings

    now = utcnow()
    stale = True
    age = None
    if reading:
        age = (now - reading.received_at).total_seconds()
        stale = age > settings.stale_seconds
    return {
        "patient_user_id": patient_id,
        "reading": {
            "heart_rate": reading.heart_rate if reading and reading.is_valid else None,
            "spo2": reading.spo2 if reading and reading.is_valid else None,
            "ambient_temperature": reading.ambient_temperature if reading else None,
            "ambient_humidity": reading.ambient_humidity if reading else None,
            "systolic_bp": reading.systolic_bp if reading else None,
            "diastolic_bp": reading.diastolic_bp if reading else None,
            "bp_unit": reading.bp_unit if reading else "mmHg",
            "bp_simulated": reading.bp_simulated if reading else False,
            "bp_pot_raw": reading.bp_pot_raw if reading else None,
            "finger_present": reading.finger_present if reading else None,
            "hr_valid": reading.hr_valid if reading else None,
            "spo2_valid": reading.spo2_valid if reading else None,
            "buzzer_alert": reading.buzzer_alert if reading else False,
            "signal_quality": reading.signal_quality if reading else None,
            "is_valid": reading.is_valid if reading else False,
            "received_at": reading.received_at if reading else None,
            "is_simulated": reading.is_simulated if reading else False,
        } if reading else None,
        "data_age_seconds": age,
        "stale": stale,
        "risk": {
            "category": risk.risk_category if risk else "low",
            "score": risk.risk_score if risk else 0.0,
            "reasons": json.loads(risk.reasons) if risk and risk.reasons else [],
            "assessed_at": risk.assessed_at if risk else None,
            "is_simulated": risk.is_simulated if risk else False,
        },
        "active_alerts": [
            {
                "id": a.id, "severity": a.severity, "category": a.category,
                "title": a.title, "message": a.message, "status": a.status,
                "created_at": a.created_at, "is_simulated": a.is_simulated,
            }
            for a in alerts
        ],
        "server_time": now,
        "disclaimer": "Prototype monitoring only — not a substitute for hospital alarm systems or clinical care.",
    }
