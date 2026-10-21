"""Test fixtures: throwaway database + FastAPI test client helpers."""
from __future__ import annotations

import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="icu_test_")
os.environ.setdefault("ICU_DATABASE_URL", f"sqlite:///{_TMPDIR}/test_icu.db")
os.environ.setdefault("ICU_SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("ICU_STALE_SECONDS", "60")
os.environ.setdefault("ICU_LOGIN_MAX_ATTEMPTS", "5")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.database import Base, engine, init_db  # noqa: E402
from backend.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_client():
    """A SEPARATE logged-in admin client (own cookie jar, independent of `client`)."""
    _ensure_admin()
    with TestClient(app) as c:
        _login(c, "admin@example.org", "AdminPassw0rd!")
        yield c


def _ensure_admin() -> None:
    from backend.database import SessionLocal
    from backend.models import User
    from backend.security import hash_password

    db = SessionLocal()
    try:
        if not db.query(User).filter(User.email == "admin@example.org").first():
            db.add(User(
                email="admin@example.org",
                password_hash=hash_password("AdminPassw0rd!"),
                full_name="Test Admin",
                role="admin",
                is_active=True,
                is_approved=True,
            ))
            db.commit()
    finally:
        db.close()


def _login(client: TestClient, email: str, password: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json()


def register_patient(client: TestClient, email: str, name: str = "Test Patient") -> dict:
    r = client.post("/api/v1/auth/register/patient", json={
        "email": email, "password": "PatientPass1!", "full_name": name,
    })
    assert r.status_code == 201, f"patient registration failed: {r.status_code} {r.text}"
    return r.json()


def create_device(client: TestClient, device_uid: str) -> dict:
    r = client.post("/api/v1/devices/register", json={"device_uid": device_uid, "name": "Test ESP32"})
    assert r.status_code == 201, f"device registration failed: {r.status_code} {r.text}"
    return r.json()


def ingest(client: TestClient, token: str, payload: dict) -> dict:
    return client.post("/api/v1/ingest", json=payload, headers={"X-Device-Token": token})

