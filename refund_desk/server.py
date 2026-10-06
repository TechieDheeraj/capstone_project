"""The web UI and its API (FastAPI).

Practice: "Stream responses so users never stare at a spinner."
Practice: "Deploy it with a live link anyone can try."

A run takes seconds to minutes. The browser opens one Server-Sent Events
(SSE) stream per run and sees every step as it happens: the route, each
model call, each tool call, retries, flags, the reply while the model writes
it, and the approval request. SSE is plain HTTP, works through proxies, and
reconnects by itself; a chat app does not need WebSockets for this.

DEMO_MODE=1 makes a public link safe: the offline model only (no API cost),
a rate limit per IP address, short tickets only, and no real e-mail (the
outbox is a table).
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .app import App, build
from .config import load_settings
from .schemas import Ticket

WEB = Path(__file__).resolve().parent / "web"
MAX_RUNS_IN_MEMORY = 300


class Broker:
    """Keeps the events of recent runs in memory, so a late browser can replay them."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.events: dict[str, list[dict]] = {}
        self.order: deque[str] = deque()

    def publish(self, event: dict) -> None:
        with self.cond:
            rid = event["run_id"]
            if rid not in self.events:
                self.events[rid] = []
                self.order.append(rid)
                while len(self.order) > MAX_RUNS_IN_MEMORY:
                    self.events.pop(self.order.popleft(), None)
            self.events[rid].append({**event, "ts": time.time()})
            self.cond.notify_all()

    def stream(self, run_id: str, start: int = 0, idle_timeout: float = 600) -> Iterator[str]:
        i, last = start, time.time()
        while True:
            with self.cond:
                evs = self.events.get(run_id, [])
                if i >= len(evs):
                    self.cond.wait(timeout=15)
                    evs = self.events.get(run_id, [])
            if i < len(evs):
                for ev in evs[i:]:
                    i += 1
                    yield f"id: {i}\ndata: {json.dumps(ev, default=str)}\n\n"
                last = time.time()
                if evs[-1]["type"] in ("run_finished", "approval_needed", "run_error"):
                    return
            else:
                if time.time() - last > idle_timeout:
                    return
                yield ": keep-alive\n\n"   # stops proxies from closing an idle stream


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute, self.hits = per_minute, defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


class RunIn(BaseModel):
    ticket_id: str | None = None
    ticket: Ticket | None = None


class DecisionIn(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=300)


class FeedbackIn(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=500)


