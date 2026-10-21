"""Tests for the ESP8266 direct-fetch integration and simulated-BP handling.

Covers:
* normalising / validating the user-entered endpoint (SSRF allowlist).
* mapping raw firmware /data JSON to platform fields.
* the /data mapping: null stays null (never zero), finger/validity flags.
* storing a fetched reading through fetch-ingest.
* simulated BP never raises the clinical risk category.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.models import SensorReading, utcnow
from backend.services.device_fetcher import check_endpoint_allowed, normalize_endpoint
from backend.services.device_data_mapper import map_device_data
from backend.services.sensor_validation import validate_reading
from backend.tests.conftest import create_device, ingest, register_patient


def _uid(email: str) -> int:
    from backend.database import SessionLocal
    from backend.models import User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        assert u, f"user {email} not found"
        return u.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Endpoint normalisation + SSRF allowlist
# ---------------------------------------------------------------------------
def test_normalize_endpoint_variants():
    assert normalize_endpoint("10.92.127.134")[0:2] == ("10.92.127.134", 80)
    assert normalize_endpoint("10.92.127.134:8080")[0:2] == ("10.92.127.134", 8080)
    assert normalize_endpoint("http://192.168.43.12/")[0:2] == ("192.168.43.12", 80)
    host, port, err = normalize_endpoint("")
    assert host is None and err


def test_private_allowlist_blocks_public_hosts():
    assert check_endpoint_allowed("10.92.127.134") is None
    assert check_endpoint_allowed("192.168.43.12") is None
    assert check_endpoint_allowed("8.8.8.8") is not None          # public
    assert check_endpoint_allowed("example.com") is not None      # not an IP


# ---------------------------------------------------------------------------
# Raw /data JSON -> platform fields
# ---------------------------------------------------------------------------
DEVICE_JSON = {
    "device": "ESP8266-ICU-001",
    "wifi_connected": True,
    "ip": "10.92.127.134",
    "heart_rate": 78,
    "spo2": 97,
    "ambient_temperature_c": 28.4,
    "humidity_percent": 61.0,
    "finger_present": True,
    "hr_valid": True,
    "spo2_valid": True,
    "systolic_bp": 132,
    "diastolic_bp": 84,
    "bp_unit": "mmHg",
    "bp_simulated": True,
    "bp_pot_raw": 512,
    "bp_alert": False,
    "hr_alert": False,
    "spo2_alert": False,
    "buzzer_alert": False,
    "raw_ir": 123456,
    "raw_red": 65432,
    "uptime_ms": 123456,
}


def test_map_device_data_translates_fields():
    reading, notes = map_device_data(DEVICE_JSON)
    assert reading.heart_rate == 78
    assert reading.spo2 == 97
    assert reading.ambient_temperature == 28.4
    assert reading.ambient_humidity == 61.0
    assert reading.systolic_bp == 132
    assert reading.diastolic_bp == 84
    assert reading.bp_simulated is True
    assert reading.bp_pot_raw == 512
    assert reading.finger_detected is True
    assert reading.signal_quality == "good"


def test_map_device_data_null_stays_none_not_zero():
    raw = dict(DEVICE_JSON)
    raw["heart_rate"] = None
    raw["spo2"] = None
    raw["hr_valid"] = False
    raw["spo2_valid"] = False
    reading, _ = map_device_data(raw)
    assert reading.heart_rate is None
    assert reading.spo2 is None
    assert reading.signal_quality == "poor"


def test_map_device_data_marks_unflagged_bp_simulated():
    raw = {k: v for k, v in DEVICE_JSON.items() if k != "bp_simulated"}
    reading, notes = map_device_data(raw)
    assert reading.bp_simulated is True
    assert any("simulated" in n.lower() for n in notes)


# ---------------------------------------------------------------------------
# Validation of simulated BP
# ---------------------------------------------------------------------------
def test_validate_reading_bp_simulated_never_usable():
    res = validate_reading(
        heart_rate=80, spo2=98, ambient_temperature=25, ambient_humidity=50,
        signal_quality="good", finger_detected=True,
        systolic_bp=200, diastolic_bp=120, bp_simulated=True,
    )
    assert res.bp_simulated is True
    assert res.bp_usable is True
    assert any("SIMULATED" in n for n in res.notes)


def test_validate_reading_bp_only_is_not_clinically_valid():
    res = validate_reading(
        heart_rate=None, spo2=None, ambient_temperature=None, ambient_humidity=None,
        signal_quality="no_finger", finger_detected=False,
        systolic_bp=130, diastolic_bp=85, bp_simulated=True,
    )
    assert res.is_valid is False
    assert res.hr_usable is False


# ---------------------------------------------------------------------------
# Direct fetch endpoints (device_fetcher mocked)
# ---------------------------------------------------------------------------
def _setup(admin_client: TestClient, client: TestClient, email: str, uid: str):
    register_patient(client, email)
    device = create_device(admin_client, uid)
    admin_client.post(f"/api/v1/devices/{device['id']}/assign", json={"patient_user_id": _uid(email)})
    return _uid(email), device


def test_fetch_config_persists_and_validates(admin_client: TestClient, client: TestClient):
    _, device = _setup(admin_client, client, "esp_cfg@example.org", "ESP8266-CFG-1")
    r = admin_client.post(f"/api/v1/devices/{device['id']}/fetch-config",
                          json={"device_id": device["id"], "endpoint": "10.92.127.134", "enabled": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == "10.92.127.134"
    assert body["enabled"] is True

    # A public host must be refused.
    bad = admin_client.post(f"/api/v1/devices/{device['id']}/fetch-config",
                            json={"device_id": device["id"], "endpoint": "8.8.8.8", "enabled": True})
    assert bad.status_code == 400

    got = admin_client.get(f"/api/v1/devices/{device['id']}/fetch-config")
    assert got.json()["endpoint"] == "10.92.127.134"


def test_fetch_ingest_stores_mapped_reading(admin_client: TestClient, client: TestClient, monkeypatch):
    pid, device = _setup(admin_client, client, "esp_ing@example.org", "ESP8266-ING-1")
    admin_client.post(f"/api/v1/devices/{device['id']}/fetch-config",
                      json={"device_id": device["id"], "endpoint": "10.92.127.134", "enabled": True})

    import backend.api.devices as devices_api
    from backend.services.device_fetcher import FetchResult

    def fake_fetch(endpoint, path="/data"):
        return FetchResult(ok=True, status="ok", endpoint=endpoint, data=DEVICE_JSON)

    monkeypatch.setattr(devices_api, "fetch_device_data", fake_fetch)

    r = admin_client.post(f"/api/v1/devices/{device['id']}/fetch-ingest")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "stored"

    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        reading = (db.query(SensorReading)
                   .filter(SensorReading.patient_user_id == pid)
                   .order_by(SensorReading.received_at.desc()).first())
        assert reading is not None
        assert reading.heart_rate == 78
        assert reading.bp_simulated is True
        assert reading.systolic_bp == 132
        assert reading.buzzer_alert is False
    finally:
        db.close()


def test_fetch_ingest_reports_error_when_unreachable(admin_client: TestClient, client: TestClient, monkeypatch):
    _, device = _setup(admin_client, client, "esp_err@example.org", "ESP8266-ERR-1")
    admin_client.post(f"/api/v1/devices/{device['id']}/fetch-config",
                      json={"device_id": device["id"], "endpoint": "10.92.127.134", "enabled": True})

    import backend.api.devices as devices_api
    from backend.services.device_fetcher import FetchResult

    monkeypatch.setattr(devices_api, "fetch_device_data",
                        lambda endpoint, path="/data": FetchResult(ok=False, status="timeout", error="no response"))
    r = admin_client.post(f"/api/v1/devices/{device['id']}/fetch-ingest")
    assert r.status_code == 200
    assert r.json()["status"] == "error"
    assert "timeout" in r.json()["message"]


# ---------------------------------------------------------------------------
# Simulated BP must NOT raise the clinical risk category
# ---------------------------------------------------------------------------
def test_simulated_bp_does_not_raise_risk_category(admin_client: TestClient, client: TestClient):
    pid, device = _setup(admin_client, client, "esp_bp@example.org", "ESP8266-BP-1")
    # Normal vitals but an extreme SIMULATED BP.
    resp = ingest(client, device["device_token"], {
        "heart_rate": 78, "spo2": 98, "signal_quality": "good", "finger_detected": True,
        "systolic_bp": 220, "diastolic_bp": 140, "bp_simulated": True, "bp_pot_raw": 1023,
    })
    assert resp.status_code == 200, resp.text

    from backend.database import SessionLocal
    from backend.models import RiskAssessment
    import json

    db = SessionLocal()
    try:
        row = (db.query(RiskAssessment)
               .filter(RiskAssessment.patient_user_id == pid)
               .order_by(RiskAssessment.assessed_at.desc()).first())
        assert row is not None
        assert row.risk_category == "low", f"simulated BP wrongly raised category: {row.risk_category}"
        modules = json.loads(row.evidence)["modules"]
        assert "simulated_blood_pressure" in modules
        assert modules["simulated_blood_pressure"]["status"] in ("simulated_within_range", "simulated_threshold_crossed")
        assert modules["simulated_blood_pressure"]["evidence"]["bp_simulated"] is True
    finally:
        db.close()


def test_live_snapshot_includes_simulated_bp_fields(admin_client: TestClient, client: TestClient):
    pid, device = _setup(admin_client, client, "esp_live@example.org", "ESP8266-LIVE-1")
    ingest(client, device["device_token"], {
        "heart_rate": 80, "spo2": 97, "signal_quality": "good", "finger_detected": True,
        "systolic_bp": 120, "diastolic_bp": 80, "bp_simulated": True,
    })
    r = admin_client.get(f"/api/v1/live/{pid}")
    assert r.status_code == 200, r.text
    reading = r.json()["reading"]
    assert reading["bp_simulated"] is True
    assert reading["systolic_bp"] == 120
    assert "buzzer_alert" in reading
    assert "finger_present" in reading
