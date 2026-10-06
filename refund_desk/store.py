"""The agent's own database: runs, checkpoints, approvals, traces, feedback, cache.

One SQLite file (var/app.db). SQLite is enough for one server. For many
servers, move the same tables to Postgres; the code above this layer does
not change.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  ticket_json TEXT NOT NULL,
  status TEXT NOT NULL,              -- running | waiting_approval | resolved | escalated | stopped_budget | failed
  route TEXT, model TEXT, arm TEXT, experiment TEXT,
  prompt_version TEXT, prompt_hash TEXT,
  state_json TEXT NOT NULL,          -- the checkpoint: messages, step, pending tool calls
  spend_json TEXT NOT NULL,
  resolution_json TEXT,
  flags_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vault (     -- PII placeholders -> real values. Never sent to a model.
  run_id TEXT PRIMARY KEY, data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, tool_use_id TEXT,
  payload_json TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, flagged INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, decided_at REAL, decided_by TEXT
);
CREATE TABLE IF NOT EXISTS spans (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_id TEXT, kind TEXT NOT NULL, name TEXT NOT NULL,
  start_ts REAL NOT NULL, end_ts REAL, status TEXT, attrs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spans_run ON spans(run_id, start_ts);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, rating INTEGER NOT NULL,
  comment TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, expires_at REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # -- runs (the checkpoint is the whole state, saved after every step) ------
    def save_run(self, run: dict) -> None:
        now = time.time()
        self._exec(
            """INSERT INTO runs (id, ticket_id, ticket_json, status, route, model, arm, experiment,
                                 prompt_version, prompt_hash, state_json, spend_json, resolution_json,
                                 flags_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, model=excluded.model,
                 state_json=excluded.state_json, spend_json=excluded.spend_json,
                 resolution_json=excluded.resolution_json, flags_json=excluded.flags_json,
                 updated_at=excluded.updated_at""",
            (run["id"], run["ticket"]["id"], json.dumps(run["ticket"]), run["status"], run.get("route"),
             run.get("model"), run.get("arm"), run.get("experiment"), run.get("prompt_version"),
             run.get("prompt_hash"), json.dumps(run["state"]), json.dumps(run["spend"]),
             json.dumps(run.get("resolution")) if run.get("resolution") is not None else None,
             json.dumps(run.get("flags", [])), run.get("created_at", now), now))

    def load_run(self, run_id: str) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row:
            return None
        return {
            "id": row["id"], "ticket": json.loads(row["ticket_json"]), "status": row["status"],
            "route": row["route"], "model": row["model"], "arm": row["arm"], "experiment": row["experiment"],
            "prompt_version": row["prompt_version"], "prompt_hash": row["prompt_hash"],
            "state": json.loads(row["state_json"]), "spend": json.loads(row["spend_json"]),
            "resolution": json.loads(row["resolution_json"]) if row["resolution_json"] else None,
            "flags": json.loads(row["flags_json"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self._all("SELECT id FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [r for r in (self.load_run(x["id"]) for x in rows) if r]

    # -- the PII vault ----------------------------------------------------------
    def save_vault(self, run_id: str, vault: dict[str, str]) -> None:
        self._exec("INSERT INTO vault (run_id, data_json) VALUES (?,?) "
                   "ON CONFLICT(run_id) DO UPDATE SET data_json=excluded.data_json", (run_id, json.dumps(vault)))

    def load_vault(self, run_id: str) -> dict[str, str]:
        row = self._one("SELECT data_json FROM vault WHERE run_id=?", (run_id,))
        return json.loads(row["data_json"]) if row else {}

    # -- approvals ---------------------------------------------------------------
    def create_approval(self, run_id: str, kind: str, tool_use_id: str | None, payload: dict,
                        flagged: bool) -> str:
        aid = new_id("apr")
        self._exec("INSERT INTO approvals (id, run_id, kind, tool_use_id, payload_json, status, flagged, created_at)"
                   " VALUES (?,?,?,?,?,?,?,?)",
                   (aid, run_id, kind, tool_use_id, json.dumps(payload), "pending", int(flagged), time.time()))
        return aid

    def decide_approval(self, approval_id: str, approved: bool, by: str, reason: str | None = None) -> dict:
        row = self.get_approval(approval_id)
        if not row:
            raise KeyError(approval_id)
        if row["status"] != "pending":
            return row  # deciding twice changes nothing (idempotent)
        self._exec("UPDATE approvals SET status=?, reason=?, decided_by=?, decided_at=? WHERE id=?",
                   ("approved" if approved else "rejected", reason, by, time.time(), approval_id))
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> dict | None:
        row = self._one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if row:
            row["payload"] = json.loads(row.pop("payload_json"))
        return row

    def list_approvals(self, status: str | None = "pending", run_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        rows = self._all(sql + " ORDER BY created_at", tuple(args))
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json"))
        return rows

    # -- spans (traces) -----------------------------------------------------------
    def save_span(self, span: dict) -> None:
        self._exec("INSERT OR REPLACE INTO spans (id, run_id, parent_id, kind, name, start_ts, end_ts, status,"
                   " attrs_json) VALUES (?,?,?,?,?,?,?,?,?)",
                   (span["id"], span["run_id"], span.get("parent_id"), span["kind"], span["name"],
                    span["start_ts"], span.get("end_ts"), span.get("status"), json.dumps(span.get("attrs", {}))))

    def spans(self, run_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM spans WHERE run_id=? ORDER BY start_ts", (run_id,))
        for r in rows:
            r["attrs"] = json.loads(r.pop("attrs_json"))
        return rows

    # -- feedback ---------------------------------------------------------------
    def add_feedback(self, run_id: str, rating: int, comment: str | None) -> int:
        cur = self._exec("INSERT INTO feedback (run_id, rating, comment, created_at) VALUES (?,?,?,?)",
                         (run_id, rating, comment, time.time()))
        return int(cur.lastrowid or 0)

    def feedback(self, run_id: str | None = None) -> list[dict]:
        if run_id:
            return self._all("SELECT * FROM feedback WHERE run_id=? ORDER BY id", (run_id,))
        return self._all("SELECT * FROM feedback ORDER BY id")

    # -- cache --------------------------------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        row = self._one("SELECT value_json, expires_at FROM cache WHERE key=?", (key,))
        if not row or row["expires_at"] < time.time():
            return None
        self._exec("UPDATE cache SET hits = hits + 1 WHERE key=?", (key,))
        return json.loads(row["value_json"])

    def cache_put(self, key: str, value: Any, ttl_s: float) -> None:
        self._exec("INSERT OR REPLACE INTO cache (key, value_json, expires_at, hits) VALUES (?,?,?,0)",
                   (key, json.dumps(value), time.time() + ttl_s))

    def cache_clear(self) -> None:
        self._exec("DELETE FROM cache")
