"""Shipment tracking: the carrier API first, the browser fallback second.

Practices in this file:
  * Retry with backoff (retry.py) on HTTP 429/5xx and timeouts.
  * A circuit breaker: after 3 failures in a row, stop calling the API for 30 s.
  * Validate the API answer against a strict schema (schemas.TrackingInfo).
  * Browser fallback (portal_reader.py, in the sandbox) when there is no API,
    or when the API is down.
  * Network allowlist: only hosts in ALLOWED_HOSTS.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from .config import Settings, today
from .retry import Attempt, CircuitBreaker, CircuitOpen, PermanentError, RetryPolicy, TransientError, call_with_retry
from .sandbox import Limits, SandboxError, run_module
from .schemas import TrackingInfo

API_RETRY = RetryPolicy(max_attempts=4, base_delay=0.4, factor=2.0, max_delay=4.0)


def carrier_of(tracking_no: str) -> str:
    if tracking_no.startswith("SS-"):
        return "swiftship"
    if tracking_no.startswith("PC-"):
        return "parcelo"
    raise PermanentError(f"unknown carrier for tracking number {tracking_no}")


def _days_late(status: str, eta: dt.date | None) -> int:
    if status == "delivered" or eta is None:
        return 0
    return max(0, (today() - eta).days)


class CarrierClient:
    def __init__(self, settings: Settings, on_retry: Callable[[Attempt], None] | None = None):
        self.s = settings
        self.on_retry = on_retry
        self.breaker = CircuitBreaker("swiftship-api", failure_threshold=3, cooldown_s=30)

    # -- network guard ------------------------------------------------------------
    def _allowed(self, url: str) -> str:
        host = urlparse(url).hostname or ""
        if host not in self.s.allowed_hosts:
            raise PermanentError(f"blocked: host {host!r} is not in ALLOWED_HOSTS")
        return url

    # -- the API ------------------------------------------------------------------
    def _api_once(self, tracking_no: str) -> dict:
        url = self._allowed(f"{self.s.carrier_base_url}/swiftship/api/v1/track/{tracking_no}")
        try:
            r = httpx.get(url, timeout=5.0)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"network: {type(exc).__name__}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After")
            raise TransientError(f"HTTP {r.status_code}", retry_after=float(ra) if ra and ra.isdigit() else None)
        if r.status_code == 404:
            raise PermanentError(f"tracking number {tracking_no} not found")
        if r.status_code != 200:
            raise PermanentError(f"HTTP {r.status_code}")
        return r.json()

    def _from_api(self, data: dict) -> TrackingInfo:
        try:
            eta = dt.date.fromisoformat(data["eta"]) if data.get("eta") else None
            info = TrackingInfo(
                tracking_no=data["tracking_number"], carrier="swiftship", status=data["status"], eta=eta,
                days_late=_days_late(data["status"], eta),
                events=[{"on": e["date"], "text": e["description"]} for e in data.get("events", [])],
                source="api")
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            # The schema stops bad data here. The model gets a clear error, not garbage.
            raise PermanentError(f"carrier API returned invalid data: {str(exc).splitlines()[0][:200]}") from exc
        return info

    # -- the browser fallback -----------------------------------------------------
    def _from_browser(self, carrier: str, tracking_no: str) -> TrackingInfo:
        portal = self._allowed(f"{self.s.carrier_base_url}/{carrier}/")
        engine = self.s.browser
        try:
            out = run_module("refund_desk.portal_reader",
                             {"portal_url": portal, "tracking_no": tracking_no, "engine": engine,
                              "allowed_hosts": list(self.s.allowed_hosts)},
                             Limits(), apply_memory=(engine == "http"))
        except SandboxError as exc:
            raise TransientError(f"browser sandbox: {exc}") from exc
        if not out.get("ok"):
            raise PermanentError(f"browser could not read the page: {out.get('error')}")
        f = out["fields"]
        status = f.get("status") or "unknown"
        if status not in ("in_transit", "delivered", "delayed", "lost", "unknown"):
            status = "unknown"
        try:
            eta = dt.date.fromisoformat(f["eta"]) if f.get("eta") else None
            return TrackingInfo(tracking_no=tracking_no, carrier=carrier, status=status, eta=eta,
                                days_late=_days_late(status, eta),
                                events=[{"on": e["on"], "text": e["text"][:300]} for e in f.get("events", [])],
                                source="browser", page_note=(f.get("note") or None) and f["note"][:1000])
        except (ValueError, ValidationError) as exc:
            raise PermanentError(f"tracking page had invalid data: {str(exc).splitlines()[0][:200]}") from exc

    # -- public -------------------------------------------------------------------
    def track(self, tracking_no: str) -> tuple[TrackingInfo, dict]:
        """Return (info, notes). notes says how the answer was found (for the trace)."""
        carrier = carrier_of(tracking_no)
        notes: dict = {"carrier": carrier, "path": []}
        if carrier == "swiftship":
            try:
                data = self.breaker.call(lambda: call_with_retry(
                    lambda: self._api_once(tracking_no), API_RETRY, on_retry=self._note_retry(notes)))
                notes["path"].append("api")
                return self._from_api(data), notes
            except (TransientError, CircuitOpen) as exc:
                notes["path"].append(f"api failed ({exc}); circuit {self.breaker.state}")
        notes["path"].append("browser")
        return self._from_browser(carrier, tracking_no), notes

    def _note_retry(self, notes: dict) -> Callable[[Attempt], None]:
        def note(a: Attempt) -> None:
            notes.setdefault("retries", []).append({"attempt": a.number, "error": a.error, "wait_s": round(a.waited, 2)})
            if self.on_retry:
                self.on_retry(a)
        return note
