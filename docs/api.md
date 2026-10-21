# API reference — `/api/v1`

Interactive docs: **`http://127.0.0.1:8080/docs`** (Swagger UI). This file is the
curated overview; Swagger is authoritative for schemas.

## Authentication

| Caller | Mechanism |
|---|---|
| Users (admin / doctor / patient) | Signed **session cookie** `icu_session` set by `POST /auth/login` (HttpOnly, SameSite=Lax, `ICU_SESSION_MINUTES` window). |
| ESP32 devices | Header **`X-Device-Token: <token>`** on `POST /ingest`. The token is shown once at registration and rotatable. The patient is resolved **server-side** from the device assignment — a device can never choose which patient it writes to. |

Unauthorized → `401`, forbidden/other patient → `403`, unknown id → `404`.
Errors use FastAPI's shape: `{"detail": "..."}` or
`{"detail": [{"loc", "msg", "type"}]}` for validation errors.

## Endpoints

### Auth
| Method | Path | Notes |
|---|---|---|
| POST | `/auth/login` | `{email, password}` → sets session cookie |
| POST | `/auth/logout` | clears session |
| GET | `/auth/me` | current user + role |
| POST | `/auth/register/patient` | self-registration |
| POST | `/auth/register/doctor` | creates account **pending admin approval** |
| POST | `/auth/forgot-password`, `/auth/reset-password` | token-based reset |

### Patients (visibility enforced server-side)
| Method | Path | Notes |
|---|---|---|
| GET | `/patients` | admin/doctor: only visible patients |
| GET | `/patients/{id}` | profile + latest state |
| GET | `/patients/{id}/readings?limit&hours` | sensor history |
| GET | `/patients/{id}/alerts` | patient's alerts |
| GET | `/patients/{id}/risk-history?limit` | assessments incl. `evidence.modules` |
| POST | `/patients/{id}/reassess` | re-run risk engine on stored data |
| GET/POST | `/patients/{id}/notes` | clinical notes (doctor/admin) |
| GET | `/patients/{id}/summary` | dashboard summary |
| GET | `/patients/{id}/export` | data export |

### Doctors
`GET /doctors/patients` · `GET /doctors/overview` · `GET|POST /doctors/assignments` · `DELETE /doctors/assignments/{id}`

### Devices
| Method | Path | Notes |
|---|---|---|
| GET | `/devices` | enriched: `patient_name`, `last_reading_at`, `data_age_seconds`, `data_freshness` (`fresh/stale/no_data`), `last_ingest_at`, `last_ingest_status`, `last_error`, `auth_status`. Never includes secrets. |
| POST | `/devices/register` | returns `device_token` **once** |
| POST | `/devices/{id}/assign` / `/unassign` | one device ↔ exactly one patient |
| POST | `/devices/{id}/rotate-token` | old token dies immediately |
| POST | `/devices/{id}/test` | connectivity + ingest diagnostics |

### Sensor ingest
```
POST /ingest
Headers: X-Device-Token: <token>
Body: {
  "device_uid": "ESP32-ICU-001",          // optional, must match token
  "heart_rate": 118,                       // int, physiologic range or null
  "spo2": 93.0,                            // float % or null
  "ambient_temperature": 26.5,             // °C, AMBIENT room air (DHT11)
  "ambient_humidity": 55.0,                // %
  "signal_quality": "good",                // good|fair|poor|unknown (string)
  "finger_detected": true,
  "is_simulated": false                    // test scenarios set true server-side
}
→ {"status": "stored", "reading_id": ..., "risk_category": "..."}
```
Invalid readings are stored as invalid **with notes**, never silently dropped.
Missing/invalid fields → `422` with field-level messages.

### Live data
`GET /live/{patient_id}` (latest reading + staleness) · `WS /ws/live/{patient_id}`
(session-cookie authenticated; the UI falls back to polling if WS is blocked).

### Alerts & alarm lifecycle
| Method | Path | Notes |
|---|---|---|
| GET | `/alerts?status&severity&category&patient_id&limit` | `status` includes `under_review` |
| POST | `/alerts/{id}/acknowledge` | `{note}` |
| POST | `/alerts/{id}/under-review` | `{note}` — active/acknowledged → under_review |
| POST | `/alerts/{id}/resolve` | `{note}`; already resolved → `409` |
| POST | `/alerts/{id}/reopen` | `{note}` — resolved → active |
| GET | `/alerts/{id}/history` | full audit trail of state changes |
| POST | `/stroke-symptoms` | clinician FAST entry → critical `stroke_suspect` alert |
| GET | `/stroke-symptoms/{patient_id}` | entry history |

### Analytics
`GET /analytics/summary` · `/analytics/alerts-frequency` · `/analytics/device-uptime`
· `/analytics/monitoring-summary` · `/analytics/export`

### Admin
`GET /admin/overview` · `/admin/users` · `/admin/pending-doctors` ·
`/admin/audit-logs` · `/admin/settings` · `/admin/system-health` ·
`POST /admin/users/create` · `/admin/doctors/{id}/approve|reject` ·
`POST /admin/users/{id}/deactivate|activate` · `POST /admin/settings`

### Test scenarios (demo without hardware)
`GET /test/scenarios` lists all scenarios:
`normal`, `sustained_high_hr`, `low_spo2`, `critical_combined`, `invalid_signal`,
`finger_removed`, `sensor_disconnected`, `hot_ambient`, `device_network_failure`,
`patient_reassignment`, `stroke_symptoms`.

```
POST /test/scenarios      {"scenario": "sustained_high_hr"}
POST /test/clear-simulated/{patient_id}    # remove simulated data again
```
Simulated data is always flagged `is_simulated: true` end-to-end.

### System
`GET /health` · `GET /config/public` (non-secret UI config)
