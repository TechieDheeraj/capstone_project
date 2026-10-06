"""Prices and cost per task.

Practice: "Track cost per task, not cost per token."

Price per token tells you little. The business question is: "What does one
resolved ticket cost?" That number includes every model call, every retry,
the router, the failed runs and the runs that a human had to finish.

PRICES are EXAMPLE values in US dollars per 1 million tokens. Prices change.
Copy the current values from the provider's pricing page before you trust a
cost report. Cache reads are much cheaper than normal input; cache writes
cost a little more than normal input.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# model id -> (input, output, cache write, cache read) in USD per 1M tokens.
# EXAMPLE PRICES (check the current price list): Haiku 4.5 is $1 / $5.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
    "claude-sonnet-5": (3.00, 15.00, 3.75, 0.30),
    "claude-opus-5-5": (5.00, 25.00, 6.25, 0.50),
    # A local model has no per-token price. You pay for the hardware instead.
    "local": (0.0, 0.0, 0.0, 0.0),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write_tokens + self.cache_read_tokens

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens

    def to_dict(self) -> dict:
        return asdict(self)


def price_for(model: str) -> tuple[float, float, float, float]:
    if model in PRICES:
        return PRICES[model]
    for key, value in PRICES.items():  # "claude-haiku-4-5-20251001" -> "claude-haiku-4-5"
        if model.startswith(key):
            return value
    return PRICES["local"]


def cost_usd(model: str, usage: Usage) -> float:
    p_in, p_out, p_cw, p_cr = price_for(model)
    return (usage.input_tokens * p_in + usage.output_tokens * p_out
            + usage.cache_write_tokens * p_cw + usage.cache_read_tokens * p_cr) / 1_000_000


@dataclass
class Spend:
    """What one run has used so far. The budget checks this object."""
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    model_calls: int = 0
    cache_hits: int = 0
    active_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {"usage": self.usage.to_dict(), "cost_usd": round(self.cost_usd, 6),
                "model_calls": self.model_calls, "cache_hits": self.cache_hits,
                "active_seconds": round(self.active_seconds, 3)}

    @classmethod
    def from_dict(cls, d: dict) -> "Spend":
        return cls(usage=Usage(**d.get("usage", {})), cost_usd=d.get("cost_usd", 0.0),
                   model_calls=d.get("model_calls", 0), cache_hits=d.get("cache_hits", 0),
                   active_seconds=d.get("active_seconds", 0.0))
