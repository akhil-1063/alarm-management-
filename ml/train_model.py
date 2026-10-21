"""Optional, offline trainer for the ICU Sentinel ML enhancement.

This script is NOT part of the running system. The backend never imports it.
It trains a small, clearly-labelled experimental classifier and writes
ml/model.json, which ml/predict.py may optionally score with.

Honesty constraints enforced by this script:
  * Labels default to WEAK labels derived from the same explainable rule
    thresholds the backend uses. A model trained on them can only imitate the
    rules — it cannot learn new clinical truth. This is printed on every run.
  * No accuracy claim is asserted. A held-out report is PRINTED as a
    diagnostic number, not as a performance claim, and it is measured against
    the same weak labels (so it only measures rule imitation).
  * Splitting is grouped by patient so no patient appears in both train and
    test (no leakage).
  * Fully deterministic (fixed seed, stable feature/label order).

Usage:
    py ml/train_model.py --csv patient_1_readings.csv [--csv more.csv ...]
                         [--label-col label] [--out ml/model.json] [--seed 42]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Feature engineering (pure stdlib — no numpy/sklearn required to *prepare*)
# ---------------------------------------------------------------------------
NUMERIC_COLS = {
    "heart_rate_bpm": "hr",
    "spo2_percent": "spo2",
    "ambient_temperature_c": "temp",
    "ambient_humidity_percent": "hum",
}

FEATURE_ORDER: list[str] = [
    "hr", "spo2", "temp", "hum",
    "hr_mean5", "hr_min5", "hr_max5", "spo2_mean5", "spo2_min5",
    "spo2_delta", "hr_delta",
    "quality_poor", "quality_fair", "quality_good",
    "is_valid", "is_simulated",
]

LABELS = ["low", "moderate", "high", "critical"]

# Same thresholds as backend/services/risk_engine.py defaults (weak labels only)
RULE_THRESHOLDS = {
    "hr_critical_low": 40.0,
    "hr_critical_high": 140.0,
    "hr_low": 50.0,
    "hr_high": 120.0,
    "spo2_critical_low": 88.0,
    "spo2_low": 94.0,
}


def rule_label(hr: float | None, spo2: float | None, valid: bool) -> str:
    """Weak label = the backend's own priority ladder (documented imitation)."""
    if not valid:
        return "low"  # invalid readings carry no risk signal for labelling
    score = 0
    if hr is not None:
        if hr <= RULE_THRESHOLDS["hr_critical_low"] or hr >= RULE_THRESHOLDS["hr_critical_high"]:
            score += 2
        if hr <= RULE_THRESHOLDS["hr_low"] or hr >= RULE_THRESHOLDS["hr_high"]:
            score += 1
    if spo2 is not None:
        if spo2 <= RULE_THRESHOLDS["spo2_critical_low"]:
            score += 2
        elif spo2 <= RULE_THRESHOLDS["spo2_low"]:
            score += 1
    if score >= 3:
        return "critical"
    if score == 2:
        return "high"
    if score == 1:
        return "moderate"
    return "low"


def _f(row: dict[str, str], col: str) -> float | None:
    raw = (row.get(col) or "").strip()
    if not raw or raw.lower() == "none":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def load_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            sys.exit(f"ERROR: CSV not found: {p}")
        with p.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                r["_group"] = p.stem  # group key = patient file (one patient per export)
                rows.append(r)
    if not rows:
        sys.exit("ERROR: no rows loaded.")
    return rows


