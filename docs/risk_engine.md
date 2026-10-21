# The explainable risk engine

Source: `backend/services/risk_engine.py` · Alert mapping: `backend/services/alert_manager.py`

## What it is (and is not)

A **deterministic rule engine**: thresholds + persistence + hysteresis +
combination patterns. It is **not** a trained machine-learning model, it does
**not** diagnose disease, and a `low` category never means "safe". Every
assessment stores its reasons, its evidence and a per-module report so a
clinician can see exactly which inputs produced which output.

The function boundary (`assess_patient_risk`) is intentionally narrow so a
validated ML model can be plugged in later behind the same interface
(`ml/README.md`). Nothing in `ml/` is loaded by the backend today.

## Input handling

| Step | Behaviour |
|---|---|
| Validation | `sensor_validation.py` decides if HR / SpO2 are *usable* (signal quality, finger present, physiologic range). Unusable values are stored but excluded from clinical rules. |
| Persistence | `abnormal_persist_count` consecutive abnormal readings are required before a pattern escalates. |
| Hysteresis | `hysteresis_hr` / `hysteresis_spo2` margins stop alerts flapping around a threshold. |
| Staleness | Older than `stale_seconds` → the last value is labelled *not current* (technical alarm, never a clinical one). |
| Thresholds | Config defaults (`ICU_HR_HIGH=120` …) can be overridden at runtime via `admin/settings` (stored in `application_settings`). |

DHT11 readings are **ambient room air** only. They are shown as environmental
context and are never presented as body temperature.

## Per-module reports

Every assessment produces `evidence.modules` with one report per disease-warning
module:

| Module | Statuses | What can flag it |
|---|---|---|
| `possible_sepsis` | `insufficient_parameters`, `possible_risk_flag` | Only a specific pattern (sustained tachycardia **together with** low/declining SpO2). Otherwise it reports **"Insufficient clinical parameters for this assessment"** — body temperature, BP and WBC/lactate are not measured by this hardware and are never invented. |
| `possible_cardiac` | `not_flagged`, `possible_risk_flag`, `unavailable` | Critical HR values, sustained tachycardia/bradycardia, critical or repeated low SpO2, combined HR+SpO2 pattern. **No ECG**: rhythms, arrest and MI cannot be detected; a "not flagged" result is not proof of safety. |
| `stroke_suspect` | `not_flagged`, `flagged` | **Clinician-entered FAST-style symptoms only** (last 24 h). The MAX30102/DHT11 can never detect stroke; the module says so explicitly. Evaluated on *every* path — including when the device has never sent a reading. |
| `general_abnormalities` | `not_flagged`, `flagged`, `unavailable` | Individual measured vital-sign abnormalities (HR, SpO2, environmental context). |
| `technical` | `ok`, `sensor_disconnected_or_offline`, `no_latest_reading`, `invalid_signal`, `stale_data`, `device_offline` | Device/signal/data faults. Never labelled as a disease. |

Each report: `{status, message, severity, evidence, missing_parameters}`.
`missing_parameters` lists exactly what an assessment could not use and why.

The **dominant module** (`evidence.dominant_module`) is chosen by priority:
`stroke_suspect` > `possible_cardiac` > `possible_sepsis` > `technical` >
`general_abnormalities` > `general`.

## Categories

`low` (0–29) < `moderate` (30–49) < `high` (50–74) < `critical` (75–100) —
category and score are a **software priority**, not a clinical score
(`evidence.disclaimer` states this on every record).

## Alert mapping and alarm management

`evaluate_and_open_alerts` turns module flags into alerts
(`backend/services/alert_manager.py`):

| Category | Source |
|---|---|
| `stroke_suspect` | Flagged clinician stroke entry — always **critical**, immediate. |
| `cardiac` | Possible-cardiac concern. |
| `infection` | Possible sepsis concern. |
| `general` | Individual vital-sign abnormalities. |
| `device` | Technical faults: device offline, stale data, invalid signal. |

Alarm-management policy (all configurable in `.env`):

- **Lifecycle**: `active → acknowledged → under_review → resolved`, plus
  `reopen` (resolved → active). Resolve of an already-resolved alert → `409`.
- **Dedupe**: identical events share a dedupe key (per patient for vitals,
  per device for faults, `stroke_suspect:{patient}` for stroke) — one live alert,
  not a flood.
- **Cooldown**: after an alert is resolved, identical events are suppressed for
  `ICU_ALERT_COOLDOWN_MINUTES` (default 10).
- **Escalation**: a `critical` alert unacknowledged for
  `ICU_CRITICAL_ESCALATION_MINUTES` (default 15) is re-escalated by the
  background device-health loop, which also raises `device_offline` and
  `stale_data` technical alarms (`ICU_DEVICE_HEALTH_INTERVAL_SECONDS`, default 30).
- Every state change writes an audit-log entry with actor, note and timestamp.

## Honest limitations

- SpO2 from MAX30102 is an **estimate** (motion/perfusion/fit dependent).
- No blood pressure, no body temperature, no ECG → those assessments report
  `insufficient_parameters` instead of guessing.
- Stroke detection requires a human; sensor AI is never used to confirm or
  rule it out.
