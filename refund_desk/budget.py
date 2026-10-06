"""Cap every run with token, time and cost budgets.

Practice: "Cap every run with token, time and cost budgets."

An agent decides its own next step, so it can also loop for ever: call a tool,
read the error, call it again. A budget is the hard stop. The harness checks
it before every model call. When a limit is reached, the run stops in a safe
state ("stopped_budget") and a human gets the ticket. It does not fail open.
"""

from __future__ import annotations

from dataclasses import dataclass

from .costs import Spend


@dataclass(frozen=True)
class Budget:
    max_steps: int
    max_tokens: int
    max_seconds: float
    max_cost_usd: float


class BudgetExceeded(Exception):
    def __init__(self, limit: str, used: float, cap: float):
        super().__init__(f"{limit} budget exceeded: {used:g} of {cap:g}")
        self.limit, self.used, self.cap = limit, used, cap


def check(budget: Budget, spend: Spend, steps: int, next_input_tokens: int = 0) -> None:
    """Raise BudgetExceeded if the next model call would break a limit."""
    if steps >= budget.max_steps:
        raise BudgetExceeded("steps", steps, budget.max_steps)
    projected = spend.usage.total + next_input_tokens
    if projected > budget.max_tokens:
        raise BudgetExceeded("tokens", projected, budget.max_tokens)
    if spend.active_seconds > budget.max_seconds:
        raise BudgetExceeded("seconds", round(spend.active_seconds, 1), budget.max_seconds)
    if spend.cost_usd > budget.max_cost_usd:
        raise BudgetExceeded("cost_usd", round(spend.cost_usd, 4), budget.max_cost_usd)


def estimate_tokens(text: str) -> int:
    """About 4 characters per token for English text. Good enough for a budget check."""
    return max(1, len(text) // 4)
