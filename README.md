# ICU Sentinel — AI-Based Real-Time ICU Patient Monitoring, Early-Warning & Doctor–Patient Management System

A complete, **local-first** hospital monitoring prototype: an ESP32 bedside node
streams heart rate, SpO₂ and ambient environment data to a FastAPI backend that
runs an **explainable rule-based early-warning engine**, raises deduplicated and
escalating alerts, and presents everything to patients, doctors and
administrators through a professional, hospital-style web dashboard.

> **Prototype disclaimer.** This is research/educational software. It is **not** a
> certified medical device, not a diagnostic tool, and not a substitute for
> clinical-grade equipment, hospital alarm systems or clinical judgement. See
> [`frontend/privacy.html`](frontend/privacy.html) for the full medical-safety
> and privacy information.

---

## 1. What it does

| Area | Capability |
|---|---|
| **Real-time vitals** | ESP32 + MAX30102 (HR / SpO₂) + DHT11 (ambient temp/humidity) → HTTP ingest → WebSocket live push to open dashboards, with automatic polling fallback |
| **Early warning** | Explainable rule engine: hard thresholds, persistence (consecutive abnormal readings), hysteresis, staleness, device-offline, signal-quality gating, clinician-entered stroke symptoms — every assessment stores **which rule fired and why** |
| **Alerts** | Deduplicated by patient+vital, severity escalation over time, acknowledge/resolve with full history; in-browser sound + Web Notifications |
| **Roles** | `admin`, `doctor`, `patient` — enforced **server-side** (patients see only themselves, doctors only assigned patients) |
| **Devices** | Register/assign/rotate tokens; devices authenticate with `X-Device-Token`; the **patient is resolved server-side**, never trusted from the payload |
| **Analytics** | Ward summaries, alert frequency, device uptime, CSV exports (real and simulated data separable) |
| **Test panel** | 10 named simulated scenarios (`is_simulated=True`, visibly labelled, excluded from real statistics, one-click clear) |
| **Audit** | Login, access, device, assignment and settings changes recorded with actor/action/IP |

---

## 2. Tech stack

- **Backend** — Python 3.11+, FastAPI 0.115, SQLAlchemy 2, Pydantic v2, SQLite,
  passlib PBKDF2-SHA256 password hashing, itsdangerous signed session cookies,
  WebSockets, pytest (26 tests).
- **Frontend** — vanilla HTML/CSS/JS, custom medical design system, Chart.js
  (vendored, offline), Lucide icons (vendored, offline). No build step, no CDN.
- **Firmware** — Arduino ESP32 sketch for MAX30102 + DHT11.
- **ML (optional)** — `ml/` offline experiment; the live system **never** depends on it.

---

## 3. Quick start (Windows)

### Option A — scripts (recommended)

```bat
scripts\setup.bat     :: one-time: venv, dependencies, .env, first admin
scripts\run.bat       :: start server on http://127.0.0.1:8080
scripts\stop.bat      :: stop it again
```

`setup.bat` prompts you interactively for the first admin email/password
(or use env vars before running):

```bat
set ICU_BOOTSTRAP_EMAIL=admin@hospital.local
set ICU_BOOTSTRAP_NAME=Main Admin
set ICU_BOOTSTRAP_PASSWORD=supersecret-admin-pw
scripts\setup.bat
```

### Option B — manual

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env            :: then edit ICU_SECRET_KEY
py -m backend.create_admin        :: interactive first-admin prompt
py -m uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

Then open **http://127.0.0.1:8080/** → *Sign in* with the admin account.

> Generate a strong secret: `py -3 -c "import secrets; print(secrets.token_urlsafe(64))"`
> and put it in `.env` as `ICU_SECRET_KEY`.

---

## 4. First-run demo walkthrough (no hardware needed)

1. **Sign in as admin** → *Administration* page.
2. **Create a patient** — either from *Register* (role: patient) or
   Administration → Users → *Create account*.
3. **Create a doctor** the same way → then **approve** them
   (Administration → Users → *Approve*). Unapproved doctors cannot sign in.
4. **Register a device** → *Devices* page → copy the device token **once**.
   (For hardware, paste `DEVICE_ID` + `DEVICE_TOKEN` into `esp32/config.h`.)
5. **Run a test scenario** → *Administration → Developer test panel* →
   pick e.g. `hr_spike` or `desaturation` → *Run*. The readings appear within
   seconds on *Overview*, *Patient monitor* and *Alerts*, each labelled
   **SIMULATED** (purple badge).
6. **Assign the doctor** → *Doctors* page → assign the patient to the doctor.
7. **Sign in as the doctor** → *Doctor dashboard*: reassess, add clinical notes,
   acknowledge/resolve alerts, review the evidence behind each risk level.
8. **Sign in as the patient** → *Patient dashboard*: own readings, alerts,
   assignments — and nothing else (server-enforced).
9. **Clear simulated data** → test panel → *Clear simulated data* — real
   measurements are untouched.
10. **CSV export** → Patient page → *Export CSV* (includes `is_simulated` column).

---

## 5. Project structure

