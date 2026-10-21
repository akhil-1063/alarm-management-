"""Layer-1 safety/validation for incoming sensor payloads.

Rejects or flags malformed, out-of-range, missing and stale values so they
never masquerade as valid measurements or trigger misleading clinical alarms.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Physiologically-plausible hard bounds for the prototype (device reports only).
HR_HARD_MIN, HR_HARD_MAX = 20.0, 250.0
SPO2_HARD_MIN, SPO2_HARD_MAX = 50.0, 100.0
AMB_TEMP_HARD_MIN, AMB_TEMP_HARD_MAX = -20.0, 60.0
AMB_HUM_HARD_MIN, AMB_HUM_HARD_MAX = 0.0, 100.0
# Simulated BP (potentiometer) has its own wide sanity bounds only — it is never
# a validated clinical measurement.
BP_HARD_MIN, BP_HARD_MAX = 0.0, 400.0


@dataclass
class ValidationResult:
    heart_rate: float | None = None
    spo2: float | None = None
    ambient_temperature: float | None = None
    ambient_humidity: float | None = None
    # Simulated blood pressure (potentiometer) — display only, never clinical.
    systolic_bp: float | None = None
    diastolic_bp: float | None = None
    bp_simulated: bool = False
    bp_usable: bool = False
    signal_quality: str = "unknown"
    is_valid: bool = True
    notes: list[str] = field(default_factory=list)
    # Flags used by the risk engine to avoid clinical interpretation of bad data
    hr_usable: bool = False
    spo2_usable: bool = False
    amb_usable: bool = False

    @property
    def notes_joined(self) -> str | None:
        return "; ".join(self.notes) if self.notes else None


def _check(value: float | None, lo: float, hi: float, name: str, res: ValidationResult) -> float | None:
    if value is None:
        res.notes.append(f"{name} missing")
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        res.notes.append(f"{name} non-numeric")
        return None
    if v != v:  # NaN
        res.notes.append(f"{name} NaN")
        return None
    if v < lo or v > hi:
        res.notes.append(f"{name} out of range ({v})")
        return None
    return v


def validate_reading(
    *,
    heart_rate: float | None,
    spo2: float | None,
    ambient_temperature: float | None,
    ambient_humidity: float | None,
    signal_quality: str,
    finger_detected: bool = True,
    systolic_bp: float | None = None,
    diastolic_bp: float | None = None,
    bp_simulated: bool = False,
) -> ValidationResult:
    """Validate one reading. Values outside hard bounds are nulled and noted.

    A reading is considered "clinically usable" only when signal quality is
    good/fair AND a finger is detected.

    Simulated blood pressure (potentiometer) is kept in the record for display
    and trend only. It is sanity-checked against very wide bounds and is NEVER
    marked clinically usable, regardless of value.
    """
    res = ValidationResult(signal_quality=(signal_quality or "unknown").lower())

    hr = _check(heart_rate, HR_HARD_MIN, HR_HARD_MAX, "heart_rate", res)
    sp = _check(spo2, SPO2_HARD_MIN, SPO2_HARD_MAX, "spo2", res)
    at = _check(ambient_temperature, AMB_TEMP_HARD_MIN, AMB_TEMP_HARD_MAX, "ambient_temperature", res)
    ah = _check(ambient_humidity, AMB_HUM_HARD_MIN, AMB_HUM_HARD_MAX, "ambient_humidity", res)

    res.heart_rate = hr
    res.spo2 = sp
    res.ambient_temperature = at
    res.ambient_humidity = ah

    if systolic_bp is not None or diastolic_bp is not None:
        sys_bp = _check(systolic_bp, BP_HARD_MIN, BP_HARD_MAX, "systolic_bp", res)
        dia_bp = _check(diastolic_bp, BP_HARD_MIN, BP_HARD_MAX, "diastolic_bp", res)
        res.systolic_bp = sys_bp
        res.diastolic_bp = dia_bp
        res.bp_simulated = bool(bp_simulated)
        res.bp_usable = sys_bp is not None or dia_bp is not None
        if res.bp_simulated:
            res.notes.append(
                "blood pressure is SIMULATED from a potentiometer — display/trend only, NOT a measurement"
            )
        else:
            res.notes.append(
                "blood pressure source is not a validated BP sensor — treated as non-clinical"
            )

    quality_ok = res.signal_quality in {"good", "fair"}
    finger_ok = bool(finger_detected)

    if not finger_ok:
        res.notes.append("finger not detected — physiological values not usable")
    if not quality_ok:
        if res.signal_quality in {"no_finger", "invalid", "poor"}:
            res.notes.append(f"signal quality '{res.signal_quality}' — values not clinically usable")
        else:
            res.notes.append(f"signal quality '{res.signal_quality}' — treated as unusable")

    usable = quality_ok and finger_ok
    res.hr_usable = usable and hr is not None
    res.spo2_usable = usable and sp is not None
    # Ambient readings come from DHT11 and are independent of the MAX30102 finger check.
    res.amb_usable = (at is not None) or (ah is not None)

    if not usable and (hr is not None or sp is not None):
        # Keep the numbers in the record for diagnostics but flag the whole
        # physiological part invalid so risk engine and dashboards treat them
        # as unreliable measurements, not normal values.
        res.is_valid = False
    elif hr is None and sp is None and at is None and ah is None:
        res.is_valid = False
        if res.bp_usable:
            # A reading carrying ONLY simulated BP is stored for display but is
            # explicitly not a valid clinical reading.
            res.notes.append("no usable clinical measurements in payload (simulated BP only)")
        else:
            res.notes.append("no usable measurements in payload")
    else:
        res.is_valid = True

    return res
