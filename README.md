# ICU Sentinel — AI-Based Real-Time ICU Patient Monitoring & Early-Warning System

A complete, **local-first** hospital patient monitoring prototype: bedside hardware nodes (ESP32 / ESP8266) stream real-time physiological and ambient telemetry to a high-performance FastAPI backend. The system runs an **explainable rule-based early-warning engine**, manages escalating and deduplicated alerts, and presents everything to patients, doctors, and administrators through a responsive, clinical web dashboard.

> **Prototype Disclaimer:** This is research/educational software. It is **not** a certified medical device, not a diagnostic tool, and not a substitute for clinical-grade hospital equipment, certified alarms, or professional medical judgement. DHT11 measures room air temperature, not body temperature. MAX30102 provides estimates. See [`frontend/privacy.html`](file:///d:/Freelance%20Projects/Hospital_Assistant/frontend/privacy.html) for full medical safety and privacy notices.

---

## 1. What It Does & Core Features

| Feature Area | Description |
|---|---|
| **Clinical Triage Board** | **`Patient — Disease (Priority)` view**: Summarizes patient condition, flagged clinical module (e.g. *Cardiac Concern*, *Stroke Suspect*, *Infection/Sepsis Risk*, *Stable*), and priority tier at a glance, with a one-click toggle to Full Telemetry. |
| **Real-Time Vitals** | Heart Rate (bpm) and SpO₂ (%) via MAX30102 pulse oximeter, room ambient temperature (°C) and humidity (%) via DHT11, and simulated Blood Pressure (mmHg) via ESP8266 potentiometer. Streams live over WebSockets with automatic polling fallback. |
| **Explainable Early Warning** | Deterministic multi-stage rule engine: data validation, persistence (consecutive abnormal readings before escalation), hysteresis margins (avoids alarm flapping), data staleness detection, and FAST stroke assessment. Every warning includes written evidence of **which rule fired and why**. |
| **Alert Management** | Lifecycle tracking: `active` $\rightarrow$ `acknowledged` $\rightarrow$ `under_review` $\rightarrow$ `resolved` $\rightarrow$ `reopened`. Deduplicated by patient and condition; post-resolution cooldown suppresses alert storms. |
| **FAST Stroke Protocol** | Structured clinician entry for facial asymmetry, limb weakness, speech difficulty, and sudden neurological change. Evaluated independently of sensors (raises an immediate critical warning). |
| **Role-Based Access Control** | Three enforced roles (`admin`, `doctor`, `patient`): patients see only their own data; doctors see only assigned patients; doctors require admin approval before login. |
| **Developer Test Panel** | 11 preconfigured clinical simulation scenarios (e.g. `sustained_high_hr`, `low_spo2`, `stroke_symptoms`, `finger_removed`). All test readings are explicitly tagged `is_simulated=True`, marked with purple badges, excluded from real statistics, and can be purged in one click. |
| **Dual Hardware Modes** | **ESP32** (HTTP POST with token authentication) and **ESP8266** (direct LAN `/data` polling with SSRF protection). |
| **Analytics & Export** | Ward summaries, alert frequency distribution, device uptime tracking, and gap-aware CSV data exports (real and simulated data separable). |
| **Security & Privacy** | Local SQLite storage (no third-party cloud required), PBKDF2-SHA256 password hashing (200k rounds), signed `HttpOnly` session cookies, login attempt throttling, and comprehensive audit logs. |

---

## 2. Quick Start & User Guide

### A. Pre-configured Credentials

The development database comes preloaded with ready-to-use accounts for immediate evaluation:

| Role | Email | Password | What You Can See / Do |
|---|---|---|---|
| **Doctor** | `doctor@example.org` | `DoctorPass1!` | View assigned patients, use the **Patient Triage Board**, acknowledge/resolve alerts, write doctor notes, and review evidence. |
| **Admin** | `admin@example.org` | `AdminPassw0rd!` | Manage accounts, approve doctor registrations, adjust clinical thresholds, review audit logs, and run simulated test scenarios. |
| **Patient** | `patient@example.org` | `PatientPass1!` | View personal vital trends, live readings, and assigned care team (isolated to own records). |

---

### B. Running on Windows (Scripts Method — Recommended)

The repository provides automated batch scripts in the `scripts/` folder:

```bat
:: 1. One-time setup: creates .venv, installs dependencies, initializes database & admin
scripts\setup.bat

:: 2. Start the FastAPI backend and web server (http://127.0.0.1:8080)
scripts\run.bat

:: 3. Stop the running server when done
scripts\stop.bat
```

Once running, navigate to:
* **Web Dashboard:** [http://127.0.0.1:8080/](http://127.0.0.1:8080/)
* **Interactive API Documentation:** [http://127.0.0.1:8080/docs](http://127.0.0.1:8080/docs)
* **System Health Check:** [http://127.0.0.1:8080/api/v1/health](http://127.0.0.1:8080/api/v1/health)

---

### C. Manual Setup (Terminal)

```bat
:: 1. Create and activate Python virtual environment (Python 3.11+)
py -3 -m venv .venv
.venv\Scripts\activate

:: 2. Install dependencies
pip install -r requirements.txt

:: 3. Configure environment variables
copy .env.example .env

:: 4. (Optional) Create your custom first admin account
python -m backend.create_admin

:: 5. Launch server
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

---

## 3. Step-by-Step Walkthrough Guide

### Step 1: Doctor Triage Dashboard (`Patient — Disease (Priority)`)
1. Open [http://127.0.0.1:8080/login.html](http://127.0.0.1:8080/login.html) and sign in as `doctor@example.org` / `DoctorPass1!`.
2. Click **Patients** (`doctor_dashboard.html`) in the sidebar.
3. The default view is the **Triage Board**:
   * **Patient:** Name and identifier (e.g. `P11 — Jane Doe`).
   * **Flagged Condition:** Clear clinical classification (e.g. `Cardiac Concern`, `Stroke Suspect`, `Stable / Normal Vitals`).
   * **Priority Level:** Color-coded badges (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`).
   * **Quick Vitals:** Current HR & SpO₂ with live connection indicator.
4. Click **"Full Telemetry"** in the top-right whenever you want to inspect raw 13-column hardware telemetry.

### Step 2: Clinical Monitor & Live Waveforms
1. Click **"Monitor"** or click any patient row in the triage list.
2. The **Live Monitoring** screen displays real-time vitals, connection latency, and explainable evidence.
3. Scroll down to review the **Disease-Warning Modules** (`possible_cardiac`, `possible_sepsis`, `stroke_suspect`, `general`, `technical`).

### Step 3: Alert Lifecycle & Notes
1. Open **Alerts & Events** (`alerts.html`).
2. Move an alert through its clinical lifecycle:
   * **Acknowledge**: Notes that a clinician has seen the alarm.
   * **Under Review**: Marks that assessment or intervention is underway.
   * **Resolve**: Resolves the alarm with optional resolution notes (enforces a cooldown window to prevent flapping).
   * **Reopen**: Explicitly re-activates an alarm if symptoms reoccur.

### Step 4: Admin Test Panel (No Hardware Required)
1. Sign in as `admin@example.org` / `AdminPassw0rd!`.
2. Navigate to **Admin & Audit** (`admin.html`) $\rightarrow$ select the **Developer test panel** tab.
3. Select a scenario (e.g. `sustained_high_hr`, `low_spo2`, `critical_combined`, or `stroke_symptoms`) and click **Run**.
4. The vitals appear immediately across all doctor and patient screens with purple **`SIMULATED`** badges.
5. Click **Clear simulated data** to purge scenario readings without touching real patient measurements.

---

## 4. System Architecture & Tech Stack

```
Hospital_Assistant/
├── backend/
│   ├── main.py              FastAPI entry point: routes, static mounting, background loops, WS
│   ├── config.py            Pydantic settings loaded from .env
│   ├── database.py          SQLAlchemy 2.0 SQLite engine, WAL mode, auto-column migrator
│   ├── models.py            Database models: User, Device, Reading, RiskAssessment, Alert, Note, Audit
│   ├── schemas.py           Pydantic request & response models (PatientListItem, IngestReading, etc.)
│   ├── security.py          PBKDF2 hashing, timed session cookies, audit helper, rate limiter
│   ├── dependencies.py      Role & auth guards (require_admin, require_doctor, authenticate_device)
│   ├── create_admin.py      Interactive CLI first-admin bootstrap
│   ├── api/                 Modular FastAPI routers:
│   │   ├── auth.py          Login, logout, registration, session introspection
│   │   ├── patients.py      Patient list, readings history, clinical notes, CSV export
│   │   ├── doctors.py       Doctor-patient assignments, triage overview
│   │   ├── devices.py       Device registration, token rotation, direct ESP8266 proxying
│   │   ├── sensors.py       Hardware /ingest endpoint, live poll snapshot
│   │   ├── alerts.py        Alert actions, lifecycle history, stroke symptom recording
│   │   ├── analytics.py     Ward summaries, alert frequency, device uptime reports
│   │   ├── admin.py         User management, doctor approvals, settings overrides, audit logs
│   │   ├── test_scenarios.py Predefined clinical simulation runner & data cleaner
│   │   └── shared.py        Patient isolation, patient list builder, risk recomputations
│   ├── services/            Business logic & processing engines:
│   │   ├── risk_engine.py   Deterministic explainable multi-module early-warning engine
│   │   ├── alert_manager.py Deduplication, escalation, cooldown, and lifecycle manager
│   │   ├── sensor_validation.py Plausibility bounds, NaN/format checks, signal-quality gating
│   │   ├── device_manager.py Device online/offline tracking, last-seen timestamps
│   │   ├── device_fetcher.py Direct ESP8266 HTTP /data fetcher with SSRF private-IP allowlist
│   │   ├── device_data_mapper.py Normalizes ESP8266 JSON keys into standard IngestReading
│   │   └── ingest_service.py Unified ingestion & patient resolution pipeline
│   └── tests/               53 passing automated pytest unit & integration tests
├── frontend/                Vanilla HTML/CSS/JS (no build step, no npm, fully offline)
│   ├── index.html           Public landing page
│   ├── login.html           Authentication portal
│   ├── register.html        Patient & doctor registration
│   ├── overview.html        Ward overview & patient triage summary
│   ├── doctor_dashboard.html Doctor triage board & full telemetry table
│   ├── patient_monitor.html Bedside live waveform monitor & explainable evidence
│   ├── patient_dashboard.html Patient's personal vitals & warnings view
│   ├── doctors.html         Doctor-patient assignment manager
│   ├── devices.html         Device registry, one-time token display, ESP8266 direct connection
│   ├── alerts.html          Alert inbox, review history, stroke symptom entry
│   ├── risk.html            Explainable risk engine rule reference & audit
│   ├── analytics.html       Ward reports, device uptime, CSV export
│   ├── admin.html           User administration, threshold tuning, developer test panel
│   ├── settings.html        Profile & password management
│   ├── privacy.html         Medical safety & privacy disclaimer
│   └── assets/              Vendored Chart.js, Lucide icons, custom responsive CSS & core JS
├── esp32/
│   ├── icu_monitor_esp32.ino Arduino sketch for ESP32 + MAX30102 + DHT11
│   ├── config.example.h     Template for Wi-Fi SSID, backend URL, and device token
│   └── wiring_guide.md      Hardware schematics, pinouts, and flashing steps
├── esp8266/
│   ├── icu_monitor_esp8266.ino Arduino sketch for ESP8266 with direct HTTP /data server
│   ├── config.example.h     Template for ESP8266 Wi-Fi configuration
│   └── wiring_guide.md      NodeMCU pinouts and potentiometer simulated BP wiring
├── ml/                      Optional offline machine learning experiments (see ml/README.md)
├── docs/                    Reference guides: api.md, risk_engine.md, hotspot_guide.md, test_report.md
├── scripts/                 setup.bat, run.bat, stop.bat
├── requirements.txt         Python package dependencies
├── .env.example             Environment configuration template
├── .gitignore               Git ignore configuration
└── README.md                (This file)
```

---

## 5. Hardware Integration & Wiring

Detailed connection instructions are available in [`esp32/wiring_guide.md`](file:///d:/Freelance%20Projects/Hospital_Assistant/esp32/wiring_guide.md), [`esp8266/wiring_guide.md`](file:///d:/Freelance%20Projects/Hospital_Assistant/esp8266/wiring_guide.md), and [`docs/hotspot_guide.md`](file:///d:/Freelance%20Projects/Hospital_Assistant/docs/hotspot_guide.md).

### ESP32 Bedside Node
* **MAX30102 Pulse Oximeter**: I2C bus (SDA: `GPIO21`, SCL: `GPIO22`, Powered at **3.3V**).
* **DHT11 Ambient Sensor**: 1-wire bus (`GPIO4`, +10kΩ pull-up resistor).
* **Ingestion Method**: Connects to the laptop's 2.4 GHz mobile hotspot and sends HTTP POST requests every 5 seconds to `/api/v1/ingest` with `X-Device-Token`.

### ESP8266 Bedside Node
* **MAX30102**: I2C bus (SDA: `D2`, SCL: `D1`).
* **DHT11**: Pin `D4`.
* **Simulated Blood Pressure**: 10kΩ potentiometer on analog pin `A0` (generates demonstrative BP numbers tagged `bp_simulated=True`).
* **Ingestion Method**: Serves a local web server `GET /data`. The dashboard prompts the server to direct-poll the ESP's IP over LAN to avoid browser mixed-content blocks.

---

## 6. Automated Testing

The project includes **53 passing automated tests** covering authentication, access control, risk assessment, alarm workflows, device communication, and simulated data isolation.

To run the complete test suite:

```bat
.venv\Scripts\python.exe -m pytest backend\tests -v
```

To run a quick server smoke check:

```bat
curl http://127.0.0.1:8080/api/v1/health
```

---

## 7. Security, Privacy & Configuration Notes

* **Local-First & Offline**: All patient vitals, notes, and alerts stay on your local machine in `icu_monitor.db`.
* **Server-Side Enforcement**: Authorization checks execute on every API call — the frontend is never the security boundary.
* **Sensitive Files Protected**: Real database files (`*.db`), virtual environments (`.venv/`), environment secrets (`.env`), and hardware credentials (`esp32/config.h`) are strictly ignored by [`.gitignore`](file:///d:/Freelance%20Projects/Hospital_Assistant/.gitignore).
* **Configurable Thresholds**: Risk thresholds (HR low/high, SpO₂ low/critical, persistence count) can be dynamically adjusted by an administrator under **Admin & Audit $\rightarrow$ Risk Thresholds** without restarting the backend.
