# ICU Sentinel — Complete Feature & Technical Guide

This document provides an in-depth reference for all functional features, clinical modules, user roles, and hardware integrations available in the **ICU Sentinel** system.

---

## 1. Feature Matrix Overview

| Module / Feature | Supported Capabilities | Access Roles |
|---|---|---|
| **Clinical Triage Board** | `Patient — Disease (Priority)` high-level overview, priority badges, quick vitals, instant toggle to Full Telemetry. | Doctor, Admin |
| **Full Telemetry View** | 13-column hardware telemetry table (BPM, SpO₂, ambient temp, humidity, simulated BP, device ID, signal quality, timestamps). | Doctor, Admin |
| **Live Waveform & Monitoring** | Real-time vital metrics, dynamic charts (HR, SpO₂, ambient temp), connection status, last updated indicators. | Doctor, Admin, Patient (own vitals) |
| **Explainable Early Warning** | Deterministic multi-stage rule engine: persistence counting, hysteresis thresholds, stale data alarms, evidence logging. | All (Evidence viewable by Clinicians) |
| **Disease Flagging** | Automatic categorization: *Cardiac Concern*, *Stroke Suspect*, *Infection / Sepsis Risk*, *Stable / Normal Vitals*, *Technical / Sensor Fault*. | Doctor, Admin |
| **Alert Lifecycle Management** | Four-state workflow (`active` $\rightarrow$ `acknowledged` $\rightarrow$ `under_review` $\rightarrow$ `resolved` $\rightarrow$ `reopened`), post-resolution cooldown window. | Doctor, Admin |
| **FAST Stroke Assessment** | Clinician entry for Face, Arms, Speech, and Time. Triggers instant critical escalation independent of physical sensors. | Doctor, Admin |
| **Role-Based Access Control** | Strictly isolated views: Patients only access their own vitals; Doctors access assigned patients; Admin manages users and system configs. | All |
| **Developer Simulation Panel** | 11 preconfigured clinical scenarios (e.g. sustained tachycardia, hypoxemia, combined critical, probe off). Tagged `is_simulated=True`. | Admin |
| **Data Isolation & Purging** | Test scenario data is visually badged in purple, excluded from real clinical analytics, and purgeable with 1-click. | Admin |
| **Dual Hardware Ingestion** | **ESP32** (HTTP POST with bearer tokens) & **ESP8266** (direct LAN polling with SSRF protection and potentiometer-simulated BP). | Hardware, Server |
| **Audit & Governance** | Comprehensive audit trail: logins, role changes, alert status modifications, note submissions, configuration edits. | Admin |
| **Analytics & Data Export** | Ward summary stats, alert distribution by module, device uptime tracking, and gap-aware CSV data export. | Doctor, Admin |

---

## 2. Clinical Triage Board (`Patient — Disease (Priority)`)

### Purpose
In acute and ICU settings, clinicians need to rapidly assess **who** needs attention, **what** clinical condition is flagged, and **how urgent** the situation is, without sifting through columns of raw ambient sensor numbers.

### Key Elements
* **Patient Identifier**: Displays `P<id> — <Patient Name>` (e.g., `P11 — Jane Doe`).
* **Condition / Disease Flag**: Translates raw numerical abnormalities into intuitive clinical terms:
  - `Cardiac Concern`: Persistent tachycardia, severe bradycardia, or low SpO₂ indicating cardiac distress.
  - `Stroke Suspect`: Triggered by abnormal FAST protocol evaluations or critical vital combinations.
  - `Infection / Sepsis Risk`: Elevated ambient/skin readings paired with high heart rates.
  - `Stable / Normal Vitals`: All measured physiological parameters within normal baseline.
  - `Offline / Insufficient Data`: Bedside sensor disconnected or stale telemetry.
* **Priority Badge**:
  - `CRITICAL` (Red, pulsing): Immediate clinical review required.
  - `HIGH` (Orange): Significant deviation from baseline.
  - `MEDIUM` (Yellow): Moderate warning or persistent minor anomaly.
  - `LOW` (Green/Neutral): Stable patient or minor informational note.
* **Quick Vitals**: Displays current Heart Rate (bpm) and SpO₂ (%) alongside a live pulsing connection badge.
* **Dual-View Switcher**: A single click on `[ 📊 Full Telemetry ]` toggles to the detailed 13-column engineering view displaying device IDs, ambient humidity, and raw timestamps.

---

## 3. Explainable Rule-Based Early Warning Engine

Unlike opaque "black-box" machine learning models, the ICU Sentinel early warning engine is **100% deterministic and explainable**. Every alert generated provides clear, auditable reasoning.

### Stages of Evaluation
1. **Signal Quality & Gating**:
   * Rejects physiologically impossible readings (e.g., HR > 250 bpm or SpO₂ < 40% with poor optical signal).
   * Identifies `finger_removed` / `probe_off` events to prevent false alarm fatigue.
2. **Persistence Filtering**:
   * An intermittent abnormal reading does not trigger an alarm immediately.
   * Requires $N$ consecutive abnormal readings (default: 3) before escalating to an active alert.