```
Hospitalai/
├── backend/
│   ├── main.py              FastAPI app: routers, WS live feed, static frontend, health
│   ├── config.py            ICU_* settings via pydantic-settings (.env)
│   ├── database.py          SQLAlchemy engine/session, init_db()
│   ├── models.py            users, patients, doctors, devices, readings,
│   │                        risk assessments, alerts, notes, assignments,
│   │                        audit logs, settings, stroke symptoms
│   ├── schemas.py           Pydantic request/response models
│   ├── security.py          PBKDF2 hashing, session cookies, audit helper
│   ├── dependencies.py      auth/role guards
│   ├── create_admin.py      first-admin bootstrap
│   ├── api/                 auth, patients, doctors, devices, sensors,
│   │                        alerts, analytics, admin, test_scenarios, shared
│   ├── services/            risk_engine, alert_manager, sensor_validation, device_manager
│   └── tests/               41 passing tests (pytest)
├── frontend/
│   ├── index.html           role-aware home
│   ├── login.html / register.html
│   ├── overview.html        ward overview (role-aware)
│   ├── patient_monitor.html clinical monitor (live charts, evidence)
│   ├── doctor_dashboard.html assigned-patient triage
│   ├── patient_dashboard.html patient's own view
│   ├── doctors.html         assignments (admin/doctor)
│   ├── devices.html         device registry & tokens
│   ├── risk.html            risk-rule reference & history
│   ├── alerts.html          alert inbox + stroke symptoms
│   ├── analytics.html       reports & exports
│   ├── admin.html           users, approvals, test panel, thresholds, audit, health
│   ├── settings.html / privacy.html
│   └── assets/
│       ├── css/styles.css   design system (light/dark, accessible contrast)
│       ├── js/core.js       API client, toasts, live WS/polling, charts
│       ├── js/app.js        sidebar shell, roles, theme, alert badge
│       └── js/vendor/       chart.umd.min.js, lucide.min.js (offline)
├── esp32/
│   ├── icu_monitor_esp32.ino firmware
│   ├── config.example.h     copy → config.h (git-ignored, never commit)
│   └── wiring_guide.md      wiring, libraries, flashing, troubleshooting
├── ml/                      OPTIONAL offline experiment (see ml/README.md)
├── docs/                    hotspot_guide, risk_engine, api, test_report
├── scripts/                 setup.bat, run.bat, stop.bat
├── requirements.txt
├── .env.example
└── README.md                (this file)
```

---

## 6. The risk engine (explainable by design)

`backend/services/risk_engine.py` evaluates, in order:

1. **Data validity** — implausible/saturated/invalid readings are rejected and
   recorded with notes instead of being scored.
2. **Thresholds** — HR (low/high/critical-low/critical-high), SpO₂ (low/critical),
   with **admin-configurable overrides** (Administration → Risk thresholds).
3. **Persistence** — an abnormal value must persist for N consecutive readings
   before it escalates (configurable).
4. **Hysteresis** — downgrades require a margin below the threshold, preventing
   flap between levels.
5. **Staleness / device offline** — no fresh data raises its own warning level.
6. **Signal quality** — poor/finger-off signals downgrade confidence rather than
   being silently treated as normal.
7. **Stroke symptoms** — structured FAST-style clinician entry produces a
   critical warning immediately (sensors cannot detect stroke).

Each `RiskAssessment` stores `score`, `category`, `reasons`, and an `evidence`
object that includes a **per-module report** for every disease-warning module
(`possible_sepsis`, `possible_cardiac`, `stroke_suspect`, `general_abnormalities`,
`technical`) with `status`, `message`, `severity`, `evidence` and
`missing_parameters` — the UI's "why this level" and module panels render it.
Categories are **software priorities**, not clinically validated classifications.

Full details: [`docs/risk_engine.md`](docs/risk_engine.md).

---

## 7. API surface (summary)

Base path `/api/v1`. Full interactive docs at **`/docs`**; curated reference in
[`docs/api.md`](docs/api.md).

| Group | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `/auth/logout`, `GET /auth/me`, `POST /auth/register/patient`, `/auth/register/doctor`, `/auth/forgot-password`, `/auth/reset-password` |
| Patients | `GET /patients`, `GET /patients/{id}`, `/readings`, `/alerts`, `/risk-history`, `/notes`, `POST /patients/{id}/reassess`, `/export`, `/summary` |
| Doctors | `GET /doctors/patients`, `/overview`, `/assignments`, `POST /assignments`, `DELETE /assignments/{id}` |
| Devices | `GET/POST /devices`, `POST /devices/{id}/assign\|unassign\|rotate-token\|test` |
| Sensors | `POST /ingest` (device token), `GET /live/{patient_id}` |
| Alerts | `GET /alerts`, `POST /alerts/{id}/acknowledge\|under-review\|resolve\|reopen`, `GET /alerts/{id}/history`, `POST/GET /stroke-symptoms` |
| Analytics | `GET /analytics/summary\|alert-frequency\|device-uptime\|monitoring-summary\|export` |
| Admin | `GET /admin/overview\|users\|audit-logs\|settings\|system-health\|pending-doctors`, `POST /admin/users/create`, `/admin/doctors/{id}/approve\|reject`, `/admin/users/{id}/deactivate\|activate`, `/admin/settings` |
| Test | `GET/POST /test/scenarios`, `POST /test/clear-simulated/{patient_id}` |
| System | `GET /health`, `GET /config/public`, `WS /ws/live/{patient_id}` (session-cookie authenticated) |