def build_dataset(rows: list[dict], label_col: str | None) -> tuple[list[list[float]], list[str], list[str]]:
    """Returns (X, y, groups). Rows are grouped per patient file, sliding
    windows computed WITHIN each group only (no cross-patient bleed)."""
    by_group: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_group[r["_group"]].append(r)

    X: list[list[float]] = []
    y: list[str] = []
    groups: list[str] = []

    for g, grows in by_group.items():
        # chronological order within the patient
        def _ts(r: dict) -> str:
            return r.get("received_at_utc") or r.get("received_at") or ""

        grows.sort(key=_ts)
        prev_hr: float | None = None
        prev_spo2: float | None = None
        hr_hist: list[float] = []
        spo2_hist: list[float] = []
        for r in grows:
            hr = _f(r, "heart_rate_bpm")
            spo2 = _f(r, "spo2_percent")
            temp = _f(r, "ambient_temperature_c")
            hum = _f(r, "ambient_humidity_percent")
            valid = (r.get("is_valid") or "1").strip() in ("1", "true", "True")
            sim = (r.get("is_simulated") or "0").strip() in ("1", "true", "True")
            qual = (r.get("signal_quality") or "").strip().lower()

            if hr is not None:
                hr_hist.append(hr)
            if spo2 is not None:
                spo2_hist.append(spo2)
            hr_win = hr_hist[-5:]
            sp_win = spo2_hist[-5:]

            feat = [
                hr if hr is not None else 0.0,
                spo2 if spo2 is not None else 0.0,
                temp if temp is not None else 0.0,
                hum if hum is not None else 0.0,
                sum(hr_win) / len(hr_win) if hr_win else 0.0,
                min(hr_win) if hr_win else 0.0,
                max(hr_win) if hr_win else 0.0,
                sum(sp_win) / len(sp_win) if sp_win else 0.0,
                min(sp_win) if sp_win else 0.0,
                (spo2 - prev_spo2) if (spo2 is not None and prev_spo2 is not None) else 0.0,
                (hr - prev_hr) if (hr is not None and prev_hr is not None) else 0.0,
                1.0 if qual == "poor" else 0.0,
                1.0 if qual == "fair" else 0.0,
                1.0 if qual == "good" else 0.0,
                1.0 if valid else 0.0,
                1.0 if sim else 0.0,
            ]

            if label_col and (r.get(label_col) or "").strip():
                lab = r[label_col].strip().lower()
                if lab not in LABELS:
                    continue
            else:
                lab = rule_label(hr, spo2, valid)

            X.append(feat)
            y.append(lab)
            groups.append(g)
            if hr is not None:
                prev_hr = hr
            if spo2 is not None:
                prev_spo2 = spo2

    return X, y, groups


# ---------------------------------------------------------------------------
# Minimal deterministic logistic-regression classifier (multinomial, full-batch)
# Pure stdlib — deliberately dependency-free so training always works.
# ---------------------------------------------------------------------------
class SoftmaxRegressor:
    def __init__(self, n_features: int, n_classes: int, seed: int = 42):
        rng = random.Random(seed)
        self.W = [[rng.uniform(-0.01, 0.01) for _ in range(n_features)] for _ in range(n_classes)]
        self.b = [0.0] * n_classes
        self.n_classes = n_classes

    @staticmethod
    def _standardize_fit(X: list[list[float]]) -> tuple[list[float], list[float]]:
        n = len(X)
        d = len(X[0])
        mean = [sum(row[j] for row in X) / n for j in range(d)]
        var = [sum((row[j] - mean[j]) ** 2 for row in X) / max(n - 1, 1) for j in range(d)]
        std = [math.sqrt(v) if v > 1e-12 else 1.0 for v in var]
        return mean, std

    def _apply(self, X: list[list[float]], mean: list[float], std: list[float]) -> list[list[float]]:
        return [[(row[j] - mean[j]) / std[j] for j in range(len(mean))] for row in X]

    def _scores(self, Xs: list[list[float]]) -> list[list[float]]:
        out = []
        for row in Xs:
            scores = [
                self.b[c] + sum(self.W[c][j] * row[j] for j in range(len(row)))
                for c in range(self.n_classes)
            ]
            out.append(scores)
        return out

    @staticmethod
    def _softmax(scores: list[float]) -> list[float]:
        m = max(scores)
        exps = [math.exp(s - m) for s in scores]
        tot = sum(exps)
        return [e / tot for e in exps]

    def fit(self, X: list[list[float]], y: list[str], epochs: int = 400, lr: float = 0.5, l2: float = 1e-3):
        mean, std = self._standardize_fit(X)
        Xs = self._apply(X, mean, std)
        label_to_idx = {lab: i for i, lab in enumerate(LABELS)}
        y_idx = [label_to_idx[v] for v in y]
        n = len(Xs)
        d = len(Xs[0])
        for _ in range(epochs):
            gW = [[0.0] * d for _ in range(self.n_classes)]
            gb = [0.0] * self.n_classes
            scores = self._scores(Xs)
            for i in range(n):
                probs = self._softmax(scores[i])
                for c in range(self.n_classes):
                    err = probs[c] - (1.0 if y_idx[i] == c else 0.0)
                    gb[c] += err
                    row = Xs[i]
                    gw = gW[c]
                    for j in range(d):
                        gw[j] += err * row[j]
            inv = 1.0 / n
            for c in range(self.n_classes):
                self.b[c] -= lr * gb[c] * inv
                gw = gW[c]
                w = self.W[c]
                for j in range(d):
                    w[j] -= lr * (gw[j] * inv + l2 * w[j])
        self.mean_ = mean
        self.std_ = std

    def predict_proba(self, X: list[list[float]]) -> list[list[float]]:
        Xs = self._apply(X, self.mean_, self.std_)
        return [self._softmax(s) for s in self._scores(Xs)]


