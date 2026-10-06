"""Command line: refund-desk <command>. Run `refund-desk --help` for the list."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path

from .config import ROOT, load_settings


def _ensure_carrier(s) -> None:
    """Start the fake carrier in this process if nothing answers at CARRIER_BASE_URL."""
    try:
        urllib.request.urlopen(s.carrier_base_url + "/health", timeout=1).read()
        return
    except Exception:
        pass
    from urllib.parse import urlparse

    from . import fake_carrier
    u = urlparse(s.carrier_base_url)
    if u.hostname in ("127.0.0.1", "localhost"):
        fake_carrier.start(u.hostname, u.port or 80)


def _tickets() -> dict[str, dict]:
    path = load_settings().data_dir / "tickets.jsonl"
    return {json.loads(x)["id"]: json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}


def _print_run(run: dict, app) -> None:
    res = run.get("resolution") or {}
    sp = run["spend"]
    print(f"\nrun      {run['id']}   status: {run['status']}")
    print(f"route    {run['route']} -> {run['model']}   arm: {run['arm']}   prompt: {run['prompt_version']} ({run['prompt_hash']})")
    print(f"spend    {sp['model_calls']} model calls, {sp['usage']['input_tokens']} in / {sp['usage']['output_tokens']} out "
          f"tokens, cache hits {sp['cache_hits']}, ${sp['cost_usd']:.4f}, {sp['active_seconds']:.1f} s")
    if run["flags"]:
        print(f"flags    {json.dumps(run['flags'])}")
    if res:
        print(f"result   action={res.get('action')} refunded={res.get('refunded_cents')} cents  refs={res.get('policy_refs')}")
        print(f"summary  {res.get('summary')}")
    pend = app.store.list_approvals("pending", run["id"])
    for a in pend:
        print(f"\nWAITING FOR APPROVAL  {a['id']}  ({a['kind']}){'  ⚠ FLAGGED' if a['flagged'] else ''}")
        print(json.dumps(a["payload"], indent=2, ensure_ascii=False)[:1500])
        print(f"\n  approve:  refund-desk approve {a['id']}\n  reject:   refund-desk approve {a['id']} --reject --reason \"...\"")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="refund-desk", description="Refund Desk: a production-style support agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("seed", help="create the demo shop database")
    sp.add_argument("--reset", action="store_true", help="delete and create again")
    sub.add_parser("tickets", help="list the sample tickets")
    sp = sub.add_parser("run", help="run the agent on one ticket")
    sp.add_argument("ticket", help="a sample ticket id (T-101) or a JSON file")
    sp.add_argument("--auto-approve", action="store_true", help="approve refunds and e-mails at once (demo only)")
    sp = sub.add_parser("resume", help="continue a run from its last checkpoint")
    sp.add_argument("run_id")
    sp = sub.add_parser("approvals", help="list pending approvals")
    sp = sub.add_parser("approve", help="approve (or reject) a pending refund or e-mail")
    sp.add_argument("approval_id")
    sp.add_argument("--reject", action="store_true")
    sp.add_argument("--reason", default=None)
    sp = sub.add_parser("trace", help="show the trace of a run")
    sp.add_argument("run_id")
    sub.add_parser("runs", help="list recent runs")
    sp = sub.add_parser("eval", help="run the golden eval set")
    sp.add_argument("--tags", nargs="*", help="only cases with these tags")
    sp.add_argument("--gate", action="store_true", help="exit 1 if scores dropped below the baseline (for CI)")
    sp.add_argument("--update-baseline", action="store_true", help="save these scores as the new baseline")
    sp.add_argument("--out", type=Path, help="write the full report as JSON")
    sp = sub.add_parser("report", help="cost per task and A/B results from past runs")
    sp.add_argument("--by", default="route", choices=["route", "model", "arm", "prompt_version", "status"])
    sp = sub.add_parser("feedback", help="rate a run; thumbs-down makes a draft eval case")
    sp.add_argument("run_id")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--up", action="store_true")
    g.add_argument("--down", action="store_true")
    sp.add_argument("--comment", default=None)
    sp = sub.add_parser("promote", help="move a completed draft case from evals/inbox into the golden set")
    sp.add_argument("draft", type=Path)
    sp = sub.add_parser("export-finetune", help="export the best runs as fine-tuning data")
    sp.add_argument("--out", type=Path, default=ROOT / "var" / "finetune" / "train.jsonl")
    sp.add_argument("--include-unrated", action="store_true", help="also use resolved runs with no rating")
    sp = sub.add_parser("serve", help="start the web UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sub.add_parser("carrier", help="start only the fake carrier service")
    sub.add_parser("mcp", help="start the MCP server on stdio (for Claude Code and other MCP clients)")

    a = p.parse_args(argv)
    s = load_settings()

    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0
    if a.cmd == "carrier":
        from . import fake_carrier
        from urllib.parse import urlparse
        port = urlparse(s.carrier_base_url).port or 8901
        fake_carrier.start("127.0.0.1", port)
        print(f"Fake carrier on http://127.0.0.1:{port}  (Ctrl+C to stop)")
        try:
            import threading
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return 0
    if a.cmd == "serve":
        from .server import serve
        serve(a.host, a.port)
        return 0
    if a.cmd == "seed":
        from .shop import seed
        s.ensure_dirs()
        seed(s.shop_db, reset=a.reset)
        print(f"Shop database ready: {s.shop_db.relative_to(ROOT) if s.shop_db.is_relative_to(ROOT) else s.shop_db}")
        return 0
    if a.cmd == "tickets":
        for t in _tickets().values():
            print(f"{t['id']}  {t['from_name']:<16} {t['subject']}")
        return 0
    if a.cmd == "eval":
        from .evals import gate, load_cases, run_evals, save_baseline
        print(f"Eval set: {len(load_cases(tags=a.tags))} cases · model mode: {s.model_mode} · prompt: {s.prompt_version}\n")
        report = run_evals(s, load_cases(tags=a.tags))
        sm = report["summary"]
        print(f"\npass rate {sm['pass_rate']:.0%}   outcome {sm['outcome']:.2f}   path {sm['path']:.2f}   "
              f"safety {sm['safety']:.2f}   reply {sm['reply']:.2f}")
        print(f"cost per task ${sm['cost_per_task_usd']:.4f}   cost per resolved ${sm['cost_per_resolved_usd']:.4f}   "
              f"mean steps {sm['mean_steps']}   p50 {sm['p50_seconds']} s")
        if a.out:
            a.out.write_text(json.dumps(report, indent=2))
        if a.update_baseline:
            save_baseline(sm)
            print("Baseline updated.")
        if a.gate:
            problems = gate(sm)
            if problems:
                print("\nEVAL GATE: FAILED")
                for x in problems:
                    print(f"  - {x}")
                return 1
            print("\nEVAL GATE: passed")
        return 0

    from .app import build
    if a.cmd == "resume":
        s = replace(s, crash_at="")   # never crash again on purpose while we recover
    _ensure_carrier(s)
    app = build(s)
    approver = (lambda appr: (True, "auto-approved from the CLI")) if getattr(a, "auto_approve", False) else None
    agent = app.agent(listener=None, approver=approver)

    if a.cmd == "run":
        from .schemas import Ticket
        if Path(a.ticket).exists():
            ticket = Ticket(**json.loads(Path(a.ticket).read_text(encoding="utf-8")))
        else:
            tickets = _tickets()
            if a.ticket not in tickets:
                print(f"unknown ticket {a.ticket}; see `refund-desk tickets`", file=sys.stderr)
                return 2
            ticket = Ticket(**tickets[a.ticket])
        from .agent import SimulatedCrash
        try:
            run = agent.start(ticket)
        except SimulatedCrash as exc:
            runs = app.store.list_runs(limit=1)
            print(f"\n💥 simulated crash at '{exc}'. The last checkpoint is safe in var/app.db.")
            print(f"   continue with:  refund-desk resume {runs[0]['id']}")
            return 3
        _print_run(run, app)
    elif a.cmd == "resume":
        _print_run(agent.resume(a.run_id), app)
    elif a.cmd == "approvals":
        for x in app.store.list_approvals("pending"):
            print(f"{x['id']}  run={x['run_id']}  {x['kind']:<6} {'FLAGGED ' if x['flagged'] else ''}"
                  f"{json.dumps(x['payload'].get('args') or {'to': x['payload'].get('to')})}")
    elif a.cmd == "approve":
        _print_run(agent.decide(a.approval_id, not a.reject, "cli-user", a.reason), app)
    elif a.cmd == "trace":
        from .trace import render_tree
        print(render_tree(app.store.spans(a.run_id)))
    elif a.cmd == "runs":
        for r in app.store.list_runs(limit=30):
            res = r.get("resolution") or {}
            print(f"{r['id']}  {r['ticket']['id']:<7} {r['status']:<16} {r['route']:<5} {r['model']:<18} "
                  f"{res.get('action') or '-':<10} ${r['spend']['cost_usd']:.4f}")
    elif a.cmd == "report":
        from .report import report
        rep = report(app.store, a.by)
        print(json.dumps(rep, indent=2))
    elif a.cmd == "feedback":
        from .feedback import record
        path = record(app.store, a.run_id, 1 if a.up else -1, a.comment)
        print("Thanks. " + (f"Draft eval case: {path.relative_to(ROOT)}  (fill in the TODO fields, then "
                            f"`refund-desk promote {path.relative_to(ROOT)}`)" if path else "Saved."))
    elif a.cmd == "promote":
        from .feedback import promote
        case = promote(a.draft)
        print(f"Added {case['id']} to evals/golden.jsonl.")
    elif a.cmd == "export-finetune":
        from .finetune import export
        result = export(app.store, app.toolbox, s.prompts_dir, a.out, a.include_unrated)
        for k in ("train_file", "holdout_file"):
            p_ = Path(result[k]).resolve()
            result[k] = str(p_.relative_to(Path.cwd())) if p_.is_relative_to(Path.cwd()) else str(p_)
        print(json.dumps(result, indent=2))
    if app.remote is not None:
        app.remote.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
