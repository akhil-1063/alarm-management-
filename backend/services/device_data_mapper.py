"""Map a raw ESP8266 ``GET /data`` JSON object into a normalised reading.

The firmware (see ``esp8266/``) serves a flat JSON object. Field names differ
from the ingest contract, so this module translates them and records any keys it
did not recognise. Nothing is invented: missing keys stay ``None`` and are never
zero-filled.
"""
from __future__ import annotations

from typing import Any

from backend.schemas import IngestReading

# Firmware /data key  ->  platform field. Straight aliases.
_ALIASES: dict[str, str] = {
    "heart_rate": "heart_rate",
    "spo2": "spo2",
    "ambient_temperature_c": "ambient_temperature",
    "ambient_temperature": "ambient_temperature",
    "humidity_percent": "ambient_humidity",
    "ambient_humidity": "ambient_humidity",
    "systolic_bp": "systolic_bp",
    "diastolic_bp": "diastolic_bp",
    "bp_unit": "bp_unit",
    "bp_simulated": "bp_simulated",
    "bp_pot_raw": "bp_pot_raw",
    "finger_present": "finger_detected",
    "hr_valid": "hr_valid",
    "spo2_valid": "spo2_valid",
    "buzzer_alert": "buzzer_alert",
    "firmware_version": "firmware_version",
    "ip": "ip_address",
}

_KNOWN_EXTRA_KEYS = {
    "device", "wifi_connected", "bp_alert", "hr_alert", "spo2_alert",
    "raw_ir", "raw_red", "uptime_ms",
}


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "on"}:
            return True
        if v in {"false", "0", "no", "off"}:
            return False
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def map_device_data(raw: dict[str, Any]) -> tuple[IngestReading, list[str]]:
    """Translate raw device JSON into an IngestReading plus human-readable notes.

    The returned IngestReading still passes through the normal validation and
    storage pipeline; this only handles naming and light type coercion.
    """
    notes: list[str] = []
    fields: dict[str, Any] = {}

    for src_key, dst_key in _ALIASES.items():
        if src_key not in raw:
            continue
        value = raw[src_key]
        if dst_key in {
            "bp_simulated", "finger_detected", "hr_valid", "spo2_valid", "buzzer_alert",
        }:
            coerced = _coerce_bool(value)
        elif dst_key == "bp_pot_raw":
            coerced = _coerce_int(value)
        elif dst_key in {"bp_unit", "firmware_version", "ip_address"}:
            coerced = None if value is None else str(value)
        else:
            coerced = _coerce_float(value)
        if coerced is not None:
            fields[dst_key] = coerced

    # The firmware always labels potentiometer BP explicitly. If a device reports
    # BP numbers but does not set the flag, we still mark it simulated so the
    # platform never treats unknown-source BP as clinical evidence.
    if ("systolic_bp" in fields or "diastolic_bp" in fields) and not fields.get("bp_simulated"):
        fields["bp_simulated"] = True
        notes.append(
            "Device reported blood pressure without an explicit source flag; "
            "treated as simulated (potentiometer) and excluded from clinical logic."
        )

    # Derive signal_quality from the firmware's validity flags.
    hr_ok = fields.get("hr_valid", False)
    spo2_ok = fields.get("spo2_valid", False)
    finger = fields.get("finger_detected", True)
    if not finger:
        quality = "no_finger"
    elif hr_ok and spo2_ok:
        quality = "good"
    elif hr_ok or spo2_ok:
        quality = "fair"
    elif "hr_valid" in fields or "spo2_valid" in fields:
        # Device explicitly reported finger present but no usable physiological
        # signal — treat as a poor-signal condition, not "unknown".
        quality = "poor" if finger else "no_finger"
    else:
        quality = "unknown"
    fields["signal_quality"] = quality

    unknown = [k for k in raw.keys() if k not in _ALIASES and k not in _KNOWN_EXTRA_KEYS]
    if unknown:
        notes.append("Unrecognised device fields ignored: " + ", ".join(sorted(unknown)))

    reading = IngestReading(**fields)
    return reading, notes
