"""The shop's own systems: orders, refunds, customer notes and the e-mail outbox.

In a real company these are other services (an order system, a payment
provider, an e-mail service). Here they are tables in var/shop.db, so the
project runs on a laptop.

Least privilege starts here: read tools get a READ-ONLY connection
(`mode=ro`). Even a bug or an attack in a read tool cannot change data.
Only the two write functions open a writable connection, and each one uses
an idempotency key, so a retried or resumed call never pays twice.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import demo_data as demo
from .config import today

SHOP_SCHEMA = """
CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT, email TEXT UNIQUE, phone TEXT);
CREATE TABLE orders (
  id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id), items_json TEXT, total_cents INTEGER,
  status TEXT, carrier TEXT, tracking_no TEXT, ordered_on TEXT, delivered_on TEXT
);
CREATE TABLE refunds (
  id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(id), amount_cents INTEGER, kind TEXT, reason TEXT,
  idempotency_key TEXT UNIQUE, created_on TEXT
);
CREATE TABLE customer_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, note TEXT,
  source TEXT, created_on TEXT);
CREATE TABLE outbox (id TEXT PRIMARY KEY, to_addr TEXT, subject TEXT, body TEXT,
  idempotency_key TEXT UNIQUE, created_at REAL);
"""


def seed(path: Path, reset: bool = False) -> None:
    """Create the shop database with the demo data. Dates are relative to today."""
    if path.exists():
        if not reset:
            return
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SHOP_SCHEMA)
    db.executemany("INSERT INTO customers VALUES (?,?,?,?)", demo.CUSTOMERS)
    for o in demo.ORDERS:
        items = [dict(sku=s, name=n, qty=q, unit_price_cents=p, final_sale=f) for s, n, q, p, f in o["items"]]
        total = sum(i["qty"] * i["unit_price_cents"] for i in items)
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
                   (o["id"], o["customer"], json.dumps(items), total, o["status"], o["carrier"], o["tracking"],
                    demo.day(o["ordered"]), demo.day(o["delivered"])))
    for order_id, cents, kind, days in demo.PAST_REFUNDS:
        db.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?,?)",
                   (f"rf_seed_{order_id}", order_id, cents, kind, "seed data", f"seed:{order_id}", demo.day(days)))
    for cust, note, days in demo.CUSTOMER_NOTES:
        db.execute("INSERT INTO customer_notes (customer_id, note, source, created_on) VALUES (?,?,?,?)",
                   (cust, note, "support team", demo.day(days)))
    db.commit()
    db.close()


class Shop:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _ro(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)   # read-only at the driver level
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _rw(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    # -- reads ----------------------------------------------------------------
    def customer_by_email(self, email: str) -> dict | None:
        with self._ro() as db:
            row = db.execute("SELECT * FROM customers WHERE lower(email)=lower(?)", (email,)).fetchone()
            return dict(row) if row else None

    def order(self, order_id: str) -> dict | None:
        with self._ro() as db:
            row = db.execute("SELECT o.*, c.email AS customer_email FROM orders o JOIN customers c "
                             "ON c.id = o.customer_id WHERE o.id=?", (order_id,)).fetchone()
            if not row:
                return None
            refunded = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM refunds WHERE order_id=?",
                                  (order_id,)).fetchone()[0]
        o = dict(row)
        o["items"] = json.loads(o.pop("items_json"))
        o["refunded_cents"] = int(refunded)
        return o

    def orders_of(self, customer_id: str) -> list[str]:
        with self._ro() as db:
            return [r[0] for r in db.execute("SELECT id FROM orders WHERE customer_id=? ORDER BY ordered_on",
                                             (customer_id,))]

    def memory(self, customer_id: str) -> dict:
        since = (today() - dt.timedelta(days=365)).isoformat()
        with self._ro() as db:
            notes = [r[0] for r in db.execute(
                "SELECT note FROM customer_notes WHERE customer_id=? ORDER BY id DESC LIMIT 10", (customer_id,))]
            rows = db.execute("SELECT r.kind FROM refunds r JOIN orders o ON o.id=r.order_id "
                              "WHERE o.customer_id=? AND r.created_on >= ?", (customer_id, since)).fetchall()
        return {"notes": notes, "refunds_last_12_months": len(rows),
                "goodwill_refunds_last_12_months": sum(1 for r in rows if r[0] == "goodwill")}

    def refunds_for(self, order_id: str) -> list[dict]:
        with self._ro() as db:
            return [dict(r) for r in db.execute("SELECT * FROM refunds WHERE order_id=?", (order_id,))]

    def refund_by_key(self, idem_key: str) -> dict | None:
        with self._ro() as db:
            row = db.execute("SELECT * FROM refunds WHERE idempotency_key=?", (idem_key,)).fetchone()
            return dict(row) if row else None

    def outbox(self) -> list[dict]:
        with self._ro() as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox ORDER BY created_at")]

    # -- writes (only after a human approved them) --------------------------------
    def issue_refund(self, order_id: str, amount_cents: int, kind: str, reason: str, idem_key: str) -> dict:
        db = self._rw()
        try:
            row = db.execute("SELECT * FROM refunds WHERE idempotency_key=?", (idem_key,)).fetchone()
            if row:   # the same request again (a retry or a resume): return the first result
                return {"refund_id": row["id"], "order_id": row["order_id"], "amount_cents": row["amount_cents"],
                        "status": "issued", "duplicate": True}
            rid = "rf_" + uuid.uuid4().hex[:10]
            db.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?,?)",
                       (rid, order_id, amount_cents, kind, reason, idem_key, today().isoformat()))
            db.commit()
            return {"refund_id": rid, "order_id": order_id, "amount_cents": amount_cents, "status": "issued",
                    "duplicate": False}
        finally:
            db.close()

    def add_note(self, customer_id: str, note: str, source: str) -> None:
        db = self._rw()
        try:
            db.execute("INSERT INTO customer_notes (customer_id, note, source, created_on) VALUES (?,?,?,?)",
                       (customer_id, note, source, today().isoformat()))
            db.commit()
        finally:
            db.close()

    def send_email(self, to_addr: str, subject: str, body: str, idem_key: str) -> dict:
        """Put the e-mail in the outbox. A real system hands it to an e-mail service."""
        db = self._rw()
        try:
            row = db.execute("SELECT id FROM outbox WHERE idempotency_key=?", (idem_key,)).fetchone()
            if row:
                return {"email_id": row["id"], "duplicate": True}
            eid = "em_" + uuid.uuid4().hex[:10]
            db.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?)", (eid, to_addr, subject, body, idem_key, time.time()))
            db.commit()
            return {"email_id": eid, "duplicate": False}
        finally:
            db.close()
