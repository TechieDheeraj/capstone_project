"""Export your best production runs as training data for a small model.

Practice: "Fine-tune small models on your best production outputs."

The idea is called distillation. The large model (expensive) handles the
hard tickets well. Its best runs, the ones that a human approved and rated
thumbs-up, show exactly what good work looks like. A small model trained on
them can take over part of the work at a fraction of the cost.

This module writes those runs as chat-format JSONL with tool calls, the
format that most fine-tuning tools accept (OpenAI-style "messages" + "tools").
The messages are already redacted: no PII goes into training data.

Rules for good training data:
  * Only runs that a human approved AND rated thumbs-up (or that passed evals).
  * No thumbs-down runs, no runs that stopped on a budget, no escalations.
  * Remove near-duplicates, so one common ticket does not dominate.
  * Keep a held-out set, and compare the tuned model with the eval set
    before you route any traffic to it.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from .prompts import load_prompt
from .providers.openai_compat import to_openai, to_openai_tools
from .store import Store
from .tools import Toolbox


def select_runs(store: Store, include_unrated: bool = False) -> list[dict]:
    ratings: dict[str, list[int]] = {}
    for f in store.feedback():
        ratings.setdefault(f["run_id"], []).append(f["rating"])
    chosen = []
    for run in store.list_runs(limit=100_000):
        r = ratings.get(run["id"], [])
        if run["status"] != "resolved" or any(x < 0 for x in r):
            continue
        if any(x > 0 for x in r) or (include_unrated and not r):
            chosen.append(run)
    return chosen


def export(store: Store, toolbox: Toolbox, prompts_dir: Path, out: Path, include_unrated: bool = False,
           holdout: float = 0.2, seed: int = 7) -> dict:
    tools = to_openai_tools(toolbox.definitions())
    seen, examples = set(), []
    for run in select_runs(store, include_unrated):
        system = load_prompt(prompts_dir, run["prompt_version"]).text
        messages = to_openai(system, run["state"]["messages"])
        key = hashlib.sha256(json.dumps(messages[1]["content"]).encode()).hexdigest()   # the first user turn
        if key in seen:
            continue
        seen.add(key)
        examples.append({"messages": messages, "tools": tools,
                         "meta": {"run_id": run["id"], "model": run["model"], "prompt": run["prompt_version"]}})
    random.Random(seed).shuffle(examples)
    n_hold = int(len(examples) * holdout) if len(examples) >= 5 else 0
    out.parent.mkdir(parents=True, exist_ok=True)
    train, held = examples[n_hold:], examples[:n_hold]
    out.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in train), encoding="utf-8")
    hold_path = out.with_name(out.stem + ".holdout.jsonl")
    hold_path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in held), encoding="utf-8")
    return {"train": len(train), "holdout": len(held), "train_file": str(out), "holdout_file": str(hold_path)}
