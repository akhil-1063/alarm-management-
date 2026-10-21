"""Administrator/developer test panel: named simulated scenarios.

Test readings are ALWAYS stored with is_simulated=True so they can never be
confused with real ESP32 measurements; charts, exports and analytics label or
separate them explicitly.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import require_admin_or_doctor
from backend.models import Device, RiskAssessment, SensorReading, StrokeSymptomEntry, User, utcnow
from backend.schemas import ScenarioRequest
from backend.api.shared import recompute_risk_for_patient
from backend.services import alert_manager

router = APIRouter(prefix="/test", tags=["test-scenarios"])

SCENARIOS: dict[str, dict] = {
    "normal": {
        "description": "Normal resting vitals with good signal quality.",
        "readings": [
            {"heart_rate": 76, "spo2": 98, "ambient_temperature": 24.0, "ambient_humidity": 48, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 74, "spo2": 98, "ambient_temperature": 24.1, "ambient_humidity": 47, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 78, "spo2": 97, "ambient_temperature": 24.0, "ambient_humidity": 48, "signal_quality": "good", "finger_detected": True},
        ],
    },
    "sustained_high_hr": {
        "description": "Sustained elevated heart rate (tachycardia pattern) with good SpO2.",
        "readings": [
            {"heart_rate": 128, "spo2": 97, "ambient_temperature": 24.0, "ambient_humidity": 48, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 132, "spo2": 97, "ambient_temperature": 24.0, "ambient_humidity": 48, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 135, "spo2": 96, "ambient_temperature": 24.0, "ambient_humidity": 48, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 140, "spo2": 96, "ambient_temperature": 24.0, "ambient_humidity": 48, "signal_quality": "good", "finger_detected": True},
        ],
    },
    "low_spo2": {
        "description": "Low SpO2 estimate (possible desaturation) with normal heart rate.",
        "readings": [
            {"heart_rate": 88, "spo2": 92, "ambient_temperature": 23.5, "ambient_humidity": 50, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 90, "spo2": 91, "ambient_temperature": 23.5, "ambient_humidity": 50, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 92, "spo2": 90, "ambient_temperature": 23.5, "ambient_humidity": 50, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 95, "spo2": 89, "ambient_temperature": 23.5, "ambient_humidity": 50, "signal_quality": "good", "finger_detected": True},
        ],
    },
    "critical_combined": {
        "description": "Critical combination: very low SpO2 with abnormal heart rate.",
        "readings": [
            {"heart_rate": 45, "spo2": 86, "ambient_temperature": 23.0, "ambient_humidity": 52, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 43, "spo2": 85, "ambient_temperature": 23.0, "ambient_humidity": 52, "signal_quality": "good", "finger_detected": True},
            {"heart_rate": 42, "spo2": 84, "ambient_temperature": 23.0, "ambient_humidity": 52, "signal_quality": "good", "finger_detected": True},
        ],
    },
    "invalid_signal": {
        "description": "MAX30102 reports poor signal quality — values must not trigger clinical alarms.",
        "readings": [
            {"heart_rate": 0, "spo2": 0, "ambient_temperature": 24.0, "ambient_humidity": 45, "signal_quality": "poor", "finger_detected": True},
            {"heart_rate": None, "spo2": None, "ambient_temperature": 24.0, "ambient_humidity": 45, "signal_quality": "invalid", "finger_detected": True},
        ],
    },
    "finger_removed": {
        "description": "Finger removed from MAX30102 — no pulse measurement available.",
        "readings": [
            {"heart_rate": None, "spo2": None, "ambient_temperature": 24.0, "ambient_humidity": 45, "signal_quality": "no_finger", "finger_detected": False},
        ],
    },
    "sensor_disconnected": {
        "description": "No readings submitted (simulates device disconnection). Existing data becomes stale.",
        "readings": [],
        "simulate_no_data": True,
    },
    "hot_ambient": {
        "description": "Ambient temperature/humidity outside comfortable range (environmental context only).",
        "readings": [
            {"heart_rate": 80, "spo2": 98, "ambient_temperature": 42.0, "ambient_humidity": 88, "signal_quality": "good", "finger_detected": True},
        ],
    },
    "device_network_failure": {
        "description": "Simulates an HTTP/network failure path: no ingest performed; device should show offline after threshold.",
        "readings": [],
        "simulate_no_data": True,
    },
    "patient_reassignment": {
        "description": "Marks the target device as unassigned for 5 seconds, then restores it — verifies isolation on reassignment.",
        "readings": [],
        "reassign_probe": True,
    },
    "stroke_symptoms": {
        "description": "Clinician-entered possible stroke indicators (facial asymmetry + one-sided weakness) — immediate urgent warning, sensor-independent.",
        "readings": [],
        "stroke_symptom": True,
    },
}


@router.get("/scenarios")
def list_scenarios(user: User = Depends(require_admin_or_doctor)):
    return {
        "scenarios": [
            {"id": k, "description": v["description"], "simulated": True}
            for k, v in SCENARIOS.items()
        ],
        "notice": "All scenario data is stored with is_simulated=True and never mixed into real patient measurement statistics.",
    }


@router.post("/scenarios")
def run_scenario(payload: ScenarioRequest, db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    scenario_id = payload.scenario
    if scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"Unknown scenario. Available: {sorted(SCENARIOS)}")

    # Resolve target device + patient
    device = None
    if payload.device_uid:
        device = db.execute(select(Device).where(Device.device_uid == payload.device_uid)).scalars().first()
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
    else:
        device = db.execute(select(Device).where(Device.patient_user_id.is_not(None))).scalars().first()
        if not device:
            raise HTTPException(status_code=400, detail="No assigned device found. Assign a device to a patient first.")

    patient_id = payload.target_patient_user_id or device.patient_user_id
    if not patient_id:
        raise HTTPException(status_code=400, detail="Target patient is required (device is unassigned).")

    spec = SCENARIOS[scenario_id]
    created: list[int] = []

    if spec.get("reassign_probe"):
        original = device.patient_user_id
        device.patient_user_id = None
        db.add(device)
        db.commit()
        device.patient_user_id = original
        db.add(device)
        db.commit()
        return {
            "status": "ok",
            "scenario": scenario_id,
            "simulated": True,
            "message": "Device assignment was briefly cleared and restored; no readings were written during the unassigned window.",
        }

    if spec.get("stroke_symptom"):
        entry = StrokeSymptomEntry(
            patient_user_id=patient_id,
            entered_by=user.id,
            facial_asymmetry=True,
            one_sided_weakness=True,
            speech_difficulty=False,
            sudden_neuro_change=False,
            free_text="SIMULATED TEST DATA — scenario 'stroke_symptoms'. Not a real clinical observation.",
            is_simulated=True,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        flags = ["facial asymmetry", "one-sided arm/leg weakness"]
        alert = alert_manager.raise_alert(
            db,
            patient_user_id=patient_id,
            category="stroke_suspect",
            severity="critical",
            title="URGENT: possible stroke symptoms entered by clinician (SIMULATED TEST)",
            message=(
                "A clinician has entered indicators consistent with possible stroke: "
                + ", ".join(flags)
                + ". Immediate clinical assessment is required. Generated from the clinician's "
                "structured symptom entry — NOT from the ESP32 sensors, which cannot detect stroke. "
                "This particular alert is SIMULATED TEST DATA (scenario 'stroke_symptoms')."
            ),
            evidence={"flags": flags, "scenario": scenario_id, "entry_id": entry.id},
            dedupe_key=f"stroke_suspect:{patient_id}",
            is_simulated=True,
        )
        return {
            "status": "ok",
            "scenario": scenario_id,
            "simulated": True,
            "message": spec["description"],
            "stroke_entry_id": entry.id,
            "alert_id": alert.id if alert else None,
            "notice": "Simulated clinician entry — stored with is_simulated=True and excluded from real statistics.",
        }

    if spec.get("simulate_no_data"):
        # Age existing real data to make staleness visible, without inventing readings.
        stale_alert = alert_manager.raise_alert(
            db,
            patient_user_id=patient_id,
            category="device",
            severity="moderate",
            title=f"[TEST] Simulated data interruption: {scenario_id}",
            message=(
                f"Test scenario '{scenario_id}' ({spec['description']}) is active. "
                "No simulated readings were created; existing data will appear stale once its age "
                "exceeds the configured threshold. This alert is clearly marked as simulated."
            ),
            evidence={"scenario": scenario_id},
            dedupe_key=f"test_scenario:{scenario_id}:{patient_id}",
            is_simulated=True,
        )
        return {
            "status": "ok",
            "scenario": scenario_id,
            "simulated": True,
            "message": "No new readings created (this scenario simulates absence of data). A simulated alert was raised.",
            "alert_id": stale_alert.id if stale_alert else None,
        }

    for idx, spec_reading in enumerate(spec["readings"]):
        reading = SensorReading(
            device_id=device.id,
            patient_user_id=patient_id,
            received_at=utcnow(),
            heart_rate=spec_reading.get("heart_rate"),
            spo2=spec_reading.get("spo2"),
            ambient_temperature=spec_reading.get("ambient_temperature"),
            ambient_humidity=spec_reading.get("ambient_humidity"),
            signal_quality=spec_reading.get("signal_quality", "unknown"),
            is_valid=True,
            validity_notes=f"SIMULATED TEST DATA — scenario '{scenario_id}', sample {idx + 1}. Not a real measurement.",
            firmware_version="test-scenario",
            is_simulated=True,  # <— never mixed with real measurements
            raw_payload=json.dumps({"scenario": scenario_id, **spec_reading}),
        )
        db.add(reading)
        db.commit()
        db.refresh(reading)
        created.append(reading.id)

    result, assessment, opened = recompute_risk_for_patient(db, patient_id, is_simulated=True)

    return {
        "status": "ok",
        "scenario": scenario_id,
        "simulated": True,
        "message": spec["description"],
        "readings_created": created,
        "risk_category": result.category,
        "risk_reasons": result.reasons,
        "alerts_opened": [a.id for a in opened],
        "notice": "All created readings are flagged is_simulated=True and excluded from real-measurement statistics.",
    }


@router.post("/clear-simulated/{patient_id}")
def clear_simulated(patient_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    """Remove simulated readings/alerts/assessments for a patient (real data untouched)."""
    from backend.models import Alert

    n1 = db.query(SensorReading).filter(
        SensorReading.patient_user_id == patient_id, SensorReading.is_simulated.is_(True)
    ).delete(synchronize_session=False)
    n2 = db.query(RiskAssessment).filter(
        RiskAssessment.patient_user_id == patient_id, RiskAssessment.is_simulated.is_(True)
    ).delete(synchronize_session=False)
    n3 = db.query(Alert).filter(
        Alert.patient_user_id == patient_id, Alert.is_simulated.is_(True)
    ).delete(synchronize_session=False)
    n4 = db.query(StrokeSymptomEntry).filter(
        StrokeSymptomEntry.patient_user_id == patient_id, StrokeSymptomEntry.is_simulated.is_(True)
    ).delete(synchronize_session=False)
    db.commit()
    return {
        "status": "cleared",
        "simulated_readings_deleted": n1,
        "simulated_assessments_deleted": n2,
        "simulated_alerts_deleted": n3,
        "simulated_stroke_entries_deleted": n4,
    }
