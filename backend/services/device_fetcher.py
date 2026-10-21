"""Fetch a raw JSON snapshot directly from an ESP8266's ``GET /data`` endpoint.

Why this lives on the server
----------------------------
The dashboard (a web page) is often opened over HTTPS while the ESP8266 serves
plain HTTP, and the firmware does not send CORS headers. Browsers therefore
block a direct ``fetch('http://<esp>/data')``. To keep the "user types the device
IP, no hardcoded IP" workflow working, the browser asks THIS server to fetch the
device and return the parsed JSON. The server is bound to a private-network
allowlist so it cannot be abused as an open SSRF proxy.

This module performs the network call only. Mapping the raw device JSON into the
platform's reading fields is done by ``device_data_mapper``.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from backend.config import settings

DEFAULT_PORT = 80


@dataclass
class FetchResult:
    ok: bool
    status: str                     # ok | blocked | disabled | timeout | unreachable | http_error | invalid_json
    endpoint: str | None = None
    http_status: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    data: dict | None = None
    notes: list[str] = field(default_factory=list)


def normalize_endpoint(raw: str | None) -> tuple[str | None, int | None, str | None]:
    """Accept ``192.168.4.1``, ``192.168.4.1:80`` or ``http://192.168.4.1/``.

    Returns ``(host, port, error)``. Only the host and port are honoured; any
    path/scheme in the user input is ignored because the device path is fixed
    at ``/data``.
    """
    if not raw:
        return None, None, "No endpoint provided"
    value = raw.strip()
    if not value:
        return None, None, "No endpoint provided"
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname
        port = parsed.port
    else:
        parsed = urlparse("//" + value)
        host = parsed.hostname
        port = parsed.port
    if not host:
        return None, None, "Could not parse a host from the endpoint"
    return host, port or DEFAULT_PORT, None


def _is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A hostname: allow only if it is a bare name with no dots (e.g. an mDNS
        # style name is NOT allowed) — we require an explicit IP for safety.
        return False
    if ip.is_loopback:
        return settings.esp_fetch_allow_localhost
    return ip.is_private or ip.is_link_local


def _allowed_by_allowlist(host: str) -> bool:
    prefixes = [p.strip() for p in settings.esp_fetch_host_allowlist.split(",") if p.strip()]
    return any(host.startswith(p) for p in prefixes)


def check_endpoint_allowed(host: str) -> str | None:
    """Return an error string if the host may not be fetched, else None."""
    if not _is_private(host):
        return "Host is not a private/local network address; direct fetch is restricted to the local network."
    if not _allowed_by_allowlist(host):
        return "Host is not within the configured local-network allowlist."
    return None


def fetch_device_data(endpoint: str | None, *, path: str = "/data") -> FetchResult:
    """GET ``http://<endpoint><path>`` and return the parsed JSON object."""
    if not settings.esp_fetch_enabled:
        return FetchResult(ok=False, status="disabled", endpoint=endpoint,
                           error="Direct device fetch is disabled on the server.")

    host, port, err = normalize_endpoint(endpoint)
    if err:
        return FetchResult(ok=False, status="blocked", endpoint=endpoint, error=err)

    blocked = check_endpoint_allowed(host)
    if blocked:
        return FetchResult(ok=False, status="blocked", endpoint=f"{host}:{port}", error=blocked)

    url = f"http://{host}:{port}{path}"
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=settings.esp_fetch_timeout_seconds) as client:
            resp = client.get(url)
    except httpx.TimeoutException:
        return FetchResult(ok=False, status="timeout", endpoint=f"{host}:{port}",
                           error=f"Device did not respond within {settings.esp_fetch_timeout_seconds}s.")
    except httpx.HTTPError as exc:
        return FetchResult(ok=False, status="unreachable", endpoint=f"{host}:{port}",
                           error=f"Could not reach the device: {exc.__class__.__name__}.")
    except Exception as exc:  # defensive: never let a fetch crash the request
        return FetchResult(ok=False, status="unreachable", endpoint=f"{host}:{port}",
                           error=f"Unexpected fetch error: {exc.__class__.__name__}.")

    latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    if resp.status_code != 200:
        return FetchResult(ok=False, status="http_error", endpoint=f"{host}:{port}",
                           http_status=resp.status_code, latency_ms=latency_ms,
                           error=f"Device returned HTTP {resp.status_code}.")
    try:
        payload = resp.json()
    except Exception:
        return FetchResult(ok=False, status="invalid_json", endpoint=f"{host}:{port}",
                           http_status=resp.status_code, latency_ms=latency_ms,
                           error="Device did not return valid JSON.")
    if not isinstance(payload, dict):
        return FetchResult(ok=False, status="invalid_json", endpoint=f"{host}:{port}",
                           http_status=resp.status_code, latency_ms=latency_ms,
                           error="Device JSON was not an object.")

    return FetchResult(ok=True, status="ok", endpoint=f"{host}:{port}",
                       http_status=resp.status_code, latency_ms=latency_ms, data=payload)
