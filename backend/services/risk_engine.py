"""Layer-2 explainable risk assessment (rule-based prototype).

IMPORTANT: This is a deterministic, threshold + persistence rule engine. It is
NOT a trained machine-learning model. The module boundary
(`assess_patient_risk`) is intentionally narrow so a validated ML model can be
plugged in later (see ml/README.md).

Disease-warning modules (Section 9 of the master spec)
------------------------------------------------------
Every assessment produces per-module reports in ``RiskResult.modules``:

* ``possible_sepsis``       — infection-related deterioration concern.
                              The current hardware cannot measure body
                              temperature or blood pressure, so this module
                              reports "Insufficient clinical parameters for
                              this assessment" unless a persistent
                              tachycardia + low/declining SpO2 pattern exists,
                              which raises a possible-risk flag only.
* ``possible_cardiac``      — cardiac concern from valid HR/SpO2 trends.
                              No ECG: rhythms, arrest and MI cannot be detected.
* ``stroke_suspect``        — clinician-entered FAST-style symptoms only.
                              MAX30102/DHT11 can NEVER detect stroke.
* ``general_abnormalities`` — individual measured vital-sign abnormalities.
* ``technical``             — sensor/device/data faults (offline, stale,
                              poor signal). Never labelled as a disease.
* ``simulated_blood_pressure`` — the ESP8266 potentiometer BP channel. These
                              values are SIMULATED, are never clinical evidence,
                              and never increase the clinical risk category;
                              threshold crossings are reported as a non-clinical
                              demonstration note only.

Each module report has: status, message, severity, evidence, missing_parameters.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import SensorReading, StrokeSymptomEntry, utcnow
from backend.services.sensor_validation import ValidationResult

SEVERITY_ORDER = {"low": 0, "moderate": 1, "high": 2, "critical": 3}


@dataclass
class RiskResult:
    category: str = "low"          # low | moderate | high | critical
    score: float = 0.0             # prototype explainable score, 0-100
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    module: str = "general"        # dominant module name
    # Per disease-warning module reports (Section 9 of the spec). Each entry:
    # {status, message, severity, evidence, missing_parameters}
    modules: dict = field(default_factory=dict)


# Clinical parameters the current hardware CANNOT provide. Modules that would
# need them must say so instead of inventing values.
MISSING_CLINICAL_PARAMS = [
    "body temperature (DHT11 measures ambient room air only — not patient temperature)",
    "blood pressure (no blood-pressure sensor in the current hardware)",
    "ECG (no ECG sensor — cardiac rhythms cannot be identified)",
]

SEPSIS_MISSING_PARAMS = [
    "body temperature (DHT11 measures ambient room air only)",
    "blood pressure (no blood-pressure sensor in the current hardware)",
    "WBC / lactate / clinician infection observations (not integrated)",
]

INSUFFICIENT_SEPSIS_MSG = (
    "Insufficient clinical parameters for this assessment. Possible infection-related "
    "deterioration cannot be confirmed or excluded from heart rate and SpO2 alone; "
    "body temperature and blood pressure are not measured by the current hardware "
    "(DHT11 measures ambient room air only)."
)


def _max_severity(a: str, b: str) -> str:
    return a if SEVERITY_ORDER.get(a, 0) >= SEVERITY_ORDER.get(b, 0) else b


def _module_report(
    status: str,
    message: str,
    *,
    severity: str = "low",
    evidence: dict | None = None,
    missing: list[str] | None = None,
) -> dict:
    return {
        "status": status,
        "message": message,
        "severity": severity,
        "evidence": evidence or {},
        "missing_parameters": missing or [],
    }


def _base_modules() -> dict:
    """Default (unflagged) per-module reports."""
    return {
        "possible_sepsis": _module_report(
            "insufficient_parameters",
            INSUFFICIENT_SEPSIS_MSG,
            missing=SEPSIS_MISSING_PARAMS,
        ),
        "possible_cardiac": _module_report(
            "not_flagged",
            "No heart-rate or SpO2 pattern met a configured possible-cardiac-concern rule. "
            "No ECG is available, so dangerous rhythms, cardiac arrest and heart attack cannot be "
            "detected. A 'not flagged' result is NOT proof of safety.",
            missing=[MISSING_CLINICAL_PARAMS[2]],
        ),
        "stroke_suspect": _module_report(
            "not_flagged",
            "No clinician-entered stroke indicators in the last 24 hours. The ESP32 sensors cannot "
            "detect stroke; a clinician must record FAST-style symptoms via the neurological "
            "symptom assessment form.",
            missing=["neurological examination (clinician entry only — sensors cannot assess this)"],
        ),
        "general_abnormalities": _module_report(
            "not_flagged",
            "No individual vital-sign abnormality met a configured rule.",
        ),
        "technical": _module_report(
            "ok",
            "Sensor data is present and physiological readings are usable.",
        ),
        "simulated_blood_pressure": _module_report(
            "no_simulated_bp",
            "No simulated blood-pressure channel data in this reading. The ESP8266 "
            "potentiometer channel (if fitted) is a demonstration input only and is never "
            "used as clinical evidence.",
            missing=["a validated blood-pressure sensor"],
        ),
    }


def _dominant_module(modules: dict) -> str:
    if modules.get("stroke_suspect", {}).get("status") == "flagged":
        return "stroke_suspect"
    if modules.get("possible_cardiac", {}).get("status") == "possible_risk_flag":
        return "possible_cardiac"
    if modules.get("possible_sepsis", {}).get("status") == "possible_risk_flag":
        return "possible_sepsis"
    if modules.get("technical", {}).get("status") != "ok":
        return "technical"
    if modules.get("general_abnormalities", {}).get("status") == "flagged":
        return "general_abnormalities"
    return "general"


def _load_thresholds(db: Session) -> dict[str, float]:
    """Overlay DB-stored application_settings on top of config defaults."""
    from backend.models import ApplicationSetting

    overrides = {}
    rows = db.execute(select(ApplicationSetting)).scalars().all()
    for row in rows:
        try:
            overrides[row.key] = float(row.value)
        except (TypeError, ValueError):
            continue
    base = {
        "hr_low": settings.hr_low,
        "hr_high": settings.hr_high,
        "hr_critical_low": settings.hr_critical_low,
        "hr_critical_high": settings.hr_critical_high,
        "spo2_low": settings.spo2_low,
        "spo2_critical_low": settings.spo2_critical_low,
        "abnormal_persist_count": settings.abnormal_persist_count,
        "hysteresis_spo2": settings.hysteresis_spo2,
        "hysteresis_hr": settings.hysteresis_hr,
    }
    base.update(overrides)
    return base


def _unavailable_clinical_modules(modules: dict, context: str) -> None:
    """Mark the clinical modules as unassessable (no data), keep technical."""
    modules["possible_cardiac"] = _module_report(
        "unavailable",
        f"Cardiac-concern assessment unavailable: {context}.",
        missing=[MISSING_CLINICAL_PARAMS[2]],
    )
    modules["general_abnormalities"] = _module_report(
        "unavailable",
        f"Vital-sign abnormality assessment unavailable: {context}.",
    )


def _apply_stroke_module(
    result: RiskResult, db: Session, patient_user_id: int, now: datetime
) -> None:
    """Apply clinician-entered stroke indicators — sensor-independent.

    Called on EVERY assessment path (including early returns for no data,
    unusable signal and stale data): a clinician's FAST-style entry must
    raise the urgent warning even when the device has never sent a reading.
    """
    stroke = db.execute(
        select(StrokeSymptomEntry)
        .where(
            StrokeSymptomEntry.patient_user_id == patient_user_id,
            StrokeSymptomEntry.created_at >= now - timedelta(hours=24),
        )
        .order_by(StrokeSymptomEntry.created_at.desc())
    ).scalars().first()
    if not stroke:
        return
    flags = []
    if stroke.facial_asymmetry:
        flags.append("facial asymmetry")
    if stroke.one_sided_weakness:
        flags.append("one-sided weakness")
    if stroke.speech_difficulty:
        flags.append("speech difficulty")
    if stroke.sudden_neuro_change:
        flags.append("sudden neurological change")
    if not flags:
        return
    result.category = "critical"
    result.score = 100.0
    result.reasons.append(
        "Clinician-entered possible stroke indicators detected: "
        + ", ".join(flags)
        + ". Urgent clinical assessment required. Sensor AI is NOT used to confirm or rule out stroke."
    )
    result.evidence["stroke_flags"] = flags
    result.modules["stroke_suspect"] = _module_report(
        "flagged",
        "URGENT: a clinician has entered FAST-style indicators consistent with possible stroke: "
        + ", ".join(flags)
        + ". Immediate clinical assessment per institutional protocol. This warning was created "
        "from clinician input only — the ESP32 sensors cannot detect stroke. Do not wait for "
        "any model to approve an emergency response.",
        severity="critical",
        evidence={
            "flags": flags,
            "entry_time": stroke.created_at.isoformat(),
            "free_text": stroke.free_text,
            "entered_by_user_id": stroke.entered_by,
        },
        missing=["neurological examination (clinician entry only)"],
    )


def assess_patient_risk(
    db: Session,
    patient_user_id: int,
    latest: SensorReading | None,
    validation: ValidationResult | None,
    *,
    now: datetime | None = None,
) -> RiskResult:
    """Produce an explainable prototype risk assessment for one patient."""
    now = now or utcnow()
    thr = _load_thresholds(db)
    result = RiskResult(evidence={}, modules=_base_modules())
    persist_n = max(int(thr["abnormal_persist_count"]), 1)

    # ---- Device / data freshness layer ------------------------------------
    window_start = now - timedelta(minutes=10)
    recent = db.execute(
        select(SensorReading)
        .where(
            SensorReading.patient_user_id == patient_user_id,
            SensorReading.received_at >= window_start,
        )
        .order_by(SensorReading.received_at.desc())
        .limit(60)
    ).scalars().all()

    if not recent:
        result.category = "moderate"
        result.score = max(result.score, 30.0)
        result.reasons.append("No sensor data received in the last 10 minutes (device may be offline).")
        result.evidence["data_window_count"] = 0
        result.modules["technical"] = _module_report(
            "sensor_disconnected_or_offline",
            "TECHNICAL ALARM: no readings received in the assessment window. The device may be "
            "offline, unpowered, out of range of the hotspot, or not pointed at this backend. "
            "This is a technical fault, not a clinical finding.",
            severity="moderate",
            evidence={"data_window_count": 0},
        )
        _unavailable_clinical_modules(result.modules, "no physiological data in the window")
        _apply_stroke_module(result, db, patient_user_id, now)
        result.module = _dominant_module(result.modules)
        return _finish(result)

    if latest is None:
        result.category = "moderate"
        result.score = max(result.score, 30.0)
        result.reasons.append("No latest reading available for assessment.")
        result.modules["technical"] = _module_report(
            "no_latest_reading",
            "TECHNICAL ALARM: readings exist in the window but no latest reading is available.",
            severity="moderate",
        )
        _unavailable_clinical_modules(result.modules, "no latest reading")
        _apply_stroke_module(result, db, patient_user_id, now)
        result.module = _dominant_module(result.modules)
        return _finish(result)

    age = (now - latest.received_at).total_seconds()
    result.evidence["latest_reading_age_seconds"] = round(age, 1)
    result.evidence["latest_reading_time"] = latest.received_at.isoformat()
    stale_threshold = settings.stale_seconds

    physio_usable = bool(validation and (validation.hr_usable or validation.spo2_usable))
    invalid_streak = 0
    for r in recent[:persist_n]:
        if not r.is_valid:
            invalid_streak += 1

    if not physio_usable:
        result.category = _max_severity(result.category, "moderate")
        result.score = max(result.score, 25.0)
        result.reasons.append(
            "Physiological readings are currently unusable (poor signal quality, finger removed, or invalid MAX30102 data)."
        )
        if invalid_streak >= persist_n:
            result.category = _max_severity(result.category, "high")
            result.score = max(result.score, 45.0)
            result.reasons.append(
                f"{invalid_streak} consecutive unusable physiological measurements — verify sensor contact and device placement."
            )
        result.evidence["signal_quality"] = latest.signal_quality
        result.evidence["invalid_streak"] = invalid_streak
        result.modules["technical"] = _module_report(
            "invalid_signal",
            "TECHNICAL ALARM: the MAX30102 signal is currently unusable "
            f"(quality='{latest.signal_quality}', invalid streak {invalid_streak}). "
            "Invalid values are excluded from clinical-risk calculations. Verify finger placement, "
            "contact and sensor wiring. This is a technical fault, not a clinical finding.",
            severity="high" if invalid_streak >= persist_n else "moderate",
            evidence={"signal_quality": latest.signal_quality, "invalid_streak": invalid_streak},
        )
        _unavailable_clinical_modules(result.modules, "physiological signal is currently unusable")
        _apply_stroke_module(result, db, patient_user_id, now)
        result.module = _dominant_module(result.modules)
        return _finish(result)

    if age > stale_threshold:
        result.category = _max_severity(result.category, "moderate")
        result.score = max(result.score, 30.0)
        result.reasons.append(
            f"Latest valid reading is {int(age)}s old (stale threshold {stale_threshold}s). Trend data may not reflect current status."
        )
        result.modules["technical"] = _module_report(
            "stale_data",
            f"TECHNICAL ALARM: latest valid reading is {int(age)}s old (threshold {stale_threshold}s). "
            "The last stored value is NOT a current live measurement. Check device power, Wi-Fi "
            "hotspot and backend URL configuration.",
            severity="moderate",
            evidence={"age_seconds": round(age, 1), "threshold_seconds": stale_threshold},
        )

    hr = latest.heart_rate if (validation and validation.hr_usable) else None
    spo2 = latest.spo2 if (validation and validation.spo2_usable) else None
    result.evidence["heart_rate"] = hr
    result.evidence["spo2"] = spo2
    result.evidence["ambient_temperature"] = latest.ambient_temperature
    result.evidence["ambient_humidity"] = latest.ambient_humidity
    result.evidence["signal_quality"] = latest.signal_quality

    # ---- Persistence analysis over recent valid readings -------------------
    valid_recent = [r for r in recent if r.is_valid][:30]
    hr_values = [r.heart_rate for r in valid_recent if r.heart_rate is not None]
    spo2_values = [r.spo2 for r in valid_recent if r.spo2 is not None]

    def _persist_high(values: list[float], threshold: float) -> int:
        count = 0
        for v in reversed(values):
            if v > threshold:
                count += 1
            else:
                break
        return count

    def _persist_low(values: list[float], threshold: float) -> int:
        count = 0
        for v in reversed(values):
            if v < threshold:
                count += 1
            else:
                break
        return count

    hr_hyst = float(thr["hysteresis_hr"])
    sp_hyst = float(thr["hysteresis_spo2"])
    hr_low_pc = hr_high_pc = spo2_low_pc = 0
    general_flags: list[str] = []

    # ---- Heart-rate rules --------------------------------------------------
    if hr is not None:
        if hr <= thr["hr_critical_low"]:
            result.category = _max_severity(result.category, "critical")
            result.score = max(result.score, 90.0)
            result.reasons.append(f"Heart rate {hr:.0f} bpm is at/below the critical low threshold ({thr['hr_critical_low']:.0f} bpm).")
            general_flags.append(f"critical low heart rate ({hr:.0f} bpm)")
        elif hr >= thr["hr_critical_high"]:
            result.category = _max_severity(result.category, "critical")
            result.score = max(result.score, 90.0)
            result.reasons.append(f"Heart rate {hr:.0f} bpm is at/above the critical high threshold ({thr['hr_critical_high']:.0f} bpm).")
            general_flags.append(f"critical high heart rate ({hr:.0f} bpm)")
        elif hr < thr["hr_low"] - hr_hyst:
            hr_low_pc = _persist_low(hr_values, thr["hr_low"] - hr_hyst)
            sev = "high" if hr_low_pc >= persist_n else "moderate"
            result.category = _max_severity(result.category, sev)
            result.score = max(result.score, 55.0 if hr_low_pc >= persist_n else 35.0)
            result.reasons.append(
                f"Heart rate {hr:.0f} bpm is below the low threshold ({thr['hr_low']:.0f} bpm)"
                + (f", sustained for {hr_low_pc} consecutive readings." if hr_low_pc else " (single reading).")
            )
            result.evidence["hr_low_streak"] = hr_low_pc
            general_flags.append(
                f"low heart rate ({hr:.0f} bpm"
                + (f", {hr_low_pc} consecutive)" if hr_low_pc >= persist_n else ", single reading)")
            )
        elif hr > thr["hr_high"] + hr_hyst:
            hr_high_pc = _persist_high(hr_values, thr["hr_high"] + hr_hyst)
            sev = "high" if hr_high_pc >= persist_n else "moderate"
            result.category = _max_severity(result.category, sev)
            result.score = max(result.score, 55.0 if hr_high_pc >= persist_n else 35.0)
            result.reasons.append(
                f"Heart rate {hr:.0f} bpm is above the high threshold ({thr['hr_high']:.0f} bpm)"
                + (f", sustained for {hr_high_pc} consecutive readings." if hr_high_pc else " (single reading).")
            )
            result.evidence["hr_high_streak"] = hr_high_pc
            general_flags.append(
                f"high heart rate ({hr:.0f} bpm"
                + (f", {hr_high_pc} consecutive)" if hr_high_pc >= persist_n else ", single reading)")
            )

    # ---- SpO2 rules --------------------------------------------------------
    if spo2 is not None:
        if spo2 <= thr["spo2_critical_low"]:
            result.category = _max_severity(result.category, "critical")
            result.score = max(result.score, 95.0)
            result.reasons.append(
                f"SpO2 estimate {spo2:.0f}% is at/below the critical desaturation threshold ({thr['spo2_critical_low']:.0f}%). "
                "Confirm with clinical-grade equipment."
            )
            general_flags.append(f"critical desaturation estimate ({spo2:.0f}%)")
        elif spo2 < thr["spo2_low"] - sp_hyst:
            spo2_low_pc = _persist_low(spo2_values, thr["spo2_low"] - sp_hyst)
            sev = "high" if spo2_low_pc >= persist_n else "moderate"
            result.category = _max_severity(result.category, sev)
            result.score = max(result.score, 60.0 if spo2_low_pc >= persist_n else 40.0)
            result.reasons.append(
                f"SpO2 estimate {spo2:.0f}% is below the low threshold ({thr['spo2_low']:.0f}%)"
                + (f", sustained for {spo2_low_pc} consecutive readings (repeated desaturation pattern)." if spo2_low_pc else " (single reading).")
            )
            result.evidence["spo2_low_streak"] = spo2_low_pc
            general_flags.append(
                f"low SpO2 estimate ({spo2:.0f}%"
                + (f", {spo2_low_pc} consecutive)" if spo2_low_pc >= persist_n else ", single reading)")
            )

    # ---- Combined-pattern rules -------------------------------------------
    hr_abnormal = hr is not None and (hr < thr["hr_low"] - hr_hyst or hr > thr["hr_high"] + hr_hyst)
    spo2_abnormal = spo2 is not None and spo2 < thr["spo2_low"] - sp_hyst
    combined = hr_abnormal and spo2_abnormal
    if combined:
        result.category = _max_severity(result.category, "high")
        result.score = max(result.score, 70.0)
        result.reasons.append(
            "Combined abnormal pattern: both heart rate and SpO2 estimate are outside configured thresholds. "
            "Requires prompt clinical review. (HR/SpO2 alone cannot diagnose a specific disease.)"
        )
        general_flags.append("combined heart-rate + SpO2 abnormality")

    # ---- Ambient environment (context only, never patient temperature) ----
    if latest.ambient_temperature is not None:
        if latest.ambient_temperature > settings.ambient_temp_high or latest.ambient_temperature < settings.ambient_temp_low:
            result.score = min(result.score + 5.0, 100.0)
            result.reasons.append(
                f"Ambient temperature {latest.ambient_temperature:.1f}°C is outside the comfortable room range "
                f"({settings.ambient_temp_low:.0f}–{settings.ambient_temp_high:.0f}°C). "
                "This is an environmental reading, NOT patient body temperature."
            )
    if latest.ambient_humidity is not None:
        if latest.ambient_humidity > settings.ambient_humidity_high or latest.ambient_humidity < settings.ambient_humidity_low:
            result.score = min(result.score + 3.0, 100.0)
            result.reasons.append(
                f"Ambient humidity {latest.ambient_humidity:.0f}% is outside the comfortable range "
                f"({settings.ambient_humidity_low:.0f}–{settings.ambient_humidity_high:.0f}%). Environmental context only."
            )

    # ---- Simulated blood pressure (ESP8266 potentiometer) ------------------
    # These numbers come from a potentiometer, not a BP sensor. They are shown
    # for demonstration and MUST NOT raise the clinical risk category. Crossing a
    # configured threshold produces an informational note in the BP module only.
    if validation and validation.bp_simulated and validation.bp_usable:
        sys_bp = validation.systolic_bp
        dia_bp = validation.diastolic_bp
        result.evidence["simulated_systolic_bp"] = sys_bp
        result.evidence["simulated_diastolic_bp"] = dia_bp
        crossings: list[str] = []
        if sys_bp is not None and sys_bp >= settings.bp_systolic_high:
            crossings.append(f"systolic {sys_bp:.0f} ≥ {settings.bp_systolic_high:.0f}")
        if sys_bp is not None and sys_bp <= settings.bp_systolic_low:
            crossings.append(f"systolic {sys_bp:.0f} ≤ {settings.bp_systolic_low:.0f}")
        if dia_bp is not None and dia_bp >= settings.bp_diastolic_high:
            crossings.append(f"diastolic {dia_bp:.0f} ≥ {settings.bp_diastolic_high:.0f}")
        if dia_bp is not None and dia_bp <= settings.bp_diastolic_low:
            crossings.append(f"diastolic {dia_bp:.0f} ≤ {settings.bp_diastolic_low:.0f}")
        sev = "moderate" if crossings else "low"
        note = (
            "SIMULATED blood pressure from the ESP8266 potentiometer channel "
            f"(systolic {sys_bp if sys_bp is not None else '—'}, diastolic {dia_bp if dia_bp is not None else '—'} mmHg). "
            "This is NOT a blood-pressure measurement and is excluded from clinical risk scoring; "
            "it cannot confirm or exclude any condition."
        )
        if crossings:
            note += " Demonstration thresholds crossed (non-clinical): " + "; ".join(crossings) + "."
        result.modules["simulated_blood_pressure"] = _module_report(
            "simulated_threshold_crossed" if crossings else "simulated_within_range",
            note,
            severity=sev,
            evidence={
                "systolic_bp": sys_bp,
                "diastolic_bp": dia_bp,
                "bp_unit": "mmHg",
                "bp_simulated": True,
                "threshold_crossings": crossings,
            },
            missing=["a validated blood-pressure sensor"],
        )

    # ---- Clinician-entered stroke symptoms (independent of sensors) -------
    _apply_stroke_module(result, db, patient_user_id, now)

    # ---- Module: possible cardiac emergency concern ------------------------
    cardiac_bits = []
    if hr is not None:
        if hr <= thr["hr_critical_low"] or hr >= thr["hr_critical_high"]:
            cardiac_bits.append(f"critical heart-rate value ({hr:.0f} bpm)")
    if hr_high_pc >= persist_n:
        cardiac_bits.append(f"sustained tachycardia pattern ({hr_high_pc} consecutive elevated readings)")
    if hr_low_pc >= persist_n:
        cardiac_bits.append(f"sustained bradycardia pattern ({hr_low_pc} consecutive low readings)")
    if spo2 is not None and spo2 <= thr["spo2_critical_low"]:
        cardiac_bits.append(f"critical desaturation estimate ({spo2:.0f}%)")
    if spo2_low_pc >= persist_n:
        cardiac_bits.append(f"repeated desaturation estimates ({spo2_low_pc} consecutive)")
    if combined:
        cardiac_bits.append("combined heart-rate + SpO2 abnormality")
    if cardiac_bits:
        sev = "critical" if result.category == "critical" else ("high" if result.category in ("high", "critical") else "moderate")
        result.modules["possible_cardiac"] = _module_report(
            "possible_risk_flag",
            "Possible cardiac concern based on available heart-rate / SpO2 trends: "
            + "; ".join(cardiac_bits)
            + ". Requires appropriate clinical assessment. IMPORTANT: no ECG is available — this "
            "system cannot identify dangerous rhythms and cannot detect or exclude cardiac arrest "
            "or heart attack. This is a possible-risk flag, not a diagnosis.",
            severity=sev,
            evidence={
                "heart_rate": hr,
                "spo2": spo2,
                "hr_high_streak": hr_high_pc,
                "hr_low_streak": hr_low_pc,
                "spo2_low_streak": spo2_low_pc,
                "combined_pattern": combined,
            },
            missing=[MISSING_CLINICAL_PARAMS[2]],
        )

    # ---- Module: possible infection-related deterioration (sepsis concern) -
    # Needs body temperature, BP and clinical observations — none available.
    # Only a specific sensor pattern (sustained tachycardia together with low or
    # declining SpO2) raises a cautious possible-risk flag.
    hr_rising = len(hr_values) >= 3 and (hr_values[-1] - hr_values[0]) >= 15
    spo2_declining = len(spo2_values) >= 3 and (spo2_values[0] - spo2_values[-1]) >= 3
    tachy_pattern = hr is not None and (hr > thr["hr_high"] + hr_hyst or hr_high_pc >= persist_n)
    spo2_pattern = spo2_abnormal or spo2_declining
    if tachy_pattern and spo2_pattern:
        result.modules["possible_sepsis"] = _module_report(
            "possible_risk_flag",
            "Possible infection-related deterioration pattern: persistent or sustained tachycardia "
            "together with a low or declining SpO2 estimate. This is a possible-risk flag for clinical "
            "review only — NOT a sepsis diagnosis, which the current hardware cannot make. "
            "Insufficient clinical parameters for a fuller assessment (no body temperature, blood "
            "pressure, WBC or lactate): body temperature cannot be inferred from the ambient DHT11 reading.",
            severity="moderate",
            evidence={
                "heart_rate": hr,
                "spo2": spo2,
                "hr_high_streak": hr_high_pc,
                "hr_rising_within_window": hr_rising,
                "spo2_declining_within_window": spo2_declining,
            },
            missing=SEPSIS_MISSING_PARAMS,
        )
        # A possible-risk flag contributes to the overall priority but does not
        # silently override a higher clinical category already assigned.
        result.category = _max_severity(result.category, "moderate")
        result.score = max(result.score, 50.0)
        result.reasons.append(
            "Possible infection-related deterioration pattern (sustained tachycardia with low/declining "
            "SpO2 estimate). Insufficient clinical parameters for fuller assessment — body temperature "
            "and blood pressure are not available from this hardware."
        )

    # ---- Module: general physiological abnormalities -----------------------
    if general_flags:
        result.modules["general_abnormalities"] = _module_report(
            "flagged",
            "Measured vital-sign abnormalities: " + "; ".join(general_flags) + ".",
            severity=result.category if result.category in ("moderate", "high", "critical") else "moderate",
            evidence={"flags": general_flags},
        )

    if not result.reasons:
        result.reasons.append("No concerning pattern detected in currently available data.")
        result.category = "low"
        result.score = min(result.score, 10.0)

    result.module = _dominant_module(result.modules)
    # A Low software risk is NOT proof of safety — the UI must state this.
    return _finish(result)


def _finish(result: RiskResult) -> RiskResult:
    result.score = max(0.0, min(100.0, result.score))
    result.evidence["disclaimer"] = (
        "Prototype rule-based software priority, not a clinically validated risk score or diagnosis."
    )
    result.evidence["modules"] = result.modules
    result.evidence["dominant_module"] = result.module
    return result


def assessment_to_json(result: RiskResult) -> tuple[str, str]:
    return json.dumps(result.reasons), json.dumps(result.evidence)
