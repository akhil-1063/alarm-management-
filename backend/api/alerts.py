"""Alert listing, acknowledgement, resolution and stroke symptom entry."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user, require_admin_or_doctor
from backend.models import Alert, AlertAcknowledgement, StrokeSymptomEntry, User, utcnow
from backend.schemas import AlertActionRequest, StrokeSymptomCreate, StrokeSymptomOut
from backend.api.shared import ensure_patient_visible, visible_patient_ids
from backend.security import audit
from backend.services import alert_manager

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
def list_alerts(
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = None,
    category: str | None = None,
    patient_id: int | None = None,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(Alert)
    ids = visible_patient_ids(db, user)
    if ids is not None:
        if not ids:
            return {"alerts": []}
        q = q.where(Alert.patient_user_id.in_(ids))
    if status_filter:
        q = q.where(Alert.status == status_filter)
    if severity:
        q = q.where(Alert.severity == severity)
    if category:
        q = q.where(Alert.category == category)
    if patient_id:
        ensure_patient_visible(db, user, patient_id)
        q = q.where(Alert.patient_user_id == patient_id)
    rows = db.execute(q.order_by(Alert.created_at.desc()).limit(limit)).scalars().all()
    out = []
    for a in rows:
        patient = db.get(User, a.patient_user_id)
        out.append({
            "id": a.id,
            "patient_user_id": a.patient_user_id,
            "patient_name": patient.full_name if patient else None,
            "assigned_doctor_user_id": a.assigned_doctor_user_id,
            "category": a.category,
            "severity": a.severity,
            "title": a.title,
            "message": a.message,
            "evidence": a.evidence,
            "status": a.status,
            "created_at": a.created_at,
            "acknowledged_at": a.acknowledged_at,
            "acknowledged_by": a.acknowledged_by,
            "resolved_at": a.resolved_at,
            "resolved_by": a.resolved_by,
            "resolution_notes": a.resolution_notes,
            "is_simulated": a.is_simulated,
        })
    return {"alerts": out}


def _load_alert_for_actor(db: Session, user: User, alert_id: int) -> Alert:
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    ensure_patient_visible(db, user, alert.patient_user_id)
    return alert


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge(
    alert_id: int,
    payload: AlertActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    alert = _load_alert_for_actor(db, user, alert_id)
    if alert.status == "resolved":
        raise HTTPException(status_code=409, detail="Alert is already resolved")
    alert = alert_manager.acknowledge_alert(db, alert, user.id, payload.note)
    audit(db, "alert_acknowledged", actor_user_id=user.id, actor_label=user.email,
          entity_type="alert", entity_id=alert.id, detail=payload.note,
          ip_address=request.client.host if request.client else None)
    return {"status": alert.status, "alert_id": alert.id, "acknowledged_at": alert.acknowledged_at}


@router.post("/alerts/{alert_id}/under-review")
def under_review(
    alert_id: int,
    payload: AlertActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    """Lifecycle step: acknowledged alert is now being clinically reviewed."""
    alert = _load_alert_for_actor(db, user, alert_id)
    if alert.status == "resolved":
        raise HTTPException(status_code=409, detail="Alert is already resolved")
    alert = alert_manager.start_under_review(db, alert, user.id, payload.note)
    audit(db, "alert_under_review", actor_user_id=user.id, actor_label=user.email,
          entity_type="alert", entity_id=alert.id, detail=payload.note,
          ip_address=request.client.host if request.client else None)
    return {"status": alert.status, "alert_id": alert.id}


@router.post("/alerts/{alert_id}/reopen")
def reopen(
    alert_id: int,
    payload: AlertActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    """Reopen a resolved alert (condition recurred / resolution was premature)."""
    alert = _load_alert_for_actor(db, user, alert_id)
    if alert.status != "resolved":
        raise HTTPException(status_code=409, detail="Only resolved alerts can be reopened")
    alert = alert_manager.reopen_alert(db, alert, user.id, payload.note)
    audit(db, "alert_reopened", actor_user_id=user.id, actor_label=user.email,
          entity_type="alert", entity_id=alert.id, detail=payload.note,
          ip_address=request.client.host if request.client else None)
    return {"status": alert.status, "alert_id": alert.id}


@router.post("/alerts/{alert_id}/resolve")
def resolve(
    alert_id: int,
    payload: AlertActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    alert = _load_alert_for_actor(db, user, alert_id)
    if alert.status == "resolved":
        raise HTTPException(status_code=409, detail="Alert is already resolved")
    alert = alert_manager.resolve_alert(db, alert, user.id, payload.note)
    audit(db, "alert_resolved", actor_user_id=user.id, actor_label=user.email,
          entity_type="alert", entity_id=alert.id, detail=payload.note,
          ip_address=request.client.host if request.client else None)
    return {"status": alert.status, "alert_id": alert.id, "resolved_at": alert.resolved_at}


@router.get("/alerts/{alert_id}/history")
def alert_history(alert_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    alert = _load_alert_for_actor(db, user, alert_id)
    rows = db.execute(
        select(AlertAcknowledgement).where(AlertAcknowledgement.alert_id == alert_id)
        .order_by(AlertAcknowledgement.created_at)
    ).scalars().all()
    return {
        "alert_id": alert_id,
        "history": [
            {
                "action": r.action,
                "note": r.note,
                "user_id": r.user_id,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.post("/stroke-symptoms", response_model=StrokeSymptomOut, status_code=201)
def record_stroke_symptoms(
    payload: StrokeSymptomCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin_or_doctor),
):
    """Clinician-entered FAST-style stroke indicators.

    Raises an urgent, critical alert immediately when any indicator is set.
    The sensor system is never used to confirm or rule out stroke.
    """
    ensure_patient_visible(db, user, payload.patient_user_id)
    if not any([payload.facial_asymmetry, payload.one_sided_weakness,
                payload.speech_difficulty, payload.sudden_neuro_change]):
        raise HTTPException(status_code=400, detail="At least one stroke indicator must be selected")

    entry = StrokeSymptomEntry(
        patient_user_id=payload.patient_user_id,
        entered_by=user.id,
        facial_asymmetry=payload.facial_asymmetry,
        one_sided_weakness=payload.one_sided_weakness,
        speech_difficulty=payload.speech_difficulty,
        sudden_neuro_change=payload.sudden_neuro_change,
        free_text=payload.free_text,
        is_simulated=payload.is_simulated,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    flags = []
    if payload.facial_asymmetry:
        flags.append("facial asymmetry")
    if payload.one_sided_weakness:
        flags.append("one-sided arm/leg weakness")
    if payload.speech_difficulty:
        flags.append("speech difficulty")
    if payload.sudden_neuro_change:
        flags.append("sudden neurological change")

    alert_manager.raise_alert(
        db,
        patient_user_id=payload.patient_user_id,
        category="stroke_suspect",
        severity="critical",
        title="URGENT: possible stroke symptoms entered by clinician",
        message=(
            "A clinician has entered indicators consistent with possible stroke: "
            + ", ".join(flags)
            + ". Immediate clinical assessment is required. This warning was generated from the "
            "clinician's structured symptom entry — NOT from the ESP32 sensors, which cannot "
            "detect stroke. Call emergency services per your institution's protocol."
        ),
        evidence={"flags": flags, "free_text": payload.free_text, "entered_by": user.id},
        dedupe_key=f"stroke_suspect:{payload.patient_user_id}",
        is_simulated=payload.is_simulated,
    )
    audit(db, "stroke_symptoms_recorded", actor_user_id=user.id, actor_label=user.email,
          entity_type="stroke_symptom", entity_id=entry.id, detail=", ".join(flags),
          ip_address=request.client.host if request.client else None)
    return entry


@router.get("/stroke-symptoms/{patient_id}", response_model=list[StrokeSymptomOut])
def list_stroke_symptoms(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ensure_patient_visible(db, user, patient_id)
    rows = db.execute(
        select(StrokeSymptomEntry)
        .where(StrokeSymptomEntry.patient_user_id == patient_id)
        .order_by(StrokeSymptomEntry.created_at.desc())
        .limit(50)
    ).scalars().all()
    return rows
