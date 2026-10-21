"""Device status helpers: online/offline determination and freshness."""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.config import settings
from backend.models import Device, utcnow


def is_device_online(device: Device, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if not device.is_active or device.last_seen_at is None:
        return False
    return (now - device.last_seen_at) <= timedelta(seconds=settings.device_offline_seconds)


def mark_seen(device: Device, ip_address: str | None = None, firmware_version: str | None = None) -> None:
    device.last_seen_at = utcnow()
    if ip_address:
        device.ip_address = ip_address
    if firmware_version:
        device.firmware_version = firmware_version
