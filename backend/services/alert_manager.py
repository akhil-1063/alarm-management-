"""Alert creation, deduplication, acknowledgement, review and resolution.

Lifecycle:  active → acknowledged → under_review → resolved
            (resolved alerts can be reopened manually, or a NEW alert is
             created when the same condition re-occurs after the cooldown)

Keeps device-health (technical) alerts separate from patient-health alerts,
uses dedupe keys + hysteresis to avoid alert storms, applies a configurable
post-resolution cooldown, and preserves full history of every state change.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config import settings
from backend.models import Alert, AlertAcknowledgement, DoctorPatientAssignment, utcnow
from backend.services.risk_engine import RiskResult

SEVERITY_RANK = {"low": 0, "moderate": 1, "high": 2, "critical": 3}


def _assigned_doctor(db: Session, patient_user_id: int) -> int | None:
    row = db.execute(
        select(DoctorPatientAssignment.doctor_user_id).where(
            DoctorPatientAssignment.patient_user_id == patient_user_id,
            DoctorPatientAssignment.is_active.is_(True),
        )
    ).first()
    return row[0] if row else None


def raise_alert(
    db: Session,
    *,
    patient_user_id: int,
    category: str,
    severity: str,
    title: str,
    message: str,
    evidence: dict | None = None,
    dedupe_key: str | None = None,
    is_simulated: bool = False,
) -> Alert | None:
    """Create an alert unless an equivalent active alert already exists.

    Deduplication: if an alert with the same dedupe_key is already active,
    acknowledged or under review, do not create a duplicate — but upgrade the
    existing alert in place if the new severity is higher (never silently
    suppress a worsening critical event). After resolution, the same dedupe
    key is suppressed for `alert_cooldown_minutes` so a single recovery does
    not immediately re-alarm; after the cooldown a new event opens a fresh
    alert (the resolved history is never overwritten).
    """
    if dedupe_key:
        existing = db.execute(
            select(Alert).where(
                Alert.dedupe_key == dedupe_key,
                Alert.patient_user_id == patient_user_id,
                Alert.status.in_(["active", "acknowledged", "under_review"]),
            )
        ).scalars().first()
        if existing:
            if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(existing.severity, 0):
                _log_ack(db, existing.id, existing.assigned_doctor_user_id, "escalated",
                         f"Severity upgraded {existing.severity} -> {severity}: {message}")
                existing.severity = severity
                existing.category = category
                existing.title = title
                existing.message = message
                existing.evidence = json.dumps(evidence or {})
                existing.created_at = utcnow()  # reset clock for escalation visibility
                existing.status = "active"
                existing.acknowledged_at = None
                existing.acknowledged_by = None
                db.add(existing)
                db.commit()
                db.refresh(existing)
            elif SEVERITY_RANK.get(severity, 0) == SEVERITY_RANK.get(existing.severity, 0):
                # Same priority, more specific module (e.g. general -> cardiac):
                # refine the details in place without duplicating or resetting
                # an already-acknowledged alert.
                if existing.category != category:
                    _log_ack(db, existing.id, None, "category_refined",
                             f"Category refined {existing.category} -> {category}")
                    existing.category = category
                    existing.title = title
                    existing.message = message
                    existing.evidence = json.dumps(evidence or {})
                    db.add(existing)
                    db.commit()
                    db.refresh(existing)
            return existing

        # Post-resolution cooldown: identical resolved events are suppressed
        # for a configurable window (worsening severity still opens a new alert).
        cooldown_min = int(
            _setting_float(db, "alert_cooldown_minutes", settings.alert_cooldown_minutes)
        )
        if cooldown_min > 0:
            cutoff = utcnow() - timedelta(minutes=cooldown_min)
            recently_resolved = db.execute(
                select(Alert).where(
                    Alert.dedupe_key == dedupe_key,
                    Alert.patient_user_id == patient_user_id,
                    Alert.status == "resolved",
                    Alert.resolved_at.is_not(None),
                    Alert.resolved_at >= cutoff,
                ).order_by(Alert.resolved_at.desc())
            ).scalars().first()
            if recently_resolved and SEVERITY_RANK.get(severity, 0) <= SEVERITY_RANK.get(recently_resolved.severity, 0):
                _log_ack(db, recently_resolved.id, None, "suppressed",
                         f"Cooldown ({cooldown_min} min): identical {severity} event suppressed after resolution.")
                db.commit()
                return None

    alert = Alert(
        patient_user_id=patient_user_id,
        assigned_doctor_user_id=_assigned_doctor(db, patient_user_id),
        category=category,
        severity=severity,
        title=title,
        message=message,
        evidence=json.dumps(evidence or {}),
        status="active",
        dedupe_key=dedupe_key,
        is_simulated=is_simulated,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def _setting_float(db: Session, key: str, default: float) -> float:
    from backend.models import ApplicationSetting

    row = db.get(ApplicationSetting, key)
    if row is None:
        return float(default)
    try:
        return float(row.value)
    except (TypeError, ValueError):
        return float(default)


def _log_ack(db: Session, alert_id: int, user_id: int | None, action: str, note: str | None) -> None:
    db.add(AlertAcknowledgement(alert_id=alert_id, user_id=user_id, action=action, note=note))


def acknowledge_alert(db: Session, alert: Alert, user_id: int, note: str | None) -> Alert:
    if alert.status == "resolved":
        raise ValueError("Alert is already resolved")
    alert.status = "acknowledged"
    alert.acknowledged_at = alert.acknowledged_at or utcnow()
    alert.acknowledged_by = user_id
    _log_ack(db, alert.id, user_id, "acknowledged", note)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def start_under_review(db: Session, alert: Alert, user_id: int, note: str | None) -> Alert:
    """Move an active/acknowledged alert into the clinical review phase."""
    if alert.status == "resolved":
        raise ValueError("Alert is already resolved")
    alert.status = "under_review"
    if alert.acknowledged_at is None:
        alert.acknowledged_at = utcnow()
        alert.acknowledged_by = user_id
    _log_ack(db, alert.id, user_id, "under_review", note)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def resolve_alert(db: Session, alert: Alert, user_id: int, note: str | None) -> Alert:
    alert.status = "resolved"
    alert.resolved_at = utcnow()
    alert.resolved_by = user_id
    if alert.acknowledged_at is None:
        alert.acknowledged_at = alert.resolved_at
        alert.acknowledged_by = user_id
    _log_ack(db, alert.id, user_id, "resolved", note)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def reopen_alert(db: Session, alert: Alert, user_id: int, note: str | None) -> Alert:
    """Explicitly reopen a resolved alert (e.g. the condition recurred)."""
    if alert.status != "resolved":
        raise ValueError("Only resolved alerts can be reopened")
    alert.status = "active"
    alert.resolved_at = None
    alert.resolved_by = None
    alert.acknowledged_at = None
    alert.acknowledged_by = None
    _log_ack(db, alert.id, user_id, "reopened", note)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def evaluate_and_open_alerts(
    db: Session,
    patient_user_id: int,
    result: RiskResult,
    *,
    is_simulated: bool = False,
) -> list[Alert]:
    """Translate a RiskResult's per-module reports into concrete alerts.

    Categories are chosen from the module that produced the flag, so technical
    faults are never labelled as disease and clinician stroke entries are
    never attributed to the sensors.
    """
    opened: list[Alert] = []
    if result.category == "low":
        return opened

    modules = result.modules or {}
    text = " ".join(result.reasons)
    severity = result.category

    def _sev(module: dict, cap: str | None = None) -> str:
        s = module.get("severity") or severity
        if cap and SEVERITY_RANK.get(s, 0) > SEVERITY_RANK.get(cap, 0):
            return cap
        return s

    # 1) Clinician-entered stroke indicators — highest priority, always critical.
    stroke = modules.get("stroke_suspect", {})
    if stroke.get("status") == "flagged":
        opened.append(raise_alert(
            db,
            patient_user_id=patient_user_id,
            category="stroke_suspect",
            severity="critical",
            title="Urgent: possible stroke indicators entered by clinician",
            message=stroke.get("message") or text,
            evidence={**(stroke.get("evidence") or {}), "modules": modules},
            dedupe_key=f"stroke_suspect:{patient_user_id}",
            is_simulated=is_simulated,
        ))

    # 2) Technical faults — category "device", never a disease label.
    technical = modules.get("technical", {})
    if technical.get("status") not in (None, "ok"):
        opened.append(raise_alert(
            db,
            patient_user_id=patient_user_id,
            category="device",
            severity=_sev(technical, cap="high"),
            title=f"Technical alarm: {technical.get('status', 'sensor fault').replace('_', ' ')}",
            message=technical.get("message") or "Sensor/device data fault.",
            evidence={**(technical.get("evidence") or {}), "modules": modules},
            dedupe_key=f"data_quality:{patient_user_id}",
            is_simulated=is_simulated,
        ))

    # 3) Possible cardiac concern — category "cardiac".
    cardiac = modules.get("possible_cardiac", {})
    if cardiac.get("status") == "possible_risk_flag":
        opened.append(raise_alert(
            db,
            patient_user_id=patient_user_id,
            category="cardiac",
            severity=_sev(cardiac),
            title="Possible cardiac concern — clinical review required",
            message=cardiac.get("message") or text,
            evidence={**(cardiac.get("evidence") or {}), "modules": modules},
            # Same dedupe key as the general vitals alert: one ongoing
            # abnormal-vitals condition stays ONE alert that escalates in place.
            dedupe_key=f"vitals:{patient_user_id}",
            is_simulated=is_simulated,
        ))

    # 4) Possible infection-related deterioration — category "infection".
    sepsis = modules.get("possible_sepsis", {})
    if sepsis.get("status") == "possible_risk_flag":
        opened.append(raise_alert(
            db,
            patient_user_id=patient_user_id,
            category="infection",
            severity=_sev(sepsis),
            title="Possible infection-related deterioration — review parameters",
            message=sepsis.get("message") or text,
            evidence={**(sepsis.get("evidence") or {}), "modules": modules},
            dedupe_key=f"infection:{patient_user_id}",
            is_simulated=is_simulated,
        ))

    # 5) General measured abnormalities — category "general".
    #    Only when no more specific clinical module already raised a flag,
    #    so one condition never produces two duplicate vitals alerts.
    general = modules.get("general_abnormalities", {})
    specific_flagged = cardiac.get("status") == "possible_risk_flag" or sepsis.get("status") == "possible_risk_flag"
    if general.get("status") == "flagged" and not specific_flagged:
        opened.append(raise_alert(
            db,
            patient_user_id=patient_user_id,
            category="general",
            severity=_sev(general),
            title="Abnormal vital-sign pattern",
            message=(general.get("message") + " " + text).strip(),
            evidence={**(general.get("evidence") or {}), "modules": modules},
            dedupe_key=f"vitals:{patient_user_id}",
            is_simulated=is_simulated,
        ))

    # Environmental context notice (never a clinical alarm by itself).
    if "ambient" in text.lower() and severity == "moderate" and not (
        cardiac.get("status") == "possible_risk_flag" or general.get("status") == "flagged"
    ):
        opened.append(raise_alert(
            db,
            patient_user_id=patient_user_id,
            category="general",
            severity="low",
            title="Environmental context notice",
            message=text,
            evidence={"modules": modules},
            dedupe_key=f"ambient:{patient_user_id}",
            is_simulated=is_simulated,
        ))

    return [a for a in opened if a]


def escalate_unacknowledged_critical(db: Session) -> list[Alert]:
    """Re-flag critical alerts that have not been acknowledged for too long.

    Returns them so the caller can broadcast an escalation; the alert itself
    remains visible until acknowledged/resolved (never silently suppressed).
    """
    minutes = int(_setting_float(db, "critical_escalation_minutes", settings.critical_escalation_minutes))
    cutoff = utcnow() - timedelta(minutes=minutes)
    rows = db.execute(
        select(Alert).where(
            Alert.severity == "critical",
            Alert.status == "active",
            Alert.created_at <= cutoff,
        )
    ).scalars().all()
    for alert in rows:
        _log_ack(db, alert.id, None, "escalated",
                 f"Critical alert unacknowledged for more than {minutes} minutes")
        alert.created_at = utcnow()  # keep at top of timeline
        db.add(alert)
    db.commit()
    return rows


def stale_active_alerts_for_patient(db: Session, patient_user_id: int) -> list[Alert]:
    rows = db.execute(
        select(Alert).where(
            Alert.patient_user_id == patient_user_id,
            Alert.status.in_(["active", "acknowledged", "under_review"]),
        )
    ).scalars().all()
    return list(rows)


def alerts_active_since(db: Session, since: datetime) -> list[Alert]:
    return list(db.execute(
        select(Alert).where(Alert.created_at >= since).order_by(Alert.created_at.desc())
    ).scalars().all())
