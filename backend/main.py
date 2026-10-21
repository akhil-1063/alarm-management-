"""FastAPI application entry point for the ICU monitoring prototype."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from backend.config import FRONTEND_DIR, SESSION_COOKIE, settings
from backend.database import init_db, SessionLocal
from backend.dependencies import get_current_user
from backend.models import User, utcnow
from backend.schemas import HealthResponse
from backend.security import read_session_token

API_PREFIX = "/api/v1"
VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _ensure_admin_exists_note()
    health_task = asyncio.create_task(_device_health_loop())
    yield
    health_task.cancel()
    try:
        await health_task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Background device-health monitor (Section 14: continuous processing)
# Raises TECHNICAL alarms for offline devices and stale data even when no new
# reading arrives (ingest-driven reevaluation cannot run if ingest stopped),
# and re-escalates unacknowledged critical alerts.
# ---------------------------------------------------------------------------
def run_device_health_cycle() -> list[int]:
    """One synchronous health pass. Returns patient IDs to broadcast."""
    from backend.models import Device, SensorReading
    from backend.services import alert_manager
    from backend.services.device_manager import is_device_online

    db = SessionLocal()
    affected: list[int] = []
    try:
        devices = db.execute(
            select(Device).where(Device.is_active.is_(True), Device.patient_user_id.is_not(None))
        ).scalars().all()
        now = utcnow()
        for device in devices:
            patient_id = device.patient_user_id
            assert patient_id is not None
            online = is_device_online(device, now)
            if not online:
                last_seen = device.last_seen_at.isoformat() if device.last_seen_at else "never"
                alert = alert_manager.raise_alert(
                    db,
                    patient_user_id=patient_id,
                    category="device",
                    severity="moderate",
                    title=f"Device offline: {device.device_uid}",
                    message=(
                        f"TECHNICAL ALARM: device {device.device_uid} has not checked in for more than "
                        f"{settings.device_offline_seconds}s (last seen: {last_seen}). Check hotspot, power "
                        "and the BACKEND_URL configured in the ESP32. This is a technical fault — the last "
                        "stored reading is NOT a current live measurement."
                    ),
                    evidence={
                        "device_uid": device.device_uid,
                        "last_seen_at": last_seen,
                        "offline_threshold_seconds": settings.device_offline_seconds,
                        "last_ingest_status": device.last_ingest_status,
                        "last_error": device.last_error,
                    },
                    dedupe_key=f"device_offline:{device.id}",
                )
                if alert:
                    affected.append(patient_id)
                continue

            # Device is checking in but its patient has no fresh reading.
            latest = db.execute(
                select(SensorReading)
                .where(SensorReading.patient_user_id == patient_id)
                .order_by(SensorReading.received_at.desc())
                .limit(1)
            ).scalars().first()
            if latest is None:
                continue  # covered by the offline path or no data yet
            age = (now - latest.received_at).total_seconds()
            if age > settings.stale_seconds:
                alert = alert_manager.raise_alert(
                    db,
                    patient_user_id=patient_id,
                    category="device",
                    severity="moderate",
                    title="Stale readings",
                    message=(
                        f"TECHNICAL ALARM: latest stored reading is {int(age)}s old (stale threshold "
                        f"{settings.stale_seconds}s) while the device reports connectivity. The displayed "
                        "value is the last stored measurement, not a current live one."
                    ),
                    evidence={"age_seconds": round(age, 1), "threshold_seconds": settings.stale_seconds},
                    dedupe_key=f"stale_data:{patient_id}",
                )
                if alert:
                    affected.append(patient_id)

        if alert_manager.escalate_unacknowledged_critical(db):
            affected.extend(
                a.patient_user_id for a in alert_manager.alerts_active_since(db, now)
            )
        db.commit()
    finally:
        db.close()
    return sorted(set(affected))


async def _device_health_loop() -> None:
    interval = max(5, int(settings.device_health_interval_seconds))
    await asyncio.sleep(interval)  # let startup settle (and keep short tests fast)
    while True:
        try:
            patient_ids = await asyncio.to_thread(run_device_health_cycle)
            for pid in patient_ids:
                try:
                    await broadcast_patient(pid)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive: loop must never die
            print(f"[device-health] cycle failed: {exc}")
        await asyncio.sleep(interval)


def _ensure_admin_exists_note() -> None:
    db = SessionLocal()
    try:
        from sqlalchemy import func

        from backend.models import User as U

        count = db.execute(select(func.count()).select_from(U).where(U.role == "admin")).scalar() or 0
        if count == 0:
            print("=" * 74)
            print("  No administrator account exists yet.")
            print("  Create one with:   py -m backend.create_admin")
            print("  (or scripts\\setup.bat which runs it automatically)")
            print("=" * 74)
    finally:
        db.close()


app = FastAPI(
    title="AI-Based ICU Patient Monitoring & Early-Warning System",
    description="Local research/educational prototype. Not a clinically validated diagnostic or life-support system.",
    version=VERSION,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
from backend.api import admin, alerts, analytics, auth, devices, doctors, patients, sensors, test_scenarios

for router in (
    auth.router,
    patients.router,
    doctors.router,
    devices.router,
    sensors.router,
    alerts.router,
    analytics.router,
    admin.router,
    test_scenarios.router,
):
    app.include_router(router, prefix=API_PREFIX)


@app.get(f"{API_PREFIX}/health", response_model=HealthResponse, tags=["system"])
def health():
    db_status = "unknown"
    try:
        db = SessionLocal()
        try:
            from sqlalchemy import text

            db.execute(text("SELECT 1"))
            db_status = "ok"
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover
        db_status = f"error: {exc}"
    return HealthResponse(status="ok" if db_status == "ok" else "degraded",
                          database=db_status, server_time=utcnow(), version=VERSION)


@app.get(f"{API_PREFIX}/config/public")
def public_config():
    """Non-secret configuration the browser needs (never includes device tokens)."""
    return {
        "stale_seconds": settings.stale_seconds,
        "device_offline_seconds": settings.device_offline_seconds,
        "disclaimer": (
            "Research and educational prototype. Not a substitute for clinical assessment, "
            "hospital alarm equipment, or emergency care."
        ),
        "server_time": utcnow(),
    }


# ---------------------------------------------------------------------------
# Live updates: WebSocket per patient + system event stream
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self) -> None:
        self.active: dict[WebSocket, int | str] = {}
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, key: int | str) -> None:
        await ws.accept()
        async with self.lock:
            self.active[ws] = key

    async def disconnect(self, ws: WebSocket) -> None:
        async with self.lock:
            self.active.pop(ws, None)

    async def send(self, message: str, key: int | str) -> None:
        async with self.lock:
            targets = [ws for ws, k in self.active.items() if k == key]
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                await self.disconnect(ws)


manager = ConnectionManager()


def _live_payload(patient_id: int) -> dict:
    from backend.api.shared import latest_reading, latest_risk
    from backend.models import Alert

    db = SessionLocal()
    try:
        reading = latest_reading(db, patient_id)
        risk = latest_risk(db, patient_id)
        alerts = db.execute(
            select(Alert)
            .where(Alert.patient_user_id == patient_id, Alert.status.in_(["active", "acknowledged", "under_review"]))
            .order_by(Alert.created_at.desc())
            .limit(10)
        ).scalars().all()
        now = utcnow()
        age = (now - reading.received_at).total_seconds() if reading else None
        return {
            "patient_user_id": patient_id,
            "server_time": now.isoformat(),
            "reading": {
                "heart_rate": reading.heart_rate if reading and reading.is_valid else None,
                "spo2": reading.spo2 if reading and reading.is_valid else None,
                "ambient_temperature": reading.ambient_temperature if reading else None,
                "ambient_humidity": reading.ambient_humidity if reading else None,
                "signal_quality": reading.signal_quality if reading else None,
                "is_valid": reading.is_valid if reading else False,
                "received_at": reading.received_at.isoformat() if reading else None,
                "is_simulated": reading.is_simulated if reading else False,
            } if reading else None,
            "data_age_seconds": age,
            "stale": (age is None) or age > settings.stale_seconds,
            "risk": {
                "category": risk.risk_category if risk else "low",
                "score": risk.risk_score if risk else 0.0,
                "reasons": json.loads(risk.reasons) if risk and risk.reasons else [],
                "assessed_at": risk.assessed_at.isoformat() if risk else None,
            },
            "active_alerts": [
                {"id": a.id, "severity": a.severity, "title": a.title, "message": a.message,
                 "category": a.category, "status": a.status, "created_at": a.created_at.isoformat(),
                 "is_simulated": a.is_simulated}
                for a in alerts
            ],
        }
    finally:
        db.close()


@app.websocket(f"{API_PREFIX}/ws/live/{{patient_id}}")
async def ws_live(ws: WebSocket, patient_id: int):
    """Live per-patient updates. Authorization: session cookie checked at connect."""
    cookie = ws.cookies.get(SESSION_COOKIE)
    data = read_session_token(cookie) if cookie else None
    if not data:
        await ws.close(code=4401, reason="unauthenticated")
        return
    db = SessionLocal()
    try:
        user = db.get(User, data.get("uid"))
        if not user or not user.is_active:
            await ws.close(code=4401, reason="unauthenticated")
            return
        # Enforce the same isolation rules as REST endpoints
        from backend.api.shared import ensure_patient_visible

        try:
            ensure_patient_visible(db, user, patient_id)
        except HTTPException:
            await ws.close(code=4403, reason="forbidden")
            return
    finally:
        db.close()

    await manager.connect(ws, patient_id)
    try:
        await ws.send_text(json.dumps(_live_payload(patient_id), default=str))
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps(_live_payload(patient_id), default=str))
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception:
        await manager.disconnect(ws)


async def broadcast_patient(patient_id: int) -> None:
    payload = _live_payload(patient_id)
    await manager.send(json.dumps(payload, default=str), patient_id)


@app.post(f"{API_PREFIX}/internal/notify", include_in_schema=False)
async def internal_notify(payload: dict):
    """Internal: notify running dashboards after a write (called by ingest path)."""
    pid = payload.get("patient_user_id")
    if pid:
        await broadcast_patient(int(pid))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Static frontend hosting (backend serves the site — no CORS setup needed)
# ---------------------------------------------------------------------------
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/{page_name}.html", include_in_schema=False)
    def page(page_name: str):
        safe = os.path.basename(page_name)
        candidate = FRONTEND_DIR / f"{safe}.html"
        if candidate.is_file():
            return FileResponse(str(candidate))
        return JSONResponse({"detail": "Not found"}, status_code=404)
else:  # pragma: no cover
    @app.get("/", include_in_schema=False)
    def index_missing():
        return JSONResponse({"detail": "frontend/ directory not found"}, status_code=500)


@app.middleware("http")
async def no_store_frontend(request: Request, call_next):
    """Always revalidate HTML/JS/CSS so edited pages and scripts are never stale."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets") or path.endswith(".html") or path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def run() -> None:
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=bool(settings.debug),
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    run()
