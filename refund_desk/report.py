"""Cost per task and A/B comparison, from the runs in var/app.db.

Practices: "Track cost per task, not cost per token" and
"A/B test new models on a slice of real traffic first."

cost per task      = all model spend / all runs
cost per resolved  = all model spend / runs that the agent resolved
                     (failed and escalated runs still cost money, so they
                      make each resolved ticket more expensive. That is real.)
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from .store import Store


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def group_stats(runs: list[dict], feedback: list[dict]) -> dict:
    down = {f["run_id"] for f in feedback if f["rating"] < 0}
    n = len(runs)
    cost = sum(r["spend"]["cost_usd"] for r in runs)
    resolved = [r for r in runs if r["status"] == "resolved"]
    secs = [r["spend"]["active_seconds"] for r in runs]
    return {
        "runs": n,
        "resolved_rate": round(len(resolved) / n, 3) if n else 0.0,
        "escalated_rate": round(sum(r["status"] == "escalated" for r in runs) / n, 3) if n else 0.0,
        "thumbs_down_rate": round(sum(r["id"] in down for r in runs) / n, 3) if n else 0.0,
        "cost_per_task_usd": round(cost / n, 5) if n else 0.0,
        "cost_per_resolved_usd": round(cost / len(resolved), 5) if resolved else None,
        "mean_steps": round(statistics.mean(r["state"]["step"] for r in runs), 2) if n else 0.0,
        "p50_seconds": round(_pct(secs, 0.5), 2),
        "p95_seconds": round(_pct(secs, 0.95), 2),
        "cache_hits": sum(r["spend"]["cache_hits"] for r in runs),
    }


def report(store: Store, by: str = "route") -> dict:
    runs = [r for r in store.list_runs(limit=100_000) if r["status"] != "running"]
    fb = store.feedback()
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        if by == "arm":
            key = f"{r.get('experiment') or '-'} / {r.get('arm')} / {r['model']}"
        else:
            key = str(r.get(by))
        groups[key].append(r)
    return {"all": group_stats(runs, fb), "by": by, "groups": {k: group_stats(v, fb) for k, v in sorted(groups.items())}}