def group_shuffle_split(groups: list[str], seed: int, test_ratio: float = 0.25):
    uniq = sorted(set(groups))
    rng = random.Random(seed)
    rng.shuffle(uniq)
    n_test = max(1, int(round(len(uniq) * test_ratio))) if len(uniq) > 1 else 0
    test_groups = set(uniq[:n_test])
    tr = [i for i, g in enumerate(groups) if g not in test_groups]
    te = [i for i, g in enumerate(groups) if g in test_groups]
    return tr, te, sorted(test_groups)


def accuracy(probs: list[list[float]], y: list[str]) -> float:
    if not y:
        return 0.0
    pred = [LABELS[max(range(len(p)), key=lambda i: p[i])] for p in probs]
    return sum(1 for a, b in zip(pred, y) if a == b) / len(y)


def main() -> None:
    ap = argparse.ArgumentParser(description="Optional ICU Sentinel model trainer (offline, stdlib-only).")
    ap.add_argument("--csv", nargs="+", required=True, help="One or more patient reading CSV exports.")
    ap.add_argument("--label-col", default=None, help="Optional explicit label column (low|moderate|high|critical).")
    ap.add_argument("--out", default=str(ROOT / "ml" / "model.json"), help="Output model path.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--epochs", type=int, default=400)
    args = ap.parse_args()

    print("=" * 74)
    print(" ICU Sentinel — OPTIONAL experimental trainer")
    print("=" * 74)
    print(" NOTE: labels are WEAK (derived from the backend's own rule")
    print("       thresholds) unless --label-col is supplied.")
    print("       Such a model can only IMITATE the rules. It does not")
    print("       learn clinical truth and NO ACCURACY IS CLAIMED.")
    print("       Any number printed below is a diagnostic for rule")
    print("       imitation, not a performance claim.")
    print("=" * 74)

    rows = load_rows([Path(p) for p in args.csv])
    X, y, groups = build_dataset(rows, args.label_col)
    if len(X) < 8:
        sys.exit(f"ERROR: need at least 8 labelled rows, got {len(X)}.")

    patients = sorted(set(groups))
    dist = Counter(y)
    print(f"\nrows              : {len(X)}")
    print(f"patients (groups) : {len(patients)} -> {patients}")
    print(f"class balance     : {dict(dist)}")
    print(f"features          : {len(FEATURE_ORDER)} -> {FEATURE_ORDER}")
    print(f"label source      : {'explicit column ' + args.label_col if args.label_col else 'weak rule-derived labels'}")

    tr_idx, te_idx, test_groups = group_shuffle_split(groups, args.seed)
    print(f"split             : GROUPED by patient, seed={args.seed}, test groups={test_groups or '(none — single patient: in-sample diagnostic)'}")

    X_tr = [X[i] for i in tr_idx]
    y_tr = [y[i] for i in tr_idx]
    X_te = [X[i] for i in te_idx]
    y_te = [y[i] for i in te_idx]

    model = SoftmaxRegressor(n_features=len(FEATURE_ORDER), n_classes=len(LABELS), seed=args.seed)
    model.fit(X_tr, y_tr, epochs=args.epochs)

    train_acc = accuracy(model.predict_proba(X_tr), y_tr)
    test_acc = accuracy(model.predict_proba(X_te), y_te) if X_te else None

    print(f"\ntrain diagnostic (weak-label imitation): {train_acc:.3f}")
    if test_acc is not None:
        print(f"held-out diagnostic (weak-label imitation): {test_acc:.3f}")
    else:
        print("held-out diagnostic: n/a (only one patient — use more CSVs for a grouped split)")

    sklearn_ver = None
    try:
        import sklearn as _sk
        sklearn_ver = _sk.__version__
    except Exception:
        sklearn_ver = "scikit-learn not installed (not required; stdlib trainer used)"

    out = {
        "format": "icu-sentinel-logreg-v1",
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seed": args.seed,
        "feature_order": FEATURE_ORDER,
        "labels": LABELS,
        "label_source": args.label_col or "weak-rule-derived",
        "training": {
            "csv_files": [str(p) for p in args.csv],
            "rows": len(X),
            "patients": len(patients),
            "class_balance": dict(dist),
            "train_diagnostic_weak_label_accuracy": round(train_acc, 4),
            "test_diagnostic_weak_label_accuracy": round(test_acc, 4) if test_acc is not None else None,
            "disclaimer": (
                "Diagnostics above measure imitation of the rule-derived labels "
                "on THIS local test data only. They are not clinical performance "
                "figures and no accuracy is claimed."
            ),
            "sklearn_version": sklearn_ver,
        },
        "mean": model.mean_,
        "std": model.std_,
        "W": model.W,
        "b": model.b,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nmodel written to: {out_path}")
    print("Reminder: the backend does NOT use this file. It remains an offline experiment.")


if __name__ == "__main__":
    main()
