# ML (Optional Enhancement)

`ml/` contains an **optional** experimental enhancement to the ICU Sentinel
risk pipeline. It is **never required** — the backend always runs the
explainable rule engine (`backend/services/risk_engine.py`) as the source of
truth, and every dashboard, alert and WebSocket frame comes from that engine
even if no model exists here.

```
ml/
├── README.md        (this file)
├── train_model.py   optional trainer — writes ml/model.json
└── predict.py       optional CLI scorer
```

## Hard constraints of this enhancement

1. **Optional by design.** If `ml/model.json` is missing, broken or stale, the
   system behaves exactly as before. There is no code path where a missing
   model degrades live monitoring.
2. **No clinical claims.** There is no accuracy claim, no sensitivity /
   specificity claim, and no claim of regulatory or clinical validation in
   this repository or its documentation. This module is a software experiment.
3. **Labels are not outcomes.** When no explicit label column is provided,
   `train_model.py` derives *weak labels* from the same explainable rules the
   backend already uses. A model trained on such labels can only learn to
   **imitate the rules** — it cannot discover new clinical truth. The README
   and `predict.py` both state this every run.
4. **No leakage.** Splitting is **grouped by patient** (`GroupShuffleSplit`),
   so a patient's readings never appear in both train and test. This is the
   minimum credible protocol for time-series patient data.
5. **Deterministic.** Fixed `random_state`, fixed column order, stable JSON
   output — retraining on the same data with the same seed reproduces the same
   model byte-for-byte (modulo sklearn version, which is recorded in the file).
6. **No bundled dataset.** No patient data ships with this repository. You
   must generate your own CSVs (test-scenario data or your own exports).

## Workflow

### 1. Export CSVs

Export reading CSVs from the dashboard (Patient → Export CSV) or use the API:

```
GET /api/v1/patients/{id}/export        (your own / assigned patients only)
```

Columns produced by the backend:

```
reading_id, received_at_utc, device_id, heart_rate_bpm, spo2_percent,
ambient_temperature_c, ambient_humidity_percent, signal_quality, is_valid,
validity_notes, is_simulated, firmware_version
```

> Real ML work on real outcomes requires labelled clinical data with proper
> ethics approval — none of that is in scope for this prototype. The CSVs you
> feed `train_model.py` are local test data only.

### 2. Train (optional)

```
py ml/train_model.py --csv patient_1_readings.csv --out ml/model.json
```

Multiple files / groups:

```
py ml/train_model.py --csv patient_1_readings.csv patient_2_readings.csv --out ml/model.json
```

With an explicit label column (e.g. `label` containing `low|moderate|high|critical`):

```
py ml/train_model.py --csv labelled.csv --label-col label
```

Dependency: `pip install scikit-learn` (graceful error otherwise).

The script prints data-preparation details (rows, patients, class balance,
split method) so nothing is a black box, and **explicitly states** that a
rule-derived weak label cannot be better than the rules it came from.

### 3. Score (optional)

```
py ml/predict.py --hr 118 --spo2 91
```

- With `ml/model.json` present → prints the model probability + the rule-engine
  category side by side.
- Without a model → prints an explicit *"model unavailable — falling back to
  rule engine"* message. Never errors.

## What is NOT here

- No accuracy / AUC / F1 numbers are asserted anywhere (nothing was validated).
- No pretrained weights, no dataset, no download scripts.
- No endpoint on the backend reads `model.json` — wiring a validated model into
  the live risk pipeline is deliberate future work requiring clinical review,
  not a checkbox.
- No `requirements.txt` change: scikit-learn is opt-in for this folder only.
