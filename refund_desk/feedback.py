"""Turn every thumbs-down into a new eval case.

Practices: "Turn every thumbs-down into a new eval case" and
"Build a golden test set from real user failures."

The loop:
  1. A support person clicks thumbs-down on a run (web UI or CLI).
  2. record() saves the feedback and writes a DRAFT case to evals/inbox/.
     The draft has the ticket, what the agent did, and "TODO" for what it
     SHOULD have done. Secrets and personal data in the text are removed
     first: eval cases live in the repository.
  3. A person fills in the TODO fields: the correct action, amount, tools.
  4. promote() checks the draft and appends it to evals/golden.jsonl.
  5. From now on, CI tests this case on every change. The same failure can
     never come back without a red build.

A human writes the expected result. The agent never grades itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import ROOT
from .redact import PII_PATTERNS, SECRET_PATTERNS, find_raw_pii, luhn_ok
from .store import Store

INBOX = ROOT / "evals" / "inbox"
GOLDEN = ROOT / "evals" / "golden.jsonl"


def scrub(text: str, keep_email: str) -> str:
    """Remove secrets and personal data from a ticket text before it goes into the repository.

    The sender's own e-mail stays (the case needs it to find the customer in the test shop).
    """
    for kind, pat in SECRET_PATTERNS:
        text = pat.sub(lambda m, k=kind: m.group(0) if k == "CARD" and not luhn_ok(m.group(0)) else f"[{k}_REMOVED]",
                       text)
    for kind, pat in PII_PATTERNS:
        text = pat.sub(lambda m, k=kind: m.group(0) if m.group(0).lower() == keep_email.lower() else f"[{k}]", text)
    return text


def record(store: Store, run_id: str, rating: int, comment: str | None = None, inbox: Path = INBOX) -> Path | None:
    run = store.load_run(run_id)
    if run is None:
        raise KeyError(run_id)
    store.add_feedback(run_id, rating, comment)
    if rating >= 0:
        return None
    res = run.get("resolution") or {}
    tools = [s["name"] for s in store.spans(run_id) if s["kind"] == "tool"]
    t = run["ticket"]
    ticket = {**t, "id": f"{t['id']}-fb", "subject": scrub(t["subject"], t["from_email"]),
              "body": scrub(t["body"], t["from_email"])}
    draft = {
        "id": f"feedback-{run_id}",
        "ticket": ticket,
        "tags": ["from-feedback"],
        "source": f"thumbs-down on {run_id}: {comment or 'no comment'}",
        "review": "Fill in every TODO. Check the ticket for personal data. Use a test customer of the demo shop.",
        "observed": {"action": res.get("action"), "refunded_cents": res.get("refunded_cents", 0), "tools": tools,
                     "model": run["model"], "prompt_version": run["prompt_version"]},
        "expect": {"action": "TODO", "refund_cents": "TODO", "must_call": tools, "must_not_call": [],
                   "max_steps": max(4, run["state"]["step"] + 1)},
    }
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{run_id}.json"
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def promote(draft_path: Path, golden: Path = GOLDEN) -> dict:
    case = json.loads(draft_path.read_text(encoding="utf-8"))
    todo = [k for k, v in case["expect"].items() if v == "TODO"]
    if todo:
        raise ValueError(f"fill in the expected values first: {', '.join(todo)}")
    if case["expect"]["action"] not in ("reply_only", "refund", "deny", "need_info", "escalate"):
        raise ValueError("expect.action is not a valid action")
    if not isinstance(case["expect"]["refund_cents"], int):
        raise ValueError("expect.refund_cents must be a whole number of cents")
    left = find_raw_pii(case["ticket"]["subject"] + "\n" + case["ticket"]["body"].replace(case["ticket"]["from_email"], ""))
    if left:
        raise ValueError(f"the ticket still contains personal data or secrets: {', '.join(sorted(set(left)))}")
    existing = {json.loads(line)["id"] for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()}
    if case["id"] in existing:
        raise ValueError(f"case {case['id']} is already in the golden set")
    case.pop("observed", None)
    case.pop("review", None)
    with golden.open("a", encoding="utf-8") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")
    draft_path.unlink()
    return case
