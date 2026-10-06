"""Log every model call, tool call and decision as a trace.

Practice: "Log every model call, tool call and decision with traces."

A trace is a tree of spans. One run is the root span. Each model call, tool
call and decision is a child span with a start time, an end time, a status
and attributes (tokens, cost, retries, cache hit, route, arm ...).

Why a tree and not plain log lines: you can see the path that the agent took,
where the time went and where the money went, for one ticket. Evals score
the same trace ("score the path"), so the trace is also test data.

Attributes are redacted: a trace never holds raw PII. The format is close to
the OpenTelemetry GenAI conventions, so an exporter to a tracing backend is
a small addition.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .store import Store, new_id

Listener = Callable[[dict], None]


class Tracer:
    def __init__(self, store: Store, run_id: str, listener: Listener | None = None, root_id: str | None = None):
        self.store, self.run_id, self.listener = store, run_id, listener
        self.root_id = root_id or new_id("span")
        self._stack: list[str] = [self.root_id]

    def emit(self, event: dict) -> None:
        """Send a live event to the UI (server-sent events). Never raises."""
        if self.listener:
            try:
                self.listener({"run_id": self.run_id, **event})
            except Exception:
                pass

    def start_root(self, name: str, attrs: dict | None = None) -> None:
        self.store.save_span({"id": self.root_id, "run_id": self.run_id, "parent_id": None, "kind": "run",
                              "name": name, "start_ts": time.time(), "status": "running", "attrs": attrs or {}})

    def end_root(self, status: str, attrs: dict | None = None) -> None:
        spans = [s for s in self.store.spans(self.run_id) if s["id"] == self.root_id]
        if spans:
            root = spans[0]
            root["end_ts"], root["status"] = time.time(), status
            root["attrs"].update(attrs or {})
            self.store.save_span(root)

    @contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Iterator[dict]:
        """Usage: with tracer.span("tool", "lookup_order", args=...) as s: s["attrs"]["x"] = 1"""
        span = {"id": new_id("span"), "run_id": self.run_id, "parent_id": self._stack[-1], "kind": kind,
                "name": name, "start_ts": time.time(), "status": "ok", "attrs": dict(attrs)}
        self._stack.append(span["id"])
        self.emit({"type": f"{kind}_start", "name": name, "span_id": span["id"], "attrs": span["attrs"]})
        try:
            yield span
        except Exception as exc:
            span["status"] = "error"
            span["attrs"].setdefault("error", f"{type(exc).__name__}: {exc}"[:300])
            raise
        finally:
            self._stack.pop()
            span["end_ts"] = time.time()
            span["attrs"]["ms"] = round((span["end_ts"] - span["start_ts"]) * 1000, 1)
            self.store.save_span(span)
            self.emit({"type": f"{kind}_end", "name": name, "span_id": span["id"],
                       "status": span["status"], "attrs": span["attrs"]})

    def decision(self, name: str, **attrs: Any) -> None:
        """A point event: a choice the harness made (route, budget stop, approval, flag)."""
        now = time.time()
        self.store.save_span({"id": new_id("span"), "run_id": self.run_id, "parent_id": self._stack[-1],
                              "kind": "decision", "name": name, "start_ts": now, "end_ts": now,
                              "status": "ok", "attrs": attrs})
        self.emit({"type": "decision", "name": name, "attrs": attrs})


def render_tree(spans: list[dict]) -> str:
    """Plain-text trace for the CLI."""
    children: dict[str | None, list[dict]] = {}
    for s in spans:
        children.setdefault(s["parent_id"], []).append(s)
    t0 = min((s["start_ts"] for s in spans), default=0.0)
    lines: list[str] = []

    def fmt(v: object) -> str:
        if isinstance(v, list):
            v = ",".join(map(str, v))
        elif isinstance(v, dict):
            v = ",".join(f"{k}:{x}" for k, x in v.items())
        text = str(v)
        return text if len(text) <= 70 else text[:67] + "..."

    skip = {"ms", "args", "spend", "simulated", "stop", "cache_read", "cache_write", "provider", "prompt_hash",
            "mode", "experiment", "approval_id"}
    marks = {"ok": "✓", "error": "✗", "running": "…", "resolved": "✓", "escalated": "!", "stopped_budget": "■",
             "failed": "✗", "waiting_approval": "⏸"}

    def walk(parent: str | None, depth: int) -> None:
        for s in sorted(children.get(parent, []), key=lambda x: x["start_ts"]):
            a = s["attrs"]
            info = [f"{k}={fmt(v)}" for k, v in a.items() if k not in skip and v not in (None, "", [], {})]
            ms = a.get("ms")
            at = f"+{(s['start_ts'] - t0) * 1000:7.0f}ms"
            mark = marks.get(s.get("status") or "ok", "·")
            lines.append(f"{at} {'  ' * depth}{mark} {s['kind']}:{s['name']}"
                         + (f" ({ms:.0f} ms)" if isinstance(ms, (int, float)) else "")
                         + (f"  {' '.join(info)}" if info else ""))
            walk(s["id"], depth + 1)

    walk(None, 0)
    return "\n".join(lines)
