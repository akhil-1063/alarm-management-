"""Device registration, assignment, listing and connectivity test."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.dependencies import require_admin, require_admin_or_doctor, require_doctor
from backend.models import AuditLog, Device, User, utcnow
from backend.schemas import (
    DeviceAssignRequest,
    DeviceDataMapOut,
    DeviceOut,
    DeviceRegistrationRequest,
    DeviceTokenResponse,
    DirectFetchConfigOut,
    DirectFetchConfigRequest,
    DirectFetchProbe,
    IngestResponse,
)
from backend.services.device_manager import is_device_online
from backend.services.device_data_mapper import map_device_data
from backend.services.device_fetcher import check_endpoint_allowed, fetch_device_data, normalize_endpoint
from backend.services.ingest_service import store_ingest

router = APIRouter(prefix="/devices", tags=["devices"])


def _device_out(db: Session, device: Device) -> dict:
    data = DeviceOut.model_validate(device).model_dump()
    data["is_online"] = is_device_online(device)
    data["auth_status"] = "active" if device.is_active else "deactivated"

    patient = db.get(User, device.patient_user_id) if device.patient_user_id else None
    data["patient_name"] = patient.full_name if patient else None

    # Freshness of the patient's latest stored reading (never interpolate).
    last_reading_at = None
    if device.patient_user_id:
        from backend.models import SensorReading

        row = db.execute(
            select(SensorReading.received_at)
            .where(SensorReading.patient_user_id == device.patient_user_id)
            .order_by(SensorReading.received_at.desc())
            .limit(1)
        ).first()
        if row:
            last_reading_at = row[0]
    data["last_reading_at"] = last_reading_at
    if last_reading_at:
        age = (utcnow() - last_reading_at).total_seconds()
        data["data_age_seconds"] = round(age, 1)
        data["data_freshness"] = "fresh" if age <= settings.stale_seconds else "stale"
    else:
        data["data_age_seconds"] = None
        data["data_freshness"] = "no_data"
    return data


@router.get("", response_model=list[DeviceOut])
def list_devices(db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    devices = db.execute(select(Device).order_by(Device.created_at.desc())).scalars().all()
    return [_device_out(db, d) for d in devices]


@router.post("/register", response_model=DeviceTokenResponse, status_code=201)
def register_device(
    payload: DeviceRegistrationRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    """Register a new ESP32 device. The device_token is returned ONLY here and
    at explicit re-creation; it is never included in ordinary list responses."""
    existing = db.execute(select(Device).where(Device.device_uid == payload.device_uid)).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="A device with this UID is already registered")

    device = Device(device_uid=payload.device_uid, name=payload.name, notes=payload.notes, is_active=True)
    db.add(device)
    db.add(AuditLog(
        actor_user_id=user.id, actor_label=user.email, action="device_registered",
        entity_type="device", entity_id=payload.device_uid, detail=payload.name,
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    db.refresh(device)
    data = DeviceTokenResponse.model_validate(device).model_dump()
    data["is_online"] = is_device_online(device)
    return data


@router.post("/{device_id}/rotate-token", response_model=DeviceTokenResponse)
def rotate_token(device_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from backend.models import generate_device_token

    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    device.device_token = generate_device_token()
    db.add(device)
    db.add(AuditLog(
        actor_user_id=user.id, actor_label=user.email, action="device_token_rotated",
        entity_type="device", entity_id=device.device_uid,
    ))
    db.commit()
    db.refresh(device)
    data = DeviceTokenResponse.model_validate(device).model_dump()
    data["is_online"] = is_device_online(device)
    return data


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(device_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_out(db, device)


@router.post("/{device_id}/assign")
def assign_device(
    device_id: int,
    payload: DeviceAssignRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    patient = db.get(User, payload.patient_user_id)
    if not patient or patient.role != "patient":
        raise HTTPException(status_code=404, detail="Patient not found")

    # Enforce: only one patient per device; only one device writes to a patient.
    from backend.models import SensorReading

    conflict = db.execute(
        select(Device).where(Device.patient_user_id == patient.id, Device.id != device.id)
    ).scalars().first()
    if conflict:
        # Remove the old assignment to prevent accidental multi-patient writes
        conflict.patient_user_id = None
        db.add(conflict)

    device.patient_user_id = patient.id
    db.add(device)
    db.add(AuditLog(
        actor_user_id=user.id, actor_label=user.email, action="device_assigned",
        entity_type="device", entity_id=device.device_uid,
        detail=f"assigned to patient_user_id={patient.id}",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    return {"status": "assigned", "device_uid": device.device_uid, "patient_user_id": patient.id}


@router.post("/{device_id}/unassign")
def unassign_device(device_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    device.patient_user_id = None
    db.add(device)
    db.add(AuditLog(
        actor_user_id=user.id, actor_label=user.email, action="device_unassigned",
        entity_type="device", entity_id=device.device_uid,
    ))
    db.commit()
    return {"status": "unassigned"}


@router.post("/{device_id}/test")
def test_device(device_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin_or_doctor)):
    """Connectivity test: verifies the device is registered, active and recently seen."""
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    online = is_device_online(device)
    return {
        "device_uid": device.device_uid,
        "is_active": device.is_active,
        "online": online,
        "last_seen_at": device.last_seen_at,
        "ip_address": device.ip_address,
        "assigned_patient_user_id": device.patient_user_id,
        "last_ingest_at": device.last_ingest_at,
        "last_ingest_status": device.last_ingest_status,
        "last_error": device.last_error,
        "message": (
            "Device is online and sending data."
            if online else
            "Device has not checked in recently. Verify the ESP32 is powered, connected to the hotspot, "
            "and points at this laptop's hotspot IPv4 address and port in its config."
        ),
    }


# ---------------------------------------------------------------------------
# ESP8266 direct /data polling (MedicAssist integration)
#
# The browser cannot reliably read JSON from the ESP over plain HTTP (HTTPS
# dashboard => mixed-content block; firmware sends no CORS headers), so the
# browser asks THIS server to fetch the device. The server enforces a
# private-network allowlist. The user always types the device IP — it is never
# hardcoded, and it is persisted per device so it survives restarts.
# ---------------------------------------------------------------------------


def _fetch_config_out(device: Device) -> dict:
    host, port, err = normalize_endpoint(device.data_endpoint)
    note = ""
    if err:
        note = "No device endpoint configured yet."
    elif check_endpoint_allowed(host):
        note = "Configured endpoint is outside the allowed local-network range and will be refused."
    return {
        "device_id": device.id,
        "device_uid": device.device_uid,
        "endpoint": device.data_endpoint,
        "enabled": device.direct_fetch_enabled,
        "host": host,
        "port": port,
        "note": note,
    }


@router.post("/{device_id}/fetch-config", response_model=DirectFetchConfigOut)
def set_fetch_config(
    device_id: int,
    payload: DirectFetchConfigRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    """Persist the ESP8266 /data endpoint (host[:port]) for a device.

    The endpoint is validated against the local-network allowlist now so the
    operator finds out immediately if it can never be fetched.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    endpoint = (payload.endpoint or "").strip() or None
    if endpoint:
        host, _port, err = normalize_endpoint(endpoint)
        if err:
            raise HTTPException(status_code=400, detail=err)
        blocked = check_endpoint_allowed(host)
        if blocked:
            raise HTTPException(status_code=400, detail=blocked)

    device.data_endpoint = endpoint
    device.direct_fetch_enabled = bool(payload.enabled) and endpoint is not None
    if endpoint:
        device.ip_address = endpoint
    db.add(device)
    db.add(AuditLog(
        actor_user_id=user.id, actor_label=user.email, action="device_fetch_config_updated",
        entity_type="device", entity_id=device.device_uid, detail=endpoint or "cleared",
        ip_address=request.client.host if request.client else None,
    ))
    db.commit()
    return _fetch_config_out(device)


