"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

Role = Literal["admin", "doctor", "patient"]
Severity = Literal["low", "moderate", "high", "critical"]
RiskCategory = Literal["low", "moderate", "high", "critical"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class PatientRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)
    date_of_birth: datetime | None = None
    sex: str | None = Field(default=None, max_length=16)
    ward: str | None = Field(default=None, max_length=64)
    bed_label: str | None = Field(default=None, max_length=64)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if v.isdigit() or v.isalpha() and v.islower() and len(set(v)) < 4:
            raise ValueError("Password must not be trivially simple")
        return v


class DoctorRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=255)
    specialty: str | None = Field(default=None, max_length=128)
    license_number: str | None = Field(default=None, max_length=64)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    role: Role
    is_active: bool
    is_approved: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class PatientProfileOut(BaseModel):
    id: int
    user_id: int
    date_of_birth: datetime | None
    sex: str | None
    ward: str | None
    bed_label: str | None
    medical_notes: str | None

    model_config = {"from_attributes": True}


class DoctorProfileOut(BaseModel):
    id: int
    user_id: int
    specialty: str | None
    license_number: str | None

    model_config = {"from_attributes": True}


class MeResponse(BaseModel):
    user: UserOut
    patient_profile: PatientProfileOut | None = None
    doctor_profile: DoctorProfileOut | None = None


class DeviceRegistrationRequest(BaseModel):
    device_uid: str = Field(min_length=4, max_length=128)
    name: str = Field(default="ESP32 Monitor", max_length=128)
    notes: str | None = None


class DeviceOut(BaseModel):
    id: int
    device_uid: str
    name: str
    firmware_version: str | None
    ip_address: str | None
    data_endpoint: str | None = None
    direct_fetch_enabled: bool = False
    last_seen_at: datetime | None
    is_active: bool
    patient_user_id: int | None
    notes: str | None
    is_online: bool = False
    created_at: datetime
    # Ingestion diagnostics (Section 6 of the spec)
    last_ingest_at: datetime | None = None
    last_ingest_status: str | None = None
    last_error: str | None = None
    # Enriched display fields computed server-side in devices._device_out()
    patient_name: str | None = None
    last_reading_at: datetime | None = None
    data_age_seconds: float | None = None
    data_freshness: str = "no_data"          # fresh | stale | no_data
    auth_status: str = "active"              # active | deactivated

    model_config = {"from_attributes": True}


class DeviceAssignRequest(BaseModel):
    patient_user_id: int


class DeviceTokenResponse(DeviceOut):
    device_token: str


class IngestReading(BaseModel):
    """Payload sent by the ESP32.

    patient_user_id is intentionally NOT accepted from devices — the server
    resolves the patient from its own device-to-patient assignment.
    """

    heart_rate: float | None = Field(default=None, ge=-50, le=400)
    spo2: float | None = Field(default=None, ge=-50, le=150)
    ambient_temperature: float | None = Field(default=None, ge=-50, le=100)
    ambient_humidity: float | None = Field(default=None, ge=-50, le=150)
    signal_quality: str = Field(default="unknown", max_length=16)
    finger_detected: bool = True
    device_timestamp: str | None = None
    firmware_version: str | None = None
    ip_address: str | None = None
    is_simulated: bool = False

    # ---- Simulated blood pressure (ESP8266 potentiometer) ----
    # If bp_simulated is true these values came from a potentiometer, not a
    # blood-pressure sensor, and are stored for display/trend only — never as
    # clinical evidence.
    systolic_bp: float | None = Field(default=None, ge=-50, le=400)
    diastolic_bp: float | None = Field(default=None, ge=-50, le=400)
    bp_unit: str = Field(default="mmHg", max_length=16)
    bp_simulated: bool = False
    bp_pot_raw: int | None = Field(default=None, ge=0, le=1000000)

    # Per-signal validity flags the ESP firmware reports directly.
    hr_valid: bool | None = None
    spo2_valid: bool | None = None
    buzzer_alert: bool | None = None

    @field_validator("signal_quality")
    @classmethod
    def normalize_quality(cls, v: str) -> str:
        allowed = {"good", "fair", "poor", "invalid", "no_finger", "unknown"}
        v = (v or "unknown").lower().strip()
        return v if v in allowed else "unknown"


