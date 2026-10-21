"""Tests for the alarm-management and disease-module upgrades (master spec):

* per-module risk reports (sepsis / cardiac / stroke / general / technical)
* alert lifecycle active -> acknowledged -> under_review -> resolved -> reopened
* post-resolution cooldown suppression
* background device-health monitor (offline/stale technical alarms)
* device ingestion diagnostics for the Devices page
* stroke symptom test scenario
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.tests.conftest import create_device, ingest, register_patient


def _uid(email: str) -> int:
    from backend.database import SessionLocal
    from backend.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        assert user, f"user {email} not found"
        return user.id
    finally:
        db.close()


def _latest_assessment(patient_id: int) -> dict:
    from backend.database import SessionLocal
    from backend.models import RiskAssessment

    db = SessionLocal()
    try:
        row = (
            db.query(RiskAssessment)
            .filter(RiskAssessment.patient_user_id == patient_id)
            .order_by(RiskAssessment.assessed_at.desc())
            .first()
        )
        assert row, "no risk assessment stored"
        import json

        return {
            "category": row.risk_category,
            "module": row.module,
            "evidence": json.loads(row.evidence),
        }
    finally:
        db.close()


def _setup_patient_with_device(admin_client: TestClient, client: TestClient, email: str, uid: str) -> tuple[int, dict]:
    register_patient(client, email)
    device = create_device(admin_client, uid)
    admin_client.post(f"/api/v1/devices/{device['id']}/assign", json={"patient_user_id": _uid(email)})
    return _uid(email), device


def _ingest_sequence(client: TestClient, token: str, readings: list[dict]) -> None:
    for r in readings:
        resp = ingest(client, token, r)
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Module reports
# ---------------------------------------------------------------------------
def test_normal_readings_produce_module_reports(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "modules_normal@example.org", "ESP32-MOD-NORM")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 76, "spo2": 98, "signal_quality": "good", "finger_detected": True}
        for _ in range(3)
    ])
    a = _latest_assessment(pid)
    modules = a["evidence"]["modules"]
    assert set(modules) == {"possible_sepsis", "possible_cardiac", "stroke_suspect",
                            "general_abnormalities", "technical", "simulated_blood_pressure"}
    assert modules["possible_sepsis"]["status"] == "insufficient_parameters"
    assert "Insufficient clinical parameters" in modules["possible_sepsis"]["message"]
    assert modules["possible_cardiac"]["status"] == "not_flagged"
    assert modules["technical"]["status"] == "ok"
    assert modules["stroke_suspect"]["status"] == "not_flagged"
    # Missing-parameter honesty
    assert any("body temperature" in m for m in modules["possible_sepsis"]["missing_parameters"])
    assert any("blood pressure" in m for m in modules["possible_sepsis"]["missing_parameters"])


def test_sustained_tachycardia_flags_cardiac_module(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "modules_cardiac@example.org", "ESP32-MOD-CARD")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": h, "spo2": 97, "signal_quality": "good", "finger_detected": True}
        for h in (125, 128, 130, 132)
    ])
    a = _latest_assessment(pid)
    modules = a["evidence"]["modules"]
    assert modules["possible_cardiac"]["status"] == "possible_risk_flag"
    assert "ECG" in modules["possible_cardiac"]["message"]  # honest limitation
    assert any("ECG" in m for m in modules["possible_cardiac"]["missing_parameters"])
    assert a["module"] in ("possible_cardiac", "possible_sepsis", "general_abnormalities")


def test_tachycardia_plus_low_spo2_flags_sepsis_module(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "modules_sepsis@example.org", "ESP32-MOD-SEP")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 126, "spo2": 93, "signal_quality": "good", "finger_detected": True},
        {"heart_rate": 130, "spo2": 92, "signal_quality": "good", "finger_detected": True},
        {"heart_rate": 133, "spo2": 91, "signal_quality": "good", "finger_detected": True},
        {"heart_rate": 135, "spo2": 90, "signal_quality": "good", "finger_detected": True},
    ])
    a = _latest_assessment(pid)
    sepsis = a["evidence"]["modules"]["possible_sepsis"]
    assert sepsis["status"] == "possible_risk_flag"
    assert "NOT a sepsis diagnosis" in sepsis["message"]
    assert any("body temperature" in m for m in sepsis["missing_parameters"])
    # An infection-category alert must exist for the flag
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    assert [x for x in alerts if x["category"] == "infection"], "expected infection-category alert"


def test_unusable_signal_marks_technical_module(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "modules_tech@example.org", "ESP32-MOD-TECH")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": None, "spo2": None, "signal_quality": "poor", "finger_detected": True}
        for _ in range(3)
    ])
    a = _latest_assessment(pid)
    technical = a["evidence"]["modules"]["technical"]
    assert technical["status"] == "invalid_signal"
    assert "TECHNICAL ALARM" in technical["message"]
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    device_alerts = [x for x in alerts if x["category"] == "device"]
    assert device_alerts, "expected a technical (device) alert"
    # Technical fault must never be labelled as a disease category
    assert all(x["category"] not in ("cardiac", "infection", "stroke_suspect") for x in device_alerts)


# ---------------------------------------------------------------------------
# Alert lifecycle: active -> acknowledged -> under_review -> resolved -> reopened
# ---------------------------------------------------------------------------
def test_alert_full_lifecycle_with_under_review_and_reopen(
    admin_client: TestClient, client: TestClient
):
    pid, device = _setup_patient_with_device(
        admin_client, client, "lifecycle@example.org", "ESP32-LIFE-001")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 145, "spo2": 96, "signal_quality": "good", "finger_detected": True}
        for _ in range(3)
    ])
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    assert alerts, "expected an alert for the abnormal sequence"
    aid = alerts[0]["id"]

    r = admin_client.post(f"/api/v1/alerts/{aid}/acknowledge", json={"note": "seen"})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"

    r = admin_client.post(f"/api/v1/alerts/{aid}/under-review", json={"note": "reviewing vitals"})
    assert r.status_code == 200 and r.json()["status"] == "under_review"

    # Under-review alerts must still appear in the active list
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid, "status": "under_review"}).json()["alerts"]
    assert [x for x in alerts if x["id"] == aid], "under_review alert must be listable"

    r = admin_client.post(f"/api/v1/alerts/{aid}/resolve", json={"note": "stable now"})
    assert r.status_code == 200 and r.json()["status"] == "resolved"

    # Double resolve is refused, not silently repeated
    r = admin_client.post(f"/api/v1/alerts/{aid}/resolve", json={"note": "again"})
    assert r.status_code == 409

    # Explicit reopen returns it to active
    r = admin_client.post(f"/api/v1/alerts/{aid}/reopen", json={"note": "condition recurred"})
    assert r.status_code == 200 and r.json()["status"] == "active"

    history = admin_client.get(f"/api/v1/alerts/{aid}/history").json()["history"]
    actions = [h["action"] for h in history]
    for expected in ("acknowledged", "under_review", "resolved", "reopened"):
        assert expected in actions, f"missing history action {expected}: {actions}"


def test_resolve_of_active_ack_alert_allowed(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "resolve_direct@example.org", "ESP32-RES-001")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 42, "spo2": 97, "signal_quality": "good", "finger_detected": True}
        for _ in range(3)
    ])
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    assert alerts
    r = admin_client.post(f"/api/v1/alerts/{alerts[0]['id']}/resolve", json={"note": "clinical review complete"})
    assert r.status_code == 200


def test_reopen_refused_for_active_alert(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "reopen_guard@example.org", "ESP32-REO-001")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 145, "spo2": 96, "signal_quality": "good", "finger_detected": True}
        for _ in range(3)
    ])
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    r = admin_client.post(f"/api/v1/alerts/{alerts[0]['id']}/reopen", json={"note": "nope"})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Post-resolution cooldown
# ---------------------------------------------------------------------------
def test_cooldown_suppresses_identical_resolved_event(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "cooldown@example.org", "ESP32-CD-001")
    readings = [
        {"heart_rate": 130, "spo2": 97, "signal_quality": "good", "finger_detected": True}
        for _ in range(4)
    ]
    _ingest_sequence(client, device["device_token"], readings)
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    vitals = [a for a in alerts if a["category"] in ("cardiac", "general")]
    assert len(vitals) == 1
    admin_client.post(f"/api/v1/alerts/{vitals[0]['id']}/resolve", json={"note": "fixed"})

    # Identical condition again WITHIN the cooldown window -> no duplicate alert
    _ingest_sequence(client, device["device_token"], readings)
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    active_vitals = [a for a in alerts if a["category"] in ("cardiac", "general")
                     and a["status"] in ("active", "acknowledged", "under_review")]
    assert not active_vitals, "cooldown should suppress the identical resolved event"


# ---------------------------------------------------------------------------
# Background device-health monitor
# ---------------------------------------------------------------------------
def test_device_health_monitor_raises_offline_alarm(admin_client: TestClient, client: TestClient):
    from backend.main import run_device_health_cycle

    pid, device = _setup_patient_with_device(
        admin_client, client, "health_offline@example.org", "ESP32-HEALTH-01")
    # Device was assigned but has NEVER checked in -> offline technical alarm
    run_device_health_cycle()
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    offline = [a for a in alerts if a["category"] == "device" and "offline" in a["title"].lower()]
    assert offline, f"expected device-offline technical alarm, got: {[a['title'] for a in alerts]}"

    # Running the cycle again must NOT duplicate it (dedup)
    run_device_health_cycle()
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    offline = [a for a in alerts if a["category"] == "device" and "offline" in a["title"].lower()]
    assert len(offline) == 1, "device-offline alarm must be deduplicated"


def test_device_health_monitor_raises_stale_alarm(admin_client: TestClient, client: TestClient):
    from datetime import timedelta

    from backend.database import SessionLocal
    from backend.main import run_device_health_cycle
    from backend.models import Device, SensorReading, utcnow

    pid, device = _setup_patient_with_device(
        admin_client, client, "health_stale@example.org", "ESP32-HEALTH-02")
    _ingest_sequence(client, device["device_token"], [
        {"heart_rate": 80, "spo2": 98, "signal_quality": "good", "finger_detected": True}
        for _ in range(2)
    ])
    # Make the device "online" and age the reading beyond the stale threshold
    db = SessionLocal()
    try:
        dev = db.query(Device).filter(Device.device_uid == "ESP32-HEALTH-02").first()
        dev.last_seen_at = utcnow()
        db.add(dev)
        db.query(SensorReading).filter(
            SensorReading.patient_user_id == pid
        ).update({"received_at": utcnow() - timedelta(seconds=120)}, synchronize_session=False)
        db.commit()
    finally:
        db.close()

    run_device_health_cycle()
    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    stale = [a for a in alerts if a["category"] == "device" and "stale" in a["title"].lower()]
    assert stale, f"expected stale-data technical alarm, got: {[a['title'] for a in alerts]}"


# ---------------------------------------------------------------------------
# Device ingestion diagnostics (Devices page fields)
# ---------------------------------------------------------------------------
def test_device_ingestion_diagnostics(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "diag@example.org", "ESP32-DIAG-001")
    ingest(client, device["device_token"],
           {"heart_rate": 80, "spo2": 98, "signal_quality": "good", "finger_detected": True})

    devices = admin_client.get("/api/v1/devices").json()
    d = [x for x in devices if x["device_uid"] == "ESP32-DIAG-001"][0]
    assert d["last_ingest_status"] == "stored"
    assert d["last_ingest_at"] is not None
    assert d["last_error"] in (None, "")
    assert d["patient_name"] and d["patient_user_id"] == pid
    assert d["last_reading_at"] is not None
    assert d["data_freshness"] == "fresh"
    assert d["auth_status"] == "active"
    assert d["is_online"] is True


def test_unassigned_ingest_records_status(admin_client: TestClient, client: TestClient):
    device = create_device(admin_client, "ESP32-DIAG-UNASSIGNED")
    r = ingest(client, device["device_token"],
               {"heart_rate": 80, "spo2": 98, "signal_quality": "good", "finger_detected": True})
    assert r.status_code == 200
    assert r.json()["status"] == "stored_unassigned"

    devices = admin_client.get("/api/v1/devices").json()
    d = [x for x in devices if x["device_uid"] == "ESP32-DIAG-UNASSIGNED"][0]
    assert d["last_ingest_status"] == "stored_unassigned"
    assert d["last_error"] and "not assigned" in d["last_error"]
    assert d["data_freshness"] == "no_data"


# ---------------------------------------------------------------------------
# Stroke symptom test scenario (required scenario #10)
# ---------------------------------------------------------------------------
def test_stroke_symptom_scenario(admin_client: TestClient, client: TestClient):
    pid, device = _setup_patient_with_device(
        admin_client, client, "stroke_scenario@example.org", "ESP32-STR-001")
    r = admin_client.post("/api/v1/test/scenarios",
                          json={"scenario": "stroke_symptoms", "device_uid": "ESP32-STR-001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["simulated"] is True
    assert body["alert_id"]

    alerts = admin_client.get("/api/v1/alerts", params={"patient_id": pid}).json()["alerts"]
    stroke = [a for a in alerts if a["category"] == "stroke_suspect"]
    assert stroke and stroke[0]["severity"] == "critical"
    assert stroke[0]["is_simulated"] is True

    # Simulated entry is separable from real data
    r = admin_client.post(f"/api/v1/test/clear-simulated/{pid}")
    assert r.status_code == 200
    assert r.json()["simulated_stroke_entries_deleted"] >= 1
    assert r.json()["simulated_readings_deleted"] == 0  # no readings were created


def test_scenarios_list_includes_required_entries(admin_client: TestClient):
    ids = [s["id"] for s in admin_client.get("/api/v1/test/scenarios").json()["scenarios"]]
    for required in ("normal", "sustained_high_hr", "low_spo2", "invalid_signal",
                     "sensor_disconnected", "device_network_failure",
                     "patient_reassignment", "stroke_symptoms"):
        assert required in ids, f"missing required scenario: {required}"
