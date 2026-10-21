"""Database models for the ICU monitoring prototype."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def generate_device_token() -> str:
    return secrets.token_urlsafe(32)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # admin|doctor|patient
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # doctors require admin approval; patients are active on registration
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lockout_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    patient_profile: Mapped["PatientProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    doctor_profile: Mapped["DoctorProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class PatientProfile(Base, TimestampMixin):
    __tablename__ = "patient_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    date_of_birth: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sex: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ward: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bed_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    medical_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="patient_profile")


class DoctorProfile(Base, TimestampMixin):
    __tablename__ = "doctor_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    specialty: Mapped[str | None] = mapped_column(String(128), nullable=True)
    license_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="doctor_profile")


class DoctorPatientAssignment(Base, TimestampMixin):
    __tablename__ = "doctor_patient_assignments"
    __table_args__ = (
        UniqueConstraint("doctor_user_id", "patient_user_id", name="uq_doctor_patient"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doctor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    patient_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Device(Base, TimestampMixin):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_uid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="ESP32 Monitor")
    device_token: Mapped[str] = mapped_column(String(128), default=generate_device_token, nullable=False, unique=True)
    firmware_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ESP8266 direct-polling: when the browser asks THIS server to fetch the
    # device's /data endpoint, that endpoint lives here (host[:port], no scheme).
    data_endpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    direct_fetch_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Ingestion diagnostics shown on the Devices page (Section 6 of the spec)
    last_ingest_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ingest_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # stored|stored_unassigned|rejected|error
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    patient_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class SensorReading(Base):
    __tablename__ = "sensor_readings"
    __table_args__ = (
        Index("ix_readings_patient_ts", "patient_user_id", "received_at"),
        Index("ix_readings_device_ts", "device_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    patient_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    device_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    heart_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    spo2: Mapped[float | None] = mapped_column(Float, nullable=True)
    ambient_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    ambient_humidity: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ---- Simulated blood pressure (ESP8266 potentiometer only) ----
    # bp_simulated is the authoritative flag: when True these numbers were
    # produced by a potentiometer, are NOT a measurement, and must never be used
    # as clinical evidence or fed into diagnosis.
    systolic_bp: Mapped[float | None] = mapped_column(Float, nullable=True)
    diastolic_bp: Mapped[float | None] = mapped_column(Float, nullable=True)
    bp_unit: Mapped[str] = mapped_column(String(16), default="mmHg", nullable=False)
    bp_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bp_pot_raw: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-signal validity flags reported by the device (distinct from the
    # aggregate is_valid, which covers the whole reading).
    finger_present: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hr_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    spo2_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    buzzer_alert: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    signal_quality: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)  # good|fair|poor|invalid|no_finger
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    validity_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    firmware_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assessed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    risk_category: Mapped[str] = mapped_column(String(16), nullable=False)  # low|moderate|high|critical
    risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reasons: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON list of reason strings
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON of input readings
    module: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_patient_active", "patient_user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_doctor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)  # general|cardiac|infection|device|stroke_suspect
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # low|moderate|high|critical
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active|acknowledged|resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AlertAcknowledgement(Base):
    __tablename__ = "alert_acknowledgements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # acknowledged|resolved|reopened|noted
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DoctorNote(Base):
    __tablename__ = "doctor_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class StrokeSymptomEntry(Base):
    __tablename__ = "stroke_symptom_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entered_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    facial_asymmetry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    one_sided_weakness: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    speech_difficulty: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sudden_neuro_change: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    free_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(160), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ApplicationSetting(Base):
    __tablename__ = "application_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
