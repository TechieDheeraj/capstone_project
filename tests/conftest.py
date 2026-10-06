"""Shared fixtures: a fresh shop and app per test, one fake carrier per session."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from refund_desk import fake_carrier
from refund_desk.app import build
from refund_desk.config import ROOT, load_settings
from refund_desk.schemas import Ticket


@pytest.fixture(scope="session")
def carrier_url():
    srv = fake_carrier.start(port=0)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def settings(tmp_path, carrier_url, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_carrier.STATE.flaky_calls.clear()
    fake_carrier.STATE.api_down = False
    return replace(load_settings(), model_mode="offline", fallback="none", var_dir=tmp_path,
                   carrier_base_url=carrier_url, crash_at="", experiments_file=tmp_path / "none.json")


@pytest.fixture
def app(settings):
    a = build(settings)
    yield a
    if a.remote is not None:
        a.remote.close()


def approve_all(_approval):
    return True, "test"


TICKETS = {json.loads(x)["id"]: json.loads(x)
           for x in (ROOT / "data" / "tickets.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}


def ticket(tid: str, **over) -> Ticket:
    return Ticket(**{**TICKETS[tid], **over})