class ReadingOut(BaseModel):
    id: int
    device_id: int
    patient_user_id: int | None
    received_at: datetime
    device_timestamp: datetime | None
    heart_rate: float | None
    spo2: float | None
    ambient_temperature: float | None
    ambient_humidity: float | None
    systolic_bp: float | None = None
    diastolic_bp: float | None = None
    bp_unit: str = "mmHg"
    bp_simulated: bool = False
    bp_pot_raw: int | None = None
    finger_present: bool = True
    hr_valid: bool = False
    spo2_valid: bool = False
    buzzer_alert: bool = False
    signal_quality: str
    is_valid: bool
    validity_notes: str | None
    firmware_version: str | None
    is_simulated: bool

    model_config = {"from_attributes": True}


class IngestResponse(BaseModel):
    status: str
    reading_id: int | None = None
    message: str = ""


class AlertOut(BaseModel):
    id: int
    patient_user_id: int
    assigned_doctor_user_id: int | None
    category: str
    severity: str
    title: str
    message: str
    evidence: str
    status: str
    created_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    resolution_notes: str | None
    dedupe_key: str | None
    is_simulated: bool

    model_config = {"from_attributes": True}


class AlertActionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=4000)


class DoctorNoteCreate(BaseModel):
    patient_user_id: int
    note_text: str = Field(min_length=1, max_length=8000)


class DoctorNoteOut(BaseModel):
    id: int
    patient_user_id: int
    doctor_user_id: int
    note_text: str
    created_at: datetime

    model_config = {"from_attributes": True}


class StrokeSymptomCreate(BaseModel):
    patient_user_id: int
    facial_asymmetry: bool = False
    one_sided_weakness: bool = False
    speech_difficulty: bool = False
    sudden_neuro_change: bool = False
    free_text: str | None = Field(default=None, max_length=2000)
    is_simulated: bool = False


class StrokeSymptomOut(BaseModel):
    id: int
    patient_user_id: int
    entered_by: int
    facial_asymmetry: bool
    one_sided_weakness: bool
    speech_difficulty: bool
    sudden_neuro_change: bool
    free_text: str | None
    created_at: datetime
    is_simulated: bool

    model_config = {"from_attributes": True}


class AuditLogOut(BaseModel):
    id: int
    actor_user_id: int | None
    actor_label: str
    action: str
    entity_type: str | None
    entity_id: str | None
    detail: str | None
    ip_address: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PatientListItem(BaseModel):
    user_id: int
    full_name: str
    email: str
    ward: str | None
    bed_label: str | None
    assigned_device_uid: str | None
    device_online: bool
    latest_hr: float | None
    latest_spo2: float | None
    latest_ambient_temp: float | None
    latest_humidity: float | None
    latest_systolic_bp: float | None = None
    latest_diastolic_bp: float | None = None
    bp_simulated: bool = False
    finger_present: bool | None = None
    buzzer_alert: bool = False
    last_data_at: datetime | None
    signal_quality: str | None
    data_stale: bool
    risk_category: str
    # Priority used for sorting/filtering. Adds "insufficient_data" so a
    # patient with no usable readings is never presented as "low" (normal).
    priority: str = "low"
    priority_label: str = "Low"
    active_alert_count: int
    critical_alert_count: int


class AnalyticsSummary(BaseModel):
    total_patients: int
    total_doctors: int
    total_devices: int
    devices_online: int
    active_alerts: int
    critical_alerts: int
    readings_last_hour: int
    readings_total: int
    risk_breakdown: dict[str, int]
    generated_at: datetime


class ScenarioRequest(BaseModel):
    scenario: str
    device_uid: str | None = None
    target_patient_user_id: int | None = None


class HealthResponse(BaseModel):
    status: str
    database: str
    server_time: datetime
    version: str


class SettingsUpdateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=4000)


class DirectFetchConfigRequest(BaseModel):
    """Set the ESP8266 /data endpoint that the browser will poll THROUGH us."""

    device_id: int
    endpoint: str | None = Field(default=None, max_length=128)  # host[:port]; None clears it
    enabled: bool = True


class DirectFetchConfigOut(BaseModel):
    device_id: int
    device_uid: str
    endpoint: str | None
    enabled: bool
    host: str | None = None
    port: int | None = None
    note: str = ""


class DirectFetchProbe(BaseModel):
    ok: bool
    status: str                     # ok | unreachable | timeout | http_error | invalid_json | blocked | disabled
    http_status: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    endpoint: str | None = None
    data: dict[str, Any] | None = None   # raw device JSON when ok


class DeviceDataMapOut(BaseModel):
    """Maps the ESP8266 raw /data keys into the platform's reading fields."""

    raw: dict[str, Any]
    mapped: IngestReading
    mapping_notes: list[str] = []
