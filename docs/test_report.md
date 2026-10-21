# Test report

Date: 2026-10-21 · Platform: Windows 11, Python 3.12 (project `.venv`)

## Automated test suite

```bat
.venv\Scripts\python.exe -m pytest backend\tests -q
```

**Result: 41 passed** (≈ 8 s), 1 unrelated deprecation warning from starlette's
test client.

| File | Tests | Focus |
|---|---|---|
| `backend/tests/test_core.py` | 27 | Auth & roles, login throttle, registration, server-side patient isolation, ingest validation + device auth, risk thresholds/persistence/hysteresis, staleness, alert dedupe/ack/resolve, device assignment & rotation, simulated-data separation, settings overrides, export, **stroke flagged with zero sensor readings** |
| `backend/tests/test_alarm_workflow.py` | 14 | Per-module risk reports (sepsis/cardiac/stroke/general/technical), missing-parameter honesty, full alert lifecycle incl. `under_review` + `reopen`, resolve guard (409), post-resolution cooldown, device-health cycle (offline + stale alarms), ingest diagnostics, `stroke_symptoms` test scenario, required scenario list |

Run both with:

```bat
.venv\Scripts\python.exe -m pytest backend\tests -q
```

## Live smoke check (server on port 8080)

Executed against a running `uvicorn` instance — **all checks passed (41/41)**:

- `GET /api/v1/health` → `ok`, database `ok`
- All **15 pages** and all static assets → HTTP 200
- Unauthenticated `GET /patients` → `401`; tokenless `POST /ingest` → `401`
- Patient registration/login (`201`/`200`) and admin login
- Device register → assign → ingest (`stored`) with `X-Device-Token`
- Device list enrichment: `last_reading_at`, `data_freshness=fresh`,
  `last_ingest_status=stored`, `last_error`, `auth_status`, `patient_name`
- Device test endpoint returns connectivity + ingest diagnostics
- Test scenarios: 11 listed incl. `stroke_symptoms`; running it creates a
  critical `stroke_suspect` alert
- Alert lifecycle end-to-end: `acknowledge → under-review → resolve → reopen
  → resolve`; double-resolve → `409`
- `POST /patients/{id}/reassess` after a stroke entry → `critical`,
  `dominant_module=stroke_suspect`, `stroke_suspect.status=flagged`
- `risk-history` returns all five module reports; sepsis reports
  `insufficient_parameters` (no invented temperature/BP)
- Device-health cycle executes without error

## Not covered / honest gaps

- **ESP32 firmware was not compiled or flashed on this machine** (no Arduino
  toolchain here). The firmware and wiring guide are provided; verify on
  hardware with `esp32/wiring_guide.md`.
- **No trained ML model is shipped**. `ml/train_model.py` is an offline
  experiment; the backend uses the rule engine only (`ml/README.md`).
- WebSocket UI fallback (polling) is exercised by the pages loading, but no
  automated WS browser test is run.
