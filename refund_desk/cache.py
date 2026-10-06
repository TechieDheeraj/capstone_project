"""Cache repeat queries so they cost almost nothing.

Practice: "Cache repeat queries so they cost almost nothing."

Three caches, three jobs:

  1. Response cache (this file). Key = a hash of the full model request:
     model, system prompt, tools and messages. Same request -> same answer,
     returned from SQLite in about a millisecond, for $0. It also makes a
     resumed run cheap: the steps before the crash are cache hits.
  2. Tool cache (this file). Read-only tools such as track_shipment keep
     their result for a few minutes. Tools that change things are never cached.
  3. Prompt caching at the provider (providers/anthropic_provider.py). The
     provider keeps the long, fixed start of the prompt (system + tools)
     ready, and bills those tokens at a much lower "cache read" price.

Only exact matches hit. A "semantic" cache (similar question -> same answer)
saves more, but it can give a wrong answer to a different question. Use it
only for FAQ-type questions, and measure it with evals first.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .store import Store


def request_key(model: str, system: str, tools: list[dict], messages: list[dict], extra: dict | None = None) -> str:
    blob = json.dumps({"model": model, "system": system, "tools": tools, "messages": messages,
                       "extra": extra or {}}, sort_keys=True, ensure_ascii=False)
    return "llm:" + hashlib.sha256(blob.encode()).hexdigest()


def tool_key(tool: str, args: dict) -> str:
    blob = json.dumps({"tool": tool, "args": args}, sort_keys=True)
    return "tool:" + hashlib.sha256(blob.encode()).hexdigest()


class Cache:
    def __init__(self, store: Store, enabled: bool = True, ttl_s: float = 86400):
        self.store, self.enabled, self.ttl_s = store, enabled, ttl_s

    def get(self, key: str) -> Any | None:
        return self.store.cache_get(key) if self.enabled else None

    def put(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        if self.enabled:
            self.store.cache_put(key, value, ttl_s if ttl_s is not None else self.ttl_s)
