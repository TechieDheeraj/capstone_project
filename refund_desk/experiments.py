"""A/B test new models on a slice of real traffic first.

Practice: "A/B test new models on a slice of real traffic first."

An offline eval set is small and you wrote it yourself. Real traffic has
cases that you did not think of. So a new model (or prompt) first gets a
small slice, for example 10% of the "large" route. The other 90% stay on the
current model (the control). After enough tickets, compare the two arms on
the numbers that matter: resolution rate, human edits, thumbs-down, cost per
task and latency. Then roll forward or roll back.

Assignment uses a hash of the ticket id, not random(). The same ticket always
lands in the same arm, so a retry or a resumed run does not switch models in
the middle of a conversation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Experiment:
    name: str
    route: str          # which route it applies to: "small" or "large"
    control: str        # current model id
    candidate: str      # new model id
    percent: int        # share of traffic for the candidate, 0..100
    enabled: bool = True


def load_experiments(path: Path) -> list[Experiment]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Experiment(**e) for e in data.get("experiments", [])]


def bucket(experiment: str, ticket_id: str) -> int:
    """A stable number from 0 to 99 for this ticket in this experiment."""
    digest = hashlib.sha256(f"{experiment}:{ticket_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def assign(experiments: list[Experiment], route: str, ticket_id: str) -> tuple[Experiment | None, str]:
    """Return (experiment, arm). arm is "control", "candidate" or "none"."""
    for e in experiments:
        if e.enabled and e.route == route:
            return e, ("candidate" if bucket(e.name, ticket_id) < e.percent else "control")
    return None, "none"