@router.get("/{device_id}/fetch-config", response_model=DirectFetchConfigOut)
def get_fetch_config(device_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_admin_or_doctor)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return _fetch_config_out(device)


@router.post("/{device_id}/fetch-preview", response_model=DirectFetchProbe)
def fetch_preview(device_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require_admin_or_doctor)):
    """Fetch ``GET /data`` from the device and return the RAW JSON (no storage)."""
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.data_endpoint:
        return DirectFetchProbe(ok=False, status="disabled",
                                error="No device endpoint configured. Save the ESP8266 IP first.")
    result = fetch_device_data(device.data_endpoint)
    return DirectFetchProbe(
        ok=result.ok, status=result.status, http_status=result.http_status,
        latency_ms=result.latency_ms, error=result.error, endpoint=result.endpoint,
        data=result.data,
    )


@router.post("/{device_id}/fetch-ingest", response_model=IngestResponse)
def fetch_and_ingest(device_id: int, background_tasks: BackgroundTasks,
                     db: Session = Depends(get_db),
                     user: User = Depends(require_admin_or_doctor)):
    """Fetch ``GET /data`` from the device, map it, validate it and STORE it.

    This is the browser-triggered ingestion path used when a direct browser fetch
    is not possible. It reuses the exact same validation/storage pipeline as the
    ESP32 POST /ingest path; the patient is resolved from the device assignment.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.data_endpoint:
        return IngestResponse(status="error", message="No device endpoint configured.")

    result = fetch_device_data(device.data_endpoint)
    if not result.ok or result.data is None:
        device.last_ingest_at = utcnow()
        device.last_ingest_status = "error"
        device.last_error = f"Direct fetch failed ({result.status}): {result.error}"
        db.add(device)
        db.commit()
        return IngestResponse(
            status="error",
            message=f"Direct fetch failed ({result.status}): {result.error}",
        )

    reading, notes = map_device_data(result.data)

    # A fetched reading is a real device reading unless the device itself says
    # the BP channel is simulated — that flag travels on the payload separately.
    stored = store_ingest(db, device, reading, is_simulated=False)
    if stored.status == "stored_unassigned":
        return IngestResponse(status="stored_unassigned",
                              message="Fetched successfully but the device is not assigned to a patient.")
    if not stored.reading:
        return IngestResponse(status="error", message="Reading was not stored.")

    from backend.api.sensors import _broadcast

    background_tasks.add_task(_broadcast, device.patient_user_id)
    return IngestResponse(
        status="stored",
        reading_id=stored.reading.id,
        message=("ok" if stored.is_valid else f"stored with validation notes: {stored.notes}")
                + (f" [mapping: {'; '.join(notes)}]" if notes else ""),
    )


@router.post("/{device_id}/data-map", response_model=DeviceDataMapOut)
def preview_data_map(device_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_admin_or_doctor)):
    """Fetch and show how the raw device JSON maps to platform fields (no storage)."""
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.data_endpoint:
        raise HTTPException(status_code=400, detail="No device endpoint configured.")
    result = fetch_device_data(device.data_endpoint)
    if not result.ok or result.data is None:
        raise HTTPException(status_code=502, detail=f"Direct fetch failed ({result.status}): {result.error}")
    mapped, notes = map_device_data(result.data)
    return DeviceDataMapOut(raw=result.data, mapped=mapped, mapping_notes=notes)

