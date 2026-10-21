"""Shared reading-persistence service.

Both the ESP32 token-authenticated ``POST /ingest`` path and the ESP8266 direct
fetch path funnel through :func:`store_ingest`. The patient is always resolved
server-side from the device's own assignment — a device (or a fetched payload)
can never choose which patient record it writes into.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from backend.api.shared import recompute_risk_for_patient
from backend.models import Device, SensorReading, utcnow
from backend.schemas import IngestReading
from backend.services.device_manager import mark_seen
from backend.services.sensor_validation import validate_reading


@dataclass
class StoreResult:
    status: str                       # stored | stored_unassigned
    reading: SensorReading | None = None
    is_valid: bool = False
    notes: str | None = None


def parse_device_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def store_ingest(
    db: Session,
    device: Device,
    payload: IngestReading,
    *,
    is_simulated: bool = False,
) -> StoreResult:
    """Validate and persist one reading for the device's assigned patient.

    ``is_simulated`` marks the whole record: device ingest is never simulated,
    but a reading assembled from a user-provided/simulated source can be.
    The ``bp_simulated`` flag travels with the payload independently.
    """
    mark_seen(device, ip_address=payload.ip_address, firmware_version=payload.firmware_version)
    device.last_ingest_at = utcnow()
    device.last_ingest_status = "stored"
    device.last_error = None
    db.add(device)
    db.commit()

    if device.patient_user_id is None:
        device.last_ingest_status = "stored_unassigned"
        device.last_error = "Device is not assigned to a patient; the reading was discarded."
        db.add(device)
        db.commit()
        return StoreResult(status="stored_unassigned")

    patient_user_id = device.patient_user_id

    result = validate_reading(
        heart_rate=payload.heart_rate,
        spo2=payload.spo2,
        ambient_temperature=payload.ambient_temperature,
        ambient_humidity=payload.ambient_humidity,
        signal_quality=payload.signal_quality,
        finger_detected=payload.finger_detected,
        systolic_bp=payload.systolic_bp,
        diastolic_bp=payload.diastolic_bp,
        bp_simulated=payload.bp_simulated,
    )

    reading = SensorReading(
        device_id=device.id,
        patient_user_id=patient_user_id,
        received_at=utcnow(),
        device_timestamp=parse_device_timestamp(payload.device_timestamp),
        heart_rate=result.heart_rate,
        spo2=result.spo2,
        ambient_temperature=result.ambient_temperature,
        ambient_humidity=result.ambient_humidity,
        systolic_bp=result.systolic_bp,
        diastolic_bp=result.diastolic_bp,
        bp_unit=payload.bp_unit or "mmHg",
        bp_simulated=result.bp_simulated,
        bp_pot_raw=payload.bp_pot_raw,
        finger_present=bool(payload.finger_detected),
        hr_valid=bool(payload.hr_valid) if payload.hr_valid is not None else result.hr_usable,
        spo2_valid=bool(payload.spo2_valid) if payload.spo2_valid is not None else result.spo2_usable,
        buzzer_alert=bool(payload.buzzer_alert),
        signal_quality=result.signal_quality,
        is_valid=result.is_valid,
        validity_notes=result.notes_joined,
        firmware_version=payload.firmware_version,
        is_simulated=is_simulated,
        raw_payload=json.dumps(payload.model_dump(), default=str),
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)

    recompute_risk_for_patient(db, patient_user_id, is_simulated=is_simulated)
    return StoreResult(status="stored", reading=reading, is_valid=result.is_valid, notes=result.notes_joined)
