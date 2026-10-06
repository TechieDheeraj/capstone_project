import subprocess

from refund_desk.mcp_client import McpTools
from refund_desk.providers.openai_compat import to_openai
from refund_desk.sandbox import run_module


def test_mcp_server_serves_read_only_tools(settings):
    m = McpTools.start(settings)
    try:
        assert set(m.tool_names) == {"search_policy", "track_shipment", "order_status"}
        assert m.call("search_policy", {"query": "missing item"})["hits"][0]["id"] == "refunds#missing"
        assert "customer" not in str(m.call("order_status", {"order_id": "A-1001"}))
    finally:
        m.close()


def test_conversion_to_the_openai_format():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"},
                                              {"type": "tool_use", "id": "t1", "name": "lookup_order",
                                               "input": {"order_id": "A-1001"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom",
                                          "is_error": True}]}]
    out = to_openai("sys", msgs)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"]["name"] == "lookup_order"
    assert out[3]["content"] == "ERROR: boom"


def test_sandbox_child_gets_no_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    seen = {}
    real = subprocess.Popen

    def spy(*args, **kwargs):
        seen.update(kwargs["env"])
        return real(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", spy)
    out = run_module("refund_desk.portal_reader", {"portal_url": "http://example.org/x", "tracking_no": "X",
                                                   "engine": "http", "allowed_hosts": ["127.0.0.1"]})
    assert "ANTHROPIC_API_KEY" not in seen
    assert out["ok"] is False and "allowlist" in out["error"]     # the host check works inside the sandbox too
