"""ModelHub: one door to every model, with cache, retries, fallback, cost and traces.

The agent never calls a provider directly. It calls hub.complete(). Thus the
cross-cutting rules live in ONE place:

    request ──► response cache ──hit──► $0, ~1 ms
                   │ miss
                   ▼
              primary provider ──(retry with backoff)──► ok
                   │ still failing (outage)
                   ▼
              fallback provider (local GPU model or offline rules)
                   │
                   ▼
              cost + tokens + latency ──► trace span, run spend
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from ..cache import Cache, request_key
from ..config import Settings
from ..costs import cost_usd
from ..retry import Attempt, RetryPolicy, TransientError, call_with_retry
from ..trace import Tracer
from .base import DeltaFn, ModelRequest, ModelResponse, Provider

MODEL_RETRY = RetryPolicy(max_attempts=3, base_delay=1.0, factor=2.0, max_delay=10.0)


class ProviderDown(Exception):
    """Every provider in the chain failed."""


@dataclass
class Result:
    response: ModelResponse
    cost_usd: float
    cached: bool
    provider: str
    fallback_used: bool


def make_provider(kind: str, s: Settings) -> Provider:
    if kind == "claude":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(timeout_s=s.model_timeout_s)
    if kind == "local":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(s.local_base_url, timeout_s=max(120.0, s.model_timeout_s),
                                    temperature=s.local_temperature)
    if kind == "offline":
        from .offline import OfflineModel
        return OfflineModel()
    raise ValueError(f"unknown model mode {kind!r}")


class ModelHub:
    def __init__(self, settings: Settings, cache: Cache, primary: Provider | None = None,
                 fallback: Provider | None | str = "auto"):
        self.s, self.cache = settings, cache
        self.primary = primary or make_provider(settings.model_mode, settings)
        if fallback == "auto":
            fallback = None if settings.fallback in ("none", "", settings.model_mode) else \
                make_provider(settings.fallback, settings)
        self.fallback = fallback

    def model_for(self, tier: str) -> str:
        """Which model id serves this route. Local mode has one model for both tiers."""
        if self.s.model_mode == "local":
            return self.s.local_model
        return self.s.large_model if tier == "large" else self.s.small_model

    def _fallback_model(self) -> str:
        return self.s.local_model if getattr(self.fallback, "name", "") == "local" else self.s.small_model

    def complete(self, req: ModelRequest, tracer: Tracer, on_delta: DeltaFn | None = None,
                 route: str | None = None) -> Result:
        key = request_key(req.model, req.system, req.tools, req.messages, {"thinking": req.thinking, "force_tool": req.force_tool})
        with tracer.span("model", req.model, route=route, provider=self.primary.name) as span:
            a = span["attrs"]
            hit = self.cache.get(key)
            if hit is not None:
                resp = ModelResponse.from_cache(hit)
                a.update(cache="hit", tokens_in=0, tokens_out=0, cost_usd=0.0)
                if on_delta:   # replay the reply so the UI still streams
                    for b in resp.content:
                        if b["type"] == "tool_use" and b["name"] == "finish":
                            on_delta("reply", b["input"].get("reply", ""))
                return Result(resp, 0.0, True, resp.provider, False)

            chain = [(self.primary, req)]
            if self.fallback is not None:
                fb = copy.copy(req)
                fb.model, fb.thinking = self._fallback_model(), False
                chain.append((self.fallback, fb))
            retries: list[dict] = []
            last_error: Exception | None = None
            for i, (provider, r) in enumerate(chain):
                def note(at: Attempt, p: str = provider.name) -> None:
                    retries.append({"provider": p, "attempt": at.number, "error": at.error, "wait_s": round(at.waited, 2)})
                    tracer.emit({"type": "retry", "name": p, "attrs": retries[-1]})
                try:
                    resp = call_with_retry(lambda: provider.complete(r, on_delta), MODEL_RETRY, on_retry=note)
                except TransientError as exc:
                    last_error = exc
                    tracer.decision("provider_down", provider=provider.name, error=str(exc)[:200])
                    continue
                cost = cost_usd(resp.model, resp.usage)
                a.update(cache="miss", provider=provider.name, model=resp.model, tokens_in=resp.usage.input_tokens,
                         tokens_out=resp.usage.output_tokens, cache_read=resp.usage.cache_read_tokens,
                         cache_write=resp.usage.cache_write_tokens, cost_usd=round(cost, 6),
                         stop=resp.stop_reason, simulated=resp.simulated, retries=len(retries) or None,
                         fallback=i > 0 or None)
                if i > 0:
                    tracer.decision("fallback_used", provider=provider.name, model=resp.model)
                if i == 0:   # never cache a degraded answer from the fallback
                    self.cache.put(key, resp.to_cache())
                return Result(resp, cost, False, provider.name, i > 0)
            a["error"] = f"all providers failed: {last_error}"
            raise ProviderDown(str(last_error))
