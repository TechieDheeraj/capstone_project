"""The model interface. Every provider speaks this one format.

The format is the Anthropic Messages format: a list of messages, each with a
list of content blocks (text, tool_use, tool_result, thinking). Other
providers convert to and from it. Thus the agent loop never changes when you
switch the model, and a run can even switch provider in the middle (fallback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..costs import Usage

# on_delta(kind, text): kind is "text" (model prose) or "reply" (the e-mail, while it is written)
DeltaFn = Callable[[str, str], None]


@dataclass
class ModelRequest:
    model: str
    system: str
    messages: list[dict]
    tools: list[dict]
    max_tokens: int = 4096
    thinking: bool = False
    force_tool: bool = False         # the model MUST call a tool (used after it answered in plain text)


@dataclass
class ModelResponse:
    content: list[dict]
    stop_reason: str                 # "end_turn" | "tool_use" | "max_tokens"
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    simulated: bool = False          # True for the offline model: tokens and cost are estimates

    def to_cache(self) -> dict:
        return {"content": self.content, "stop_reason": self.stop_reason, "usage": self.usage.to_dict(),
                "model": self.model, "provider": self.provider, "simulated": self.simulated}

    @classmethod
    def from_cache(cls, d: dict) -> "ModelResponse":
        return cls(d["content"], d["stop_reason"], Usage(**d["usage"]), d["model"], d["provider"], d["simulated"])


class Provider(Protocol):
    name: str

    def complete(self, req: ModelRequest, on_delta: DeltaFn | None = None) -> ModelResponse: ...
