"""Core automated tests for the ICU monitoring prototype."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from backend.tests.conftest import create_device, ingest, register_patient


# ---------------------------------------------------------------------------
# Health & authentication
# ---------------------------------------------------------------------------

def test_health(client: TestClient):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


def test_login_wrong_password(client: TestClient):
    _make_admin()
    r = client.post("/api/v1/auth/login", json={"email": "admin@example.org", "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_auth(client: TestClient):
    assert client.get("/api/v1/auth/me").status_code == 401


def test_patient_registers_and_logs_in(client: TestClient):
    register_patient(client, "alice@example.org", "Alice Wonder")
    r = client.post("/api/v1/auth/login", json={"email": "alice@example.org", "password": "PatientPass1!"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "patient"
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "alice@example.org"


def test_login_throttling(client: TestClient):
    _make_throttle_target()
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"email": "throttle@example.org", "password": "bad-pass-1"})
    r = client.post("/api/v1/auth/login", json={"email": "throttle@example.org", "password": "bad-pass-2"})
    assert r.status_code == 429


def _make_throttle_target() -> None:
    from backend.database import SessionLocal
    from backend.models import User
    from backend.security import hash_password

    db = SessionLocal()
    try:
        if not db.query(User).filter(User.email == "throttle@example.org").first():
            db.add(User(email="throttle@example.org", password_hash=hash_password("ValidPass1!x"),
                        full_name="Throttle Target", role="patient", is_active=True, is_approved=True))
            db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Roles & registration privileges
# ---------------------------------------------------------------------------

def test_public_registration_cannot_grant_doctor_privileges(client: TestClient):
    r = client.post("/api/v1/auth/register/doctor", json={
        "email": "sneaky@example.org", "password": "SneakyPass1!", "full_name": "Sneaky Person",
    })
    assert r.status_code == 201
    # Account exists but must NOT have privileges until approved
    login = client.post("/api/v1/auth/login", json={"email": "sneaky@example.org", "password": "SneakyPass1!"})
    assert login.status_code == 403
    assert "approval" in login.json()["detail"].lower()


def test_admin_approves_doctor(admin_client: TestClient, client: TestClient):
    client.post("/api/v1/auth/register/doctor", json={
        "email": "doc@example.org", "password": "DoctorPass1!", "full_name": "Dr Test",
    })
    pending = admin_client.get("/api/v1/admin/pending-doctors")
    assert pending.status_code == 200
    doc_id = [d["id"] for d in pending.json()["doctors"] if d["email"] == "doc@example.org"][0]
    r = admin_client.post(f"/api/v1/admin/doctors/{doc_id}/approve")
    assert r.status_code == 200
    login = client.post("/api/v1/auth/login", json={"email": "doc@example.org", "password": "DoctorPass1!"})
    assert login.status_code == 200


def _make_admin() -> None:
    from backend.database import SessionLocal
    from backend.models import User
    from backend.security import hash_password

    db = SessionLocal()
    try:
        if not db.query(User).filter(User.email == "admin@example.org").first():
            db.add(User(email="admin@example.org", password_hash=hash_password("AdminPassw0rd!"),
                        full_name="Test Admin", role="admin", is_active=True, is_approved=True))
            db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Device authentication & ingestion
# ---------------------------------------------------------------------------

def test_ingest_requires_device_token(client: TestClient):
    r = client.post("/api/v1/ingest", json={"heart_rate": 75, "spo2": 98, "signal_quality": "good"})
    assert r.status_code == 401


def test_ingest_rejects_fake_token(client: TestClient):
    r = client.post("/api/v1/ingest", json={"heart_rate": 75},
                    headers={"X-Device-Token": "not-a-real-token-123"})
    assert r.status_code == 401


def _setup_device_and_patient(admin_client: TestClient, client: TestClient) -> dict:
    """Admin registers device, patient registers, admin assigns device+doctor."""
    register_patient(client, "carl@example.org", "Carl Patient")
    device = create_device(admin_client, "ESP32-TEST-001")
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("carl@example.org")})
    return device


def _uid(email: str) -> int:
    from backend.database import SessionLocal
    from backend.models import User

    db = SessionLocal()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


def test_ingest_stores_reading_and_updates_patient(admin_client: TestClient, client: TestClient):
    device = _setup_device_and_patient(admin_client, client)
    r = ingest(admin_client, device["device_token"], {
        "heart_rate": 82, "spo2": 97, "ambient_temperature": 24.5,
        "ambient_humidity": 46, "signal_quality": "good", "finger_detected": True,
        "firmware_version": "1.0.0-test",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "stored"
    assert body["reading_id"]

    login = client.post("/api/v1/auth/login", json={"email": "carl@example.org", "password": "PatientPass1!"})
    assert login.status_code == 200
    readings = client.get(f"/api/v1/patients/{_uid('carl@example.org')}/readings")
    assert readings.status_code == 200
    rows = readings.json()["readings"]
    assert len(rows) == 1
    assert rows[0]["heart_rate"] == 82
    assert rows[0]["is_simulated"] is False


def test_device_cannot_choose_patient_ignores_payload_field(admin_client: TestClient, client: TestClient):
    """A device payload patient id must be ignored; server resolves assignment."""
    register_patient(client, "victim@example.org", "Victim Patient")
    device = create_device(admin_client, "ESP32-EVIL-001")
    # device assigned to carl, payload tries to write to victim
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("carl@example.org")})
    before = admin_client.get(f"/api/v1/patients/{_uid('carl@example.org')}/readings").json()["count"]
    r = ingest(admin_client, device["device_token"], {
        "heart_rate": 300, "spo2": 99, "patient_user_id": _uid("victim@example.org"),
        "signal_quality": "good",
    })
    assert r.status_code == 200
    # stored into the ASSIGNED patient (carl), with HR rejected as out of range
    victim_readings = admin_client.get(f"/api/v1/patients/{_uid('victim@example.org')}/readings").json()
    carl_readings = admin_client.get(f"/api/v1/patients/{_uid('carl@example.org')}/readings").json()
    assert victim_readings["count"] == 0
    assert carl_readings["count"] == before + 1
    assert carl_readings["readings"][0]["heart_rate"] is None  # out of range → nulled


def test_unassigned_device_reading_discarded(admin_client: TestClient):
    device = create_device(admin_client, "ESP32-UNASSIGNED-1")
    r = ingest(admin_client, device["device_token"], {"heart_rate": 70, "signal_quality": "good"})
    assert r.status_code == 200
    assert r.json()["status"] == "stored_unassigned"


def test_invalid_signal_stored_but_not_valid(admin_client: TestClient, client: TestClient):
    device = create_device(admin_client, "ESP32-POOR-001")
    register_patient(client, "dave@example.org", "Dave Poor")
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("dave@example.org")})
    r = ingest(admin_client, device["device_token"], {
        "heart_rate": 0, "spo2": 0, "signal_quality": "poor", "finger_detected": True,
    })
    assert r.status_code == 200
    rows = admin_client.get(f"/api/v1/patients/{_uid('dave@example.org')}/readings").json()["readings"]
    assert rows[0]["is_valid"] is False


# ---------------------------------------------------------------------------
# Data isolation
# ---------------------------------------------------------------------------

def test_patient_cannot_see_other_patients(client: TestClient, admin_client: TestClient):
    register_patient(client, "p1@example.org", "Patient One")
    register_patient(client, "p2@example.org", "Patient Two")
    p1_id, p2_id = _uid("p1@example.org"), _uid("p2@example.org")

    client.post("/api/v1/auth/login", json={"email": "p1@example.org", "password": "PatientPass1!"})
    # can see own
    assert client.get(f"/api/v1/patients/{p1_id}").status_code == 200
    # cannot see other
    assert client.get(f"/api/v1/patients/{p2_id}").status_code == 403
    assert client.get(f"/api/v1/patients/{p2_id}/readings").status_code == 403
    assert client.get(f"/api/v1/patients/{p2_id}/export").status_code == 403
    # patient list contains only self
    listing = client.get("/api/v1/patients").json()
    assert [p["user_id"] for p in listing] == [p1_id]


def test_doctor_sees_only_assigned_patients(admin_client: TestClient, client: TestClient):
    admin_client.post("/api/v1/admin/users/create", json={
        "email": "doc2@example.org", "password": "DoctorPass1!", "full_name": "Dr Two", "role": "doctor",
    })
    admin_client.post("/api/v1/admin/doctors/{id}/approve".replace("{id}", str(_uid("doc2@example.org"))))
    register_patient(client, "pa@example.org", "Patient A")
    register_patient(client, "pb@example.org", "Patient B")

    # assign only pa to the doctor
    admin_client.post("/api/v1/doctors/assignments", json={
        "doctor_user_id": _uid("doc2@example.org"), "patient_user_id": _uid("pa@example.org"),
    })

    client.post("/api/v1/auth/login", json={"email": "doc2@example.org", "password": "DoctorPass1!"})
    listing = client.get("/api/v1/patients").json()
    assert [p["user_id"] for p in listing] == [_uid("pa@example.org")]
    assert client.get(f"/api/v1/patients/{_uid('pb@example.org')}").status_code == 403
    # after unassignment the doctor loses access
    admin_client.post("/api/v1/doctors/assignments", json={
        "doctor_user_id": _uid("doc2@example.org"), "patient_user_id": _uid("pa@example.org"),
    })  # re-assign harmless
    a = [x for x in admin_client.get("/api/v1/doctors/assignments").json()["assignments"]
         if x["patient_user_id"] == _uid("pa@example.org") and x["doctor_user_id"] == _uid("doc2@example.org")]
    assert admin_client.delete(f"/api/v1/doctors/assignments/{a[0]['id']}").status_code == 200
    assert client.get(f"/api/v1/patients/{_uid('pa@example.org')}").status_code == 403


def test_patient_cannot_access_admin_endpoints(client: TestClient):
    register_patient(client, "nosy@example.org", "Nosy Patient")
    client.post("/api/v1/auth/login", json={"email": "nosy@example.org", "password": "PatientPass1!"})
    assert client.get("/api/v1/admin/audit-logs").status_code == 403
    assert client.get("/api/v1/admin/settings").status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.get("/api/v1/devices").status_code == 403


# ---------------------------------------------------------------------------
# Risk engine, alerts, dedup, ack/resolve
# ---------------------------------------------------------------------------

def _ingest_sequence(client: TestClient, device_token: str, readings: list[dict]) -> None:
    for rd in readings:
        r = ingest(client, device_token, rd)
        assert r.status_code == 200, r.text


def test_risk_engine_flags_sustained_high_hr(admin_client: TestClient, client: TestClient):
    register_patient(client, "tachy@example.org", "Tachy Patient")
    device = create_device(admin_client, "ESP32-TACHY-001")
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("tachy@example.org")})
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 130 + i, "spo2": 97, "signal_quality": "good", "finger_detected": True}
        for i in range(4)
    ])
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": _uid("tachy@example.org")}).json()["alerts"]
    cardiac = [a for a in alerts if a["category"] in ("cardiac", "general") and a["is_simulated"] is False]
    assert cardiac, "expected an alert for sustained high heart rate"
    assert cardiac[0]["severity"] in ("moderate", "high", "critical")


def test_alert_deduplication(admin_client: TestClient, client: TestClient):
    register_patient(client, "dedup@example.org", "Dedup Patient")
    device = create_device(admin_client, "ESP32-DEDUP-001")
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("dedup@example.org")})
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 140, "spo2": 96, "signal_quality": "good", "finger_detected": True}
        for _ in range(6)
    ])
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": _uid("dedup@example.org")}).json()["alerts"]
    vitals_alerts = [a for a in alerts if a["category"] in ("cardiac", "general")]
    # dedupe_key must collapse repeated identical conditions into one active alert
    assert len(vitals_alerts) == 1, f"expected 1 deduped alert, got {len(vitals_alerts)}"


def test_alert_acknowledge_and_resolve(admin_client: TestClient, client: TestClient):
    register_patient(client, "ack@example.org", "Ack Patient")
    device = create_device(admin_client, "ESP32-ACK-001")
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("ack@example.org")})
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 35, "spo2": 85, "signal_quality": "good", "finger_detected": True}
        for _ in range(4)
    ])
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": _uid("ack@example.org")}).json()["alerts"]
    assert alerts, "expected critical alert"
    target = alerts[0]
    r = admin_client.post(f"/api/v1/alerts/{target['id']}/acknowledge", json={"note": "reviewed"})
    assert r.status_code == 200
    assert r.json()["status"] == "acknowledged"
    r = admin_client.post(f"/api/v1/alerts/{target['id']}/resolve", json={"note": "patient stable"})
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"
    history = admin_client.get(f"/api/v1/alerts/{target['id']}/history").json()["history"]
    assert [h["action"] for h in history] == ["acknowledged", "resolved"]


def test_stroke_symptoms_raise_critical_alert(admin_client: TestClient, client: TestClient):
    register_patient(client, "stroke@example.org", "Stroke Patient")
    r = admin_client.post("/api/v1/stroke-symptoms", json={
        "patient_user_id": _uid("stroke@example.org"),
        "facial_asymmetry": True, "one_sided_weakness": True,
        "speech_difficulty": False, "sudden_neuro_change": False,
    })
    assert r.status_code == 201
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": _uid("stroke@example.org")}).json()["alerts"]
    stroke_alerts = [a for a in alerts if a["category"] == "stroke_suspect"]
    assert stroke_alerts and stroke_alerts[0]["severity"] == "critical"


def test_stroke_flagged_even_with_zero_sensor_readings(admin_client: TestClient, client: TestClient):
    """Clinician stroke entries are sensor-independent: the assessment must flag
    them even when the device has NEVER sent a reading (early-return paths)."""
    register_patient(client, "stroke_noread@example.org", "No Data Stroke")
    pid = _uid("stroke_noread@example.org")
    r = admin_client.post("/api/v1/stroke-symptoms", json={
        "patient_user_id": pid,
        "facial_asymmetry": True, "one_sided_weakness": False,
        "speech_difficulty": True, "sudden_neuro_change": False,
    })
    assert r.status_code == 201
    r = admin_client.post(f"/api/v1/patients/{pid}/reassess")
    assert r.status_code == 200
    hist = admin_client.get(f"/api/v1/patients/{pid}/risk-history", params={"limit": 1}).json()
    latest = hist["assessments"][0]
    mods = latest["evidence"]["modules"]
    assert mods["stroke_suspect"]["status"] == "flagged"
    assert mods["stroke_suspect"]["severity"] == "critical"
    assert latest["risk_category"] == "critical"
    assert latest["evidence"]["dominant_module"] == "stroke_suspect"
    assert mods["technical"]["status"] != "ok"


# ---------------------------------------------------------------------------
# Stale / missing data
# ---------------------------------------------------------------------------

def test_stale_data_flagged(admin_client: TestClient, client: TestClient):
    register_patient(client, "stale@example.org", "Stale Patient")
    device = create_device(admin_client, "ESP32-STALE-001")
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("stale@example.org")})
    ingest(admin_client, device["device_token"],
           {"heart_rate": 70, "spo2": 98, "signal_quality": "good", "finger_detected": True})
    live = admin_client.get(f"/api/v1/live/{_uid('stale@example.org')}").json()
    assert live["stale"] is False
    # Backdate the reading beyond the staleness threshold
    from backend.database import SessionLocal
    from backend.models import SensorReading, utcnow

    db = SessionLocal()
    try:
        row = db.query(SensorReading).filter(SensorReading.patient_user_id == _uid("stale@example.org")).first()
        row.received_at = utcnow() - timedelta(seconds=600)
        db.commit()
    finally:
        db.close()
    live = admin_client.get(f"/api/v1/live/{_uid('stale@example.org')}").json()
    assert live["stale"] is True
    # Risk must reflect staleness once re-assessed (dashboards call reassess)
    reassess = admin_client.post(f"/api/v1/patients/{_uid('stale@example.org')}/reassess").json()
    assert reassess["risk_category"] in ("moderate", "high", "critical")
    assert any("no sensor data" in r.lower() for r in reassess["reasons"])


def test_device_offline_detection(admin_client: TestClient, client: TestClient):
    device = create_device(admin_client, "ESP32-OFFLINE-1")
    detail = admin_client.get(f"/api/v1/devices/{device['id']}").json()
    assert detail["is_online"] is False
    ingest(admin_client, device["device_token"], {"heart_rate": 72, "signal_quality": "good"})
    detail = admin_client.get(f"/api/v1/devices/{device['id']}").json()
    assert detail["is_online"] is True
    test = admin_client.post(f"/api/v1/devices/{device['id']}/test").json()
    assert test["online"] is True


# ---------------------------------------------------------------------------
# Test scenarios don't contaminate real data
# ---------------------------------------------------------------------------

def test_scenarios_marked_simulated(admin_client: TestClient, client: TestClient):
    register_patient(client, "sim@example.org", "Sim Patient")
    device = create_device(admin_client, "ESP32-SIM-001")
    admin_client.post(f"/api/v1/devices/{device['id']}/assign",
                      json={"patient_user_id": _uid("sim@example.org")})
    r = admin_client.post("/api/v1/test/scenarios",
                          json={"scenario": "low_spo2", "device_uid": "ESP32-SIM-001"})
    assert r.status_code == 200, r.text
    assert r.json()["simulated"] is True

    rows = admin_client.get(
        f"/api/v1/patients/{_uid('sim@example.org')}/readings",
        params={"include_simulated": True},
    ).json()["readings"]
    assert rows, "simulated readings should be stored"
    assert all(x["is_simulated"] for x in rows)

    # default export/reading views exclude simulated data
    default_rows = admin_client.get(f"/api/v1/patients/{_uid('sim@example.org')}/readings").json()["readings"]
    assert all(x["is_simulated"] is False for x in default_rows)
    assert default_rows == []

    # clearing removes only simulated data
    cleared = admin_client.post(f"/api/v1/test/clear-simulated/{_uid('sim@example.org')}").json()
    assert cleared["simulated_readings_deleted"] == len(rows)
    remaining = admin_client.get(
        f"/api/v1/patients/{_uid('sim@example.org')}/readings",
        params={"include_simulated": True},
    ).json()["count"]
    assert remaining == 0


# ---------------------------------------------------------------------------
# CSV export authorization
# ---------------------------------------------------------------------------

def test_csv_export_requires_authorization(client: TestClient, admin_client: TestClient):
    register_patient(client, "exp1@example.org", "Export One")
    register_patient(client, "exp2@example.org", "Export Two")
    client.post("/api/v1/auth/login", json={"email": "exp1@example.org", "password": "PatientPass1!"})
    r = client.get(f"/api/v1/patients/{_uid('exp2@example.org')}/export")
    assert r.status_code == 403
    r = client.get(f"/api/v1/patients/{_uid('exp1@example.org')}/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "patient_id" not in r.text.splitlines()[0]  # header row exists
    assert r.text.splitlines()[0].startswith("reading_id")


def test_export_blocks_unauthenticated(client: TestClient):
    assert client.get("/api/v1/analytics/export?patient_id=1").status_code == 401


# ---------------------------------------------------------------------------
# Session expiry / logout
# ---------------------------------------------------------------------------

def test_logout_invalidates_session(client: TestClient):
    register_patient(client, "logout@example.org", "Logout Person")
    client.post("/api/v1/auth/login", json={"email": "logout@example.org", "password": "PatientPass1!"})
    assert client.get("/api/v1/auth/me").status_code == 200
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401

