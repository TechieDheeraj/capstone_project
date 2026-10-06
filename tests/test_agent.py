import json
from dataclasses import replace

import pytest

from refund_desk.agent import SimulatedCrash
from refund_desk.app import build
from refund_desk.providers import ModelHub
from refund_desk.providers.base import ModelResponse
from refund_desk.retry import TransientError

from .conftest import approve_all, ticket


def test_full_run_with_human_approvals(app):
    agent = app.agent()
    run = agent.start(ticket("T-101"))
    assert run["status"] == "waiting_approval"
    refund = app.store.list_approvals("pending", run["id"])[0]
    assert refund["kind"] == "refund" and refund["payload"]["args"]["amount_cents"] == 4800
    run = agent.decide(refund["id"], True, "tester")
    email = app.store.list_approvals("pending", run["id"])[0]
    assert email["kind"] == "email" and email["payload"]["body"].startswith("Hi Maya,")   # rehydrated for people
    run = agent.decide(email["id"], True, "tester")
    assert run["status"] == "resolved" and run["resolution"]["refunded_cents"] == 4800
    assert len(app.shop.outbox()) == 1


def test_rejected_refund_is_not_paid(app):
    agent = app.agent()
    run = agent.start(ticket("T-101"))
    run = agent.decide(app.store.list_approvals("pending", run["id"])[0]["id"], False, "tester", "need a photo")
    assert app.shop.refunds_for("A-1001") == []
    assert run["status"] == "waiting_approval"      # now the e-mail, with action "escalate"
    assert app.store.list_approvals("pending", run["id"])[0]["payload"]["action"] == "escalate"


def test_model_never_sees_raw_pii(app):
    run = app.agent(approver=approve_all).start(ticket("T-109"))
    sent = json.dumps(run["state"]["messages"])
    for raw in ("Priya", "priya.raman@example.com", "98765", "4539"):
        assert raw not in sent
    assert "[CARD_REMOVED]" in sent and "<PHONE_1>" in sent


def test_crash_after_model_call_resumes_from_cache(settings):
    app = build(replace(settings, crash_at="model:3"))
    with pytest.raises(SimulatedCrash):
        app.agent(approver=approve_all).start(ticket("T-106"))
    run_id = app.store.list_runs(1)[0]["id"]
    app2 = build(replace(settings, crash_at=""))
    run = app2.agent(approver=approve_all).resume(run_id)
    assert run["status"] == "resolved" and run["spend"]["cache_hits"] >= 1


def test_crash_after_refund_never_pays_twice(settings):
    app = build(replace(settings, crash_at="refund"))
    with pytest.raises(SimulatedCrash):
        app.agent(approver=approve_all).start(ticket("T-107"))
    run_id = app.store.list_runs(1)[0]["id"]
    run = build(replace(settings, crash_at="")).agent(approver=approve_all).resume(run_id)
    assert run["status"] == "resolved"
    assert len(app.shop.refunds_for("A-1008")) == 1


def test_budget_stops_the_run_safely(settings):
    app = build(replace(settings, max_steps=2))
    run = app.agent(approver=approve_all).start(ticket("T-101"))
    assert run["status"] == "stopped_budget" and run["resolution"]["action"] == "escalate"
    assert app.shop.refunds_for("A-1001") == []


def test_cache_makes_a_repeat_question_free(app):
    agent = app.agent(approver=approve_all)
    first = agent.start(ticket("T-111"))
    second = agent.start(ticket("T-111"))
    assert first["spend"]["cost_usd"] > 0
    assert second["spend"]["cost_usd"] == 0 and second["spend"]["cache_hits"] == second["spend"]["model_calls"]


class Down:
    name = "anthropic"

    def complete(self, req, on_delta=None):
        raise TransientError("HTTP 529 overloaded")


def test_fallback_model_takes_over_when_the_provider_is_down(settings, monkeypatch):
    monkeypatch.setattr("refund_desk.providers.MODEL_RETRY.base_delay", 0.0)
    from refund_desk.providers.offline import OfflineModel
    app = build(settings, hub=None)
    app.hub = ModelHub(settings, app.cache, primary=Down(), fallback=OfflineModel())
    run = app.agent(approver=approve_all).start(ticket("T-102"))
    assert run["status"] == "resolved"
    assert any(s["name"] == "fallback_used" for s in app.store.spans(run["id"]))


def test_no_fallback_means_a_safe_failure(settings, monkeypatch):
    monkeypatch.setattr("refund_desk.providers.MODEL_RETRY.base_delay", 0.0)
    app = build(settings)
    app.hub = ModelHub(settings, app.cache, primary=Down(), fallback=None)
    run = app.agent(approver=approve_all).start(ticket("T-102"))
    assert run["status"] == "failed" and run["resolution"]["action"] == "escalate"


class Liar:
    """A model that claims a refund without issuing one."""
    name = "liar"

    def complete(self, req, on_delta=None):
        args = {"action": "refund", "order_id": "A-1001", "refund_cents": 4800, "reply": "Hi, we refunded you $48.",
                "policy_refs": [], "summary": "refunded"}
        return ModelResponse([{"type": "tool_use", "id": f"t{len(req.messages)}", "name": "finish", "input": args}],
                             "tool_use", model=req.model, provider="liar")


def test_harness_rejects_a_false_claim(settings):
    app = build(replace(settings, max_steps=3))
    app.hub = ModelHub(settings, app.cache, primary=Liar(), fallback=None)
    run = app.agent(approver=approve_all).start(ticket("T-101"))
    assert run["status"] == "stopped_budget"
    assert any(s["name"] == "finish_rejected" for s in app.store.spans(run["id"]))
    assert app.shop.outbox() == []


def test_a_second_ticket_for_a_refunded_order_does_not_refund_again(app):
    agent = app.agent(approver=approve_all)
    assert agent.start(ticket("T-101"))["resolution"]["refunded_cents"] == 4800
    again = agent.start(ticket("T-101", id="T-101-again"))
    assert again["resolution"]["action"] == "reply_only" and again["resolution"]["refunded_cents"] == 0
    assert len(app.shop.refunds_for("A-1001")) == 1