def create_app(app_: App | None = None) -> FastAPI:
    s = app_.settings if app_ else load_settings()
    core = app_ or build(s)
    broker = Broker()
    limiter = RateLimiter(s.rate_limit_per_min)
    tickets = {json.loads(x)["id"]: json.loads(x)
               for x in (s.data_dir / "tickets.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
    api = FastAPI(title="Refund Desk", docs_url="/api/docs")

    if hasattr(core.hub.primary, "stream_delay_s"):
        core.hub.primary.stream_delay_s = 0.015   # the offline model "types" at a readable speed in the UI

    def agent():
        return core.agent(listener=broker.publish)

    def background(fn, run_id: str) -> None:
        def work() -> None:
            try:
                fn()
            except Exception as exc:   # report to the browser; the trace has the details
                broker.publish({"run_id": run_id, "type": "run_error", "attrs": {"error": f"{type(exc).__name__}: {exc}"[:300]}})
        threading.Thread(target=work, daemon=True).start()

    @api.get("/api/health")
    def health() -> dict:
        return {"ok": True, "mode": s.model_mode, "demo": s.demo_mode, "prompt": s.prompt_version,
                "small_model": core.hub.model_for("small"), "large_model": core.hub.model_for("large"),
                "fallback": getattr(core.hub.fallback, "name", None),
                "budget": {"steps": s.max_steps, "tokens": s.max_tokens, "seconds": s.max_seconds,
                           "cost_usd": s.max_cost_usd}}

    @api.get("/api/tickets")
    def list_tickets() -> list[dict]:
        return list(tickets.values())

    @api.post("/api/runs")
    def start_run(body: RunIn, request: Request) -> dict:
        if s.demo_mode and not limiter.allow(request.client.host if request.client else "?"):
            raise HTTPException(429, "Too many runs. Wait one minute.")
        if body.ticket_id:
            if body.ticket_id not in tickets:
                raise HTTPException(404, "unknown ticket")
            ticket = Ticket(**tickets[body.ticket_id])
        elif body.ticket:
            ticket = body.ticket
            if s.demo_mode and len(ticket.body) > 2000:
                raise HTTPException(413, "Ticket too long for the demo.")
        else:
            raise HTTPException(422, "send ticket_id or ticket")
        from .store import new_id
        run_id = new_id("run")
        background(lambda: agent().start(ticket, run_id=run_id), run_id)
        return {"run_id": run_id}

    @api.get("/api/runs")
    def runs() -> list[dict]:
        return [{"id": r["id"], "ticket": r["ticket"]["id"], "subject": r["ticket"]["subject"], "status": r["status"],
                 "route": r["route"], "model": r["model"], "action": (r["resolution"] or {}).get("action"),
                 "cost_usd": r["spend"]["cost_usd"], "created_at": r["created_at"]}
                for r in core.store.list_runs(limit=50)]

    @api.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        r = core.store.load_run(run_id)
        if not r:
            raise HTTPException(404, "unknown run")
        return {"run": r, "spans": core.store.spans(run_id), "approvals": core.store.list_approvals(None, run_id),
                "feedback": core.store.feedback(run_id)}

    @api.get("/api/runs/{run_id}/events")
    def events(run_id: str, request: Request, start: int = 0) -> StreamingResponse:
        start = int(request.headers.get("last-event-id") or start)
        return StreamingResponse(broker.stream(run_id, start), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.post("/api/approvals/{approval_id}")
    def decide(approval_id: str, body: DecisionIn) -> dict:
        appr = core.store.get_approval(approval_id)
        if not appr:
            raise HTTPException(404, "unknown approval")
        if appr["status"] != "pending":
            raise HTTPException(409, f"already {appr['status']}")
        by = "web-user" if not s.demo_mode else "demo-visitor"
        background(lambda: agent().decide(approval_id, body.approved, by, body.reason), appr["run_id"])
        return {"ok": True, "run_id": appr["run_id"]}

    @api.post("/api/runs/{run_id}/feedback")
    def feedback(run_id: str, body: FeedbackIn) -> dict:
        from .feedback import record
        try:
            path = record(core.store, run_id, body.rating, body.comment)
        except KeyError:
            raise HTTPException(404, "unknown run") from None
        return {"ok": True, "draft_case": path.name if path else None}

    @api.get("/api/report")
    def report_api(by: str = "route") -> dict:
        from .report import report
        if by not in ("route", "model", "arm", "prompt_version", "status"):
            raise HTTPException(422, "bad group")
        return report(core.store, by)

    @api.post("/api/demo/carrier")
    def carrier_down(on: bool) -> dict:
        """Switch the FAKE carrier API off and on, to watch the browser fallback work."""
        import urllib.request
        req = urllib.request.Request(f"{s.carrier_base_url}/admin/swiftship/down?on={'1' if on else '0'}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as exc:
            raise HTTPException(502, f"fake carrier not reachable: {exc}") from exc
        core.toolbox.carrier.breaker.reset()
        return {"api_down": on}

    api.mount("/static", StaticFiles(directory=WEB), name="static")

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    return api


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    from .cli import _ensure_carrier
    s = load_settings()
    _ensure_carrier(s)
    print(f"Refund Desk on http://{host}:{port}   model mode: {s.model_mode}{'   DEMO MODE' if s.demo_mode else ''}")
    uvicorn.run(create_app(build(s)), host=host, port=port, log_level="warning")
