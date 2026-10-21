"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="ICU_",
    )

    secret_key: str = "dev-insecure-secret-change-me"
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'icu_monitor.db'}"
    host: str = "0.0.0.0"
    port: int = 8080
    session_minutes: int = 480
    stale_seconds: int = 60
    device_offline_seconds: int = 120
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    debug: bool = False

    # ---- Risk thresholds (configurable, stored/overridable via application_settings) ----
    hr_low: float = 50.0
    hr_high: float = 120.0
    hr_critical_low: float = 40.0
    hr_critical_high: float = 150.0
    spo2_low: float = 94.0
    spo2_critical_low: float = 90.0
    ambient_temp_low: float = 10.0
    ambient_temp_high: float = 40.0
    ambient_humidity_low: float = 20.0
    ambient_humidity_high: float = 80.0
    # Persistence: number of consecutive abnormal readings before escalation
    abnormal_persist_count: int = 3
    # Hysteresis margin to avoid alert flapping (percent / bpm)
    hysteresis_spo2: float = 1.0
    hysteresis_hr: float = 3.0

    # ---- Alarm-management policy (configurable) ----
    # After an alert with the same dedupe key is resolved, identical events are
    # suppressed for this many minutes (worsening severity still escalates).
    alert_cooldown_minutes: int = 10
    # Critical alerts unacknowledged for this long are re-escalated in the UI.
    critical_escalation_minutes: int = 15
    # Background device-health monitor cadence (seconds).
    device_health_interval_seconds: int = 30

    # ---- Direct ESP8266 /data fetch (MedicAssist integration) ----
    # The browser cannot reliably read JSON from an ESP over plain HTTP when the
    # dashboard itself is served over HTTPS (mixed-content) or when the ESP sends
    # no CORS headers, so the browser asks THIS server to fetch the device. The
    # server enforces a private-network allowlist to avoid being used as an open
    # SSRF proxy.
    esp_fetch_enabled: bool = True
    esp_fetch_timeout_seconds: float = 4.0
    # Only these hosts/prefixes may be fetched (comma separated). Default = the
    # private IPv4 ranges used by phone hotspots and home/clinical LANs.
    esp_fetch_host_allowlist: str = (
        "10.,172.16.,172.17.,172.18.,172.19.,172.20.,172.21.,172.22.,172.23.,"
        "172.24.,172.25.,172.26.,172.27.,172.28.,172.29.,172.30.,172.31.,"
        "192.168.,169.254."
    )
    esp_fetch_allow_localhost: bool = False
    # Simulated blood pressure (potentiometer) alert thresholds. These apply ONLY
    # to values explicitly flagged bp_simulated=true and are NEVER treated as
    # clinical evidence.
    bp_systolic_high: float = 160.0
    bp_systolic_low: float = 90.0
    bp_diastolic_high: float = 100.0
    bp_diastolic_low: float = 60.0


settings = Settings()

SESSION_COOKIE = "icu_session"
DEVICE_HEADER = "X-Device-Token"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