3. **Hysteresis Margins**:
   * Prevents alarm "flapping" when vitals hover around threshold boundaries.
   * Once an alarm activates at HR > 120, it does not clear until HR drops below 115 bpm.
4. **Data Staleness Detection**:
   * If a device stops transmitting for > 30 seconds, the system flags `data_stale` rather than assuming the patient is fine.
5. **Evidence Generation**:
   * Every assessment produces human-readable evidence:
     * *Example*: `"Heart rate sustained at 134 bpm (threshold: 120 bpm) for 4 consecutive cycles; SpO2 at 89%."`

---

## 4. Alert Lifecycle & Alarm Management

Designed following clinical alarm management guidelines to avoid alert fatigue:

* **Active**: An alert condition has been detected and persisted past the threshold. Auditory and visual cues are presented.
* **Acknowledged**: A nurse or doctor clicks **Acknowledge**, notifying the team that the alert has been seen.
* **Under Review**: Marks that bedside assessment or clinical intervention is in progress.
* **Resolved**: Clinician clears the alert with optional notes.
* **Cooldown Suppression**: Following resolution, identical alerts for that patient are suppressed for a configurable cooldown window (e.g., 60 seconds) to prevent immediate re-alarming while medication or therapy takes effect.
* **Reopen**: Clinicians can reopen a previously resolved alert if the patient regresses.

---

## 5. Structured FAST Stroke Assessment

A dedicated bedside tool allowing nurses and physicians to record rapid neurological assessments:
* **F — Facial Droop**: Checked if asymmetric smile or facial weakness observed.
* **A — Arm Drift**: Checked if one arm drifts downward when extended.
* **S — Speech Difficulty**: Checked if slurred, inappropriate, or absent speech.
* **T — Time of Onset**: Documented timestamp of when symptoms were first observed.

**Clinical Logic**: Any positive FAST finding immediately triggers an active **CRITICAL** alert under the `stroke_suspect` module, bypassing vital sensor checks.

---

## 6. Hardware Bedside Nodes & Connectivity

### ESP32 Bedside Node
* **Sensors**: MAX30102 (HR & SpO₂ over I2C) + DHT11 (Ambient Temperature & Humidity).
* **Connection**: Wi-Fi station mode connecting to laptop mobile hotspot or ward network.
* **Protocol**: HTTP POST to `/api/v1/ingest` with `X-Device-Token` header every 5 seconds.
* **Fallback**: Buffers readings if Wi-Fi temporarily drops.

### ESP8266 Bedside Node
* **Sensors**: MAX30102 + DHT11 + 10kΩ Potentiometer on `A0` (simulates non-invasive blood pressure changes).
* **Connection**: Runs a lightweight embedded web server serving `GET /data`.
* **Protocol**: Dashboard triggers backend server to direct-poll the ESP's LAN IP address, mitigating browser CORS and mixed-content restrictions.
* **Data Mapping**: Normalizes keys (`bpm` $\rightarrow$ `heart_rate`, `spo2` $\rightarrow$ `spo2`, `pot` $\rightarrow$ `blood_pressure_systolic`).

---

## 7. Developer Test Panel & Clinical Scenarios

Located in the **Admin & Audit** dashboard (`admin.html`), this tool allows testing all alarm workflows, UI states, and notification logic without physical hardware:

1. `sustained_high_hr`: Simulates tachycardia (HR > 130 bpm).
2. `low_spo2`: Simulates hypoxemia (SpO₂ < 88%).
3. `critical_combined`: Simulates acute decompensation (high HR + low SpO₂ + elevated ambient).
4. `stroke_symptoms`: Injects a positive FAST neurological assessment.
5. `finger_removed`: Tests sensor disconnect handling and probe-off detection.
6. `transient_spike`: Tests persistence filtering (single anomaly does not trigger an alarm).
7. `borderline_hysteresis`: Tests anti-flapping logic around threshold boundaries.
8. `stale_sensor`: Tests timeout detection when readings stop.
9. `recurrent_cardiac`: Tests post-resolution alert reopening and cooldown enforcement.

**Safety Feature**: All injected readings are marked `is_simulated = True`. They display in the UI with a distinct purple **`SIMULATED`** badge and can be completely wiped using the **"Clear simulated data"** button.

---

## 8. User Roles & Security Model

* **Administrator (`admin`)**:
  - Full system governance.
  - Approve newly registered doctor accounts.
  - Create and reassign bedside hardware devices and patient tokens.
  - Modify clinical thresholds (e.g., HR max, SpO₂ min) dynamically.
  - Inspect audit trails.
* **Doctor (`doctor`)**:
  - Access to the **Triage Board** and **Full Telemetry**.
  - Monitor assigned patients and write clinical notes.
  - Acknowledge, investigate, and resolve alarms.
  - Conduct FAST assessments.
  - Export patient telemetry data to CSV.
* **Patient (`patient`)**:
  - Isolated dashboard showing only their personal vitals and trend charts.
  - View assigned care team details.
  - Cannot access other patients' records or system administrative controls.

