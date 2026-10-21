"""Optional CLI scorer for the ICU Sentinel ML enhancement (offline only).

Prints the experimental model's class probabilities alongside the rule-engine
category. If ml/model.json is missing or unreadable it explicitly reports that
the model is unavailable and that the rule engine remains the source of truth —
it never fails hard, because the live system never depends on this file.

Usage:
    py ml/predict.py --hr 118 --spo2 91 [--temp 24.1 --hum 55]
                     [--quality good] [--model ml/model.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ROOT / "ml" / "model.json"

# Mirrors backend/services/risk_engine.py default thresholds (for the side-by-side)
def rule_category(hr: float | None, spo2: float | None) -> str:
    score = 0
    if hr is not None:
        if hr <= 40 or hr >= 140:
            score += 2
        if hr <= 50 or hr >= 120:
            score += 1
    if spo2 is not None:
        if spo2 <= 88:
            score += 2
        elif spo2 <= 94:
            score += 1
    return "critical" if score >= 3 else "high" if score == 2 else "moderate" if score == 1 else "low"


def softmax(scores: list[float]) -> list[float]:
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    tot = sum(exps)
    return [e / tot for e in exps]


def main() -> None:
    ap = argparse.ArgumentParser(description="Optional experimental scorer (fallback = rule engine).")
    ap.add_argument("--hr", type=float, default=None, help="Heart rate (bpm).")
    ap.add_argument("--spo2", type=float, default=None, help="SpO2 (percent).")
    ap.add_argument("--temp", type=float, default=0.0, help="Ambient temperature (C).")
    ap.add_argument("--hum", type=float, default=0.0, help="Ambient humidity (%%).")
    ap.add_argument("--quality", default="good", choices=["good", "fair", "poor", "no_finger", "invalid"])
    ap.add_argument("--valid", default="1", choices=["0", "1"])
    ap.add_argument("--simulated", default="0", choices=["0", "1"])
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    args = ap.parse_args()

    cat = rule_category(args.hr, args.spo2)
    print(f"rule-engine category : {cat}   (source of truth — always available)")

    model_path = Path(args.model)
    if not model_path.exists():
        print(f"model unavailable     : {model_path} not found")
        print("=> falling back to the rule engine only. This is expected and safe:")
        print("   the live backend never requires this model file.")
        return

    try:
        m = json.loads(model_path.read_text(encoding="utf-8"))
        assert m.get("format") == "icu-sentinel-logreg-v1"
        assert m.get("feature_order") and m.get("W") and m.get("mean")
    except Exception as exc:  # corrupt / incompatible file -> graceful fallback
        print(f"model unavailable     : unreadable ({exc.__class__.__name__}: {exc})")
        print("=> falling back to the rule engine only. Safe by design.")
        return

    hr = args.hr if args.hr is not None else 0.0
    spo2 = args.spo2 if args.spo2 is not None else 0.0
    q = args.quality
    feats = {
        "hr": hr, "spo2": spo2, "temp": args.temp, "hum": args.hum,
        "hr_mean5": hr, "hr_min5": hr, "hr_max5": hr,
        "spo2_mean5": spo2, "spo2_min5": spo2,
        "spo2_delta": 0.0, "hr_delta": 0.0,
        "quality_poor": 1.0 if q == "poor" else 0.0,
        "quality_fair": 1.0 if q == "fair" else 0.0,
        "quality_good": 1.0 if q == "good" else 0.0,
        "is_valid": float(args.valid),
        "is_simulated": float(args.simulated),
    }
    x = [feats[k] for k in m["feature_order"]]
    mean, std, W, b = m["mean"], m["std"], m["W"], m["b"]
    xs = [(x[j] - mean[j]) / (std[j] if std[j] else 1.0) for j in range(len(x))]

    scores = [b[c] + sum(W[c][j] * xs[j] for j in range(len(xs))) for c in range(len(b))]
    probs = softmax(scores)
    labels = m.get("labels", ["low", "moderate", "high", "critical"])
    best = labels[max(range(len(probs)), key=lambda i: probs[i])]

    print(f"model class          : {best}")
    print("model probabilities  : " + ", ".join(f"{lab}={p:.3f}" for lab, p in zip(labels, probs)))
    print()
    print("DISCLAIMER: this model was trained on WEAK labels derived from the")
    print("same rule thresholds shown above, on local test data only. No clinical")
    print("accuracy is claimed; it does not replace the rule engine or clinical")
    print("judgement. The backend does not read this model at runtime.")

    if best != cat:
        print(f"\nnote: model ({best}) disagrees with rule engine ({cat}) — the rule engine wins.")


if __name__ == "__main__":
    main()
