"""Any OpenAI-compatible server: llama.cpp (llama-server), Ollama, vLLM, LM Studio.

Practice: "Add a fallback model for when your main provider goes down."

A local model on your own GPU is a good fallback: it has no shared outage
with the cloud provider, and each token costs nothing extra. It is weaker,
so the evals tell you which tickets it can handle.

This class converts the Anthropic-style messages to the OpenAI chat format
and back. Thinking blocks are dropped: other providers do not read them.
"""

from __future__ import annotations

import json

import httpx

from ..costs import Usage
from ..retry import PermanentError, TransientError
from .base import DeltaFn, ModelRequest, ModelResponse


def to_openai(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                     for b in blocks if b["type"] == "tool_use"]
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            texts = []
            for b in blocks:
                if b["type"] == "tool_result":
                    content = b["content"] if isinstance(b["content"], str) else json.dumps(b["content"])
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": ("ERROR: " + content) if b.get("is_error") else content})
                elif b["type"] == "text":
                    texts.append(b["text"])
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in tools]


class OpenAICompatProvider:
    name = "local"

    def __init__(self, base_url: str, timeout_s: float = 120.0, api_key: str | None = None,
                 temperature: float | None = 0.0):
        # Open models accept a temperature. 0 makes eval runs repeatable. (The newest Claude
        # models reject this parameter, so the Anthropic provider never sends it.)
        self.temperature = temperature
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def complete(self, req: ModelRequest, on_delta: DeltaFn | None = None) -> ModelResponse:
        body = {"model": req.model, "messages": to_openai(req.system, req.messages),
                "tools": to_openai_tools(req.tools), "tool_choice": "required" if req.force_tool else "auto",
                "max_tokens": req.max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers,
                           timeout=self.timeout_s)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"local model: {type(exc).__name__}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"local model: HTTP {r.status_code}")
        if r.status_code != 200:
            raise PermanentError(f"local model: HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        content: list[dict] = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
            if on_delta:
                on_delta("text", msg["content"])
        for i, call in enumerate(msg.get("tool_calls") or []):
            fn = call["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": fn.get("arguments", "")[:500]}  # the schema check will reject it
            content.append({"type": "tool_use", "id": call.get("id") or f"call_{i}", "name": fn["name"],
                             "input": args if isinstance(args, dict) else {"__value__": args}})
            if fn["name"] == "finish" and on_delta and isinstance(args, dict) and isinstance(args.get("reply"), str):
                on_delta("reply", args["reply"])
        reason = {"tool_calls": "tool_use", "length": "max_tokens"}.get(choice.get("finish_reason"), "end_turn")
        if any(b["type"] == "tool_use" for b in content):
            reason = "tool_use"
        u = data.get("usage") or {}
        usage = Usage(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0))
        return ModelResponse(content, reason, usage, req.model, self.name)