**Ingest example:**

```bash
curl -X POST http://127.0.0.1:8080/api/v1/ingest \
  -H "Content-Type: application/json" \
  -H "X-Device-Token: <token-from-devices-page>" \
  -d '{"heart_rate":118,"spo2":91,"signal_quality":"good","finger_detected":true}'
```

Invalid or missing fields are rejected with field-level messages; invalid
readings are stored as invalid with notes, never silently dropped.

---

## 8. ESP32 hardware

See [`esp32/wiring_guide.md`](esp32/wiring_guide.md) for wiring, libraries and
flashing steps, and [`docs/hotspot_guide.md`](docs/hotspot_guide.md) for
connecting the ESP32 to the laptop over a mobile hotspot. Summary:

| Component | Bus / pins |
|---|---|
| MAX30102 | I2C — SDA `GPIO21`, SCL `GPIO22`, **3.3 V** |
| DHT11 | 1-wire — `GPIO4` (+10 kΩ pull-up on bare sensors) |
| Network | 2.4 GHz hotspot; `BACKEND_URL = http://<laptop-hotspot-IP>:8080` |

The firmware reports `null` (not zero) when no finger is present or quality is
insufficient, reconnects Wi-Fi automatically, and never prints the device token
or Wi-Fi password to the Serial Monitor. DHT11 reads **room air only**.

---

## 9. Testing

```bat
.venv\Scripts\python.exe -m pytest backend\tests -q
```

**41 tests passing**, covering: auth & roles, server-side patient isolation,
ingest validation, risk rules (thresholds, persistence, hysteresis, staleness),
per-module risk reports and missing-parameter honesty, full alert lifecycle
(acknowledge / under-review / resolve / reopen, cooldown, dedupe), device
health monitoring, stroke entries with zero sensor data, device auth + patient
resolution from the server side, simulated-data separation, settings overrides
and export behaviour. Details and the live smoke-check results:
[`docs/test_report.md`](docs/test_report.md).

Run the server smoke check:

```bat
curl http://127.0.0.1:8080/api/v1/health
```

---

## 10. Security & privacy notes

- Passwords: **PBKDF2-SHA256**, 200k iterations, unique salt. No plaintext, no default password.
- Sessions: HttpOnly, SameSite=Lax signed cookies, 8 h rolling window.
- Device credentials: unique per device, shown once at registration, rotatable,
  never returned in dashboard responses or logged.
- **Server-side authorization** on every read path — the UI is never the boundary.
- Login throttling (5 attempts / 5 min per username).
- Audit log on all access-relevant actions.
- SQLite stays local; no cloud dependency for core functionality.
- `.env` and `esp32/config.h` are for local secrets — keep them out of version control.
- This local deployment is **not automatically HIPAA/GDPR/DPDP compliant** —
  do not load real patient data without institutional approval and safeguards.

---

## 11. Limitations

- Risk categories come from a **rule engine**, not a trained model — they are
  software priorities, not clinical diagnoses. “Low” never means “safe”.
- SpO₂ from MAX30102 is an **estimate** (motion, perfusion and fit dependent).
- DHT11 measures **ambient air**, never body temperature; the system does not
  measure blood pressure.
- In-browser notifications are not a substitute for hospital alarms.
- `ml/` is an optional offline experiment trained on weak rule-derived labels;
  **no trained model ships with this repository** and the backend never loads it
  (details in [`ml/README.md`](ml/README.md)).
- The **ESP32 firmware was not compiled or flashed on the development machine**
  (no Arduino toolchain there). The source and wiring guide are provided —
  verify flashing and Wi-Fi behaviour on real hardware before relying on it.

---

## 12. Troubleshooting

| Problem | Fix |
|---|---|
| Page says *unauthenticated* | Sign in again — sessions expire after 8 h |
| Doctor cannot sign in | Admin must **approve** the doctor first |
| No live updates | WebSocket may be blocked by a proxy → the UI automatically falls back to polling; check `/api/v1/health` |
| Device shows 401 | Token mismatch → *Devices → Rotate token*, update `config.h`, reflash |
| Device shows offline | Backend not running, wrong `BACKEND_URL` IP, or Windows firewall blocking port 8080 |
| `py` not found | Install Python from python.org with *Add to PATH*, or use `python -m venv` |
| Port already in use | `scripts\stop.bat`, or change `ICU_PORT` |
| Charts empty | No readings yet → run a test scenario from the admin test panel |

---

## 13. License / responsible use

For evaluation and education. Do not use with real patients or real clinical
decisions. If you extend it (new sensors, new rules, ML integration), keep the
same principles: **explainable evidence with every alert, server-side access
control, visible separation of simulated data, and no clinical claims.**
