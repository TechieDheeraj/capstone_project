import json

from refund_desk import feedback
from refund_desk.evals import gate, load_cases, run_evals

from .conftest import approve_all, ticket


def test_eval_cases_pass_with_the_offline_model(settings):
    cases = [c for c in load_cases() if c["id"] in ("damaged-full-refund", "injection-in-ticket", "carrier-api-outage")]
    report = run_evals(settings, cases, progress=None)
    assert report["summary"]["pass_rate"] == 1.0, report["results"]


def test_path_score_catches_a_right_answer_by_a_wrong_path(settings):
    case = next(c for c in load_cases() if c["id"] == "damaged-full-refund")
    case["expect"]["must_call"] = ["lookup_order", "track_shipment", "issue_refund"]   # demand a step it skips
    r = run_evals(settings, [case], progress=None)["results"][0]
    assert r["scores"]["outcome"] == 1.0 and r["scores"]["path"] < 1.0 and not r["passed"]


def test_gate_blocks_a_drop_and_any_safety_failure(tmp_path):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"offline": {"pass_rate": 1.0, "outcome": 1.0, "path": 1.0, "safety": 1.0, "reply": 1.0}}))
    good = {"model_mode": "offline", "pass_rate": 1.0, "outcome": 1.0, "path": 1.0, "safety": 1.0, "reply": 0.99}
    assert gate(good, base) == []
    assert gate({**good, "pass_rate": 0.9}, base)
    assert any("safety" in p for p in gate({**good, "safety": 0.99}, base))


def test_thumbs_down_becomes_a_draft_case_then_a_golden_case(app, tmp_path):
    run = app.agent(approver=approve_all).start(ticket("T-109"))
    draft = feedback.record(app.store, run["id"], -1, "should ask which mug", inbox=tmp_path)
    case = json.loads(draft.read_text())
    assert case["expect"]["action"] == "TODO"
    assert "4539" not in case["ticket"]["body"] and "98765" not in case["ticket"]["body"]   # no PII in the repo
    golden = tmp_path / "golden.jsonl"
    golden.write_text("")
    try:
        feedback.promote(draft, golden)
        raise AssertionError("promote must refuse TODO fields")
    except ValueError:
        pass
    case["expect"].update(action="need_info", refund_cents=0)
    draft.write_text(json.dumps(case))
    feedback.promote(draft, golden)
    assert json.loads(golden.read_text())["id"] == case["id"] and not draft.exists()


def test_eval_catches_a_redaction_regression(settings, monkeypatch):
    """The safety check must not share code with redact.py, or a bug there would hide itself."""
    import refund_desk.redact as redact
    monkeypatch.setattr(redact, "PII_PATTERNS", [p for p in redact.PII_PATTERNS if p[0] != "PHONE"])
    case = next(c for c in load_cases() if c["id"] == "missing-item-pii")
    r = run_evals(settings, [case], progress=None)["results"][0]
    assert "safety.no_raw_pii_to_model" in r["failed"]
