"""Claude through the Anthropic Messages API.

Practices here:
  * Stream responses: text and the reply e-mail reach the UI while the model
    writes them (messages.stream).
  * Prompt caching: cache_control marks the fixed start of the request
    (system prompt + tool definitions) and the conversation so far. The next
    step of the same run reads those tokens from the cache at a lower price.
  * Reasoning for hard tickets: the "large" route turns on extended thinking.
    Thinking blocks stay in the history, because the API needs them back in
    the next turn of a tool-use conversation.
  * Retries are OURS (retry.py), so the SDK's own retries are off
    (max_retries=0). One retry policy, one place, visible in the trace.

Note: the newest Claude models reject `temperature`, `top_p` and `top_k`.
This code does not send them. Stable behaviour comes from the prompt, the
strict tool schemas and the checks in code.
"""

from __future__ import annotations

import copy

from ..costs import Usage
from ..retry import PermanentError, TransientError
from .base import DeltaFn, ModelRequest, ModelResponse


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, timeout_s: float = 60.0):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install anthropic") from exc
        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=0, timeout=timeout_s)
        self.thinking_supported = True

    def _params(self, req: ModelRequest) -> dict:
        tools = copy.deepcopy(req.tools)
        if tools:
            tools[-1]["cache_control"] = {"type": "ephemeral"}      # caches system + all tools
        messages = copy.deepcopy(req.messages)
        if messages and isinstance(messages[-1]["content"], list) and messages[-1]["content"]:
            messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}  # caches the history
        params = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            "tools": tools,
            "messages": messages,
        }
        if req.thinking and self.thinking_supported:
            params["thinking"] = {"type": "adaptive"}
        elif req.force_tool:
            params["tool_choice"] = {"type": "any"}   # a forced tool choice does not work together with thinking
        return params

    def complete(self, req: ModelRequest, on_delta: DeltaFn | None = None) -> ModelResponse:
        a = self.anthropic
        params = self._params(req)
        try:
            reply_len = 0
            with self.client.messages.stream(**params) as stream:
                for event in stream:
                    if not on_delta:
                        continue
                    etype = getattr(event, "type", "")
                    if etype == "text":
                        on_delta("text", event.text)
                    elif etype == "input_json":
                        snap = getattr(event, "snapshot", None)
                        reply = snap.get("reply") if isinstance(snap, dict) else None
                        if isinstance(reply, str) and len(reply) > reply_len:
                            on_delta("reply", reply[reply_len:])
                            reply_len = len(reply)
                msg = stream.get_final_message()
        except (a.RateLimitError, a.APIConnectionError, a.APITimeoutError, a.InternalServerError) as exc:
            raise TransientError(f"anthropic: {type(exc).__name__}") from exc
        except a.BadRequestError as exc:
            if "thinking" in str(exc).lower() and "thinking" in params:
                # This model does not accept the thinking setting. Remember, and try once without it.
                self.thinking_supported = False
                return self.complete(req, on_delta)
            raise PermanentError(f"anthropic: bad request: {str(exc)[:300]}") from exc
        except a.APIStatusError as exc:
            if exc.status_code in (408, 409, 429, 529) or exc.status_code >= 500:
                raise TransientError(f"anthropic: HTTP {exc.status_code}") from exc
            raise PermanentError(f"anthropic: HTTP {exc.status_code}: {str(exc)[:300]}") from exc

        u = msg.usage
        usage = Usage(input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
        content = [_keep_known(b.model_dump(mode="json", exclude_none=True)) for b in msg.content]
        return ModelResponse(content, msg.stop_reason or "end_turn", usage, msg.model, self.name)


# Keep only the fields that the API accepts back in the next request.
_FIELDS = {"text": ("type", "text"), "tool_use": ("type", "id", "name", "input"),
           "thinking": ("type", "thinking", "signature"), "redacted_thinking": ("type", "data")}


def _keep_known(block: dict) -> dict:
    keys = _FIELDS.get(block.get("type", ""))
    return {k: block[k] for k in keys if k in block} if keys else block
