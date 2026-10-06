"""Use the MCP server's tools from the agent (TOOLS_VIA=mcp).

The MCP client is async. The agent loop is plain synchronous code. So the
client lives in its own thread with its own event loop, and call() sends
requests to it. One long-lived task opens the connection and closes it,
because an async connection must be closed by the task that opened it.

The child process gets a SMALL environment: no API key. The tool server
does not need one, so it does not get one (least privilege).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading

from mcp import Client, StdioServerParameters

from .config import ROOT, Settings
from .retry import PermanentError


class McpTools:
    def __init__(self, params: StdioServerParameters, timeout_s: float = 30.0):
        self.params, self.timeout_s = params, timeout_s
        self.tool_names: list[str] = []
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._queue: asyncio.Queue | None = None
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-client")
        self._thread.start()
        self._main_future = asyncio.run_coroutine_threadsafe(self._main(), self._loop)
        if not self._ready.wait(timeout=timeout_s) or self._error:
            raise RuntimeError(f"MCP server did not start: {self._error}")

    @classmethod
    def start(cls, s: Settings) -> "McpTools":
        keep = ("PATH", "LANG", "HOME", "VIRTUAL_ENV")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update({"PYTHONPATH": str(ROOT), "VAR_DIR": str(s.var_dir), "DATA_DIR": str(s.data_dir),
                    "CARRIER_BASE_URL": s.carrier_base_url, "ALLOWED_HOSTS": ",".join(s.allowed_hosts),
                    "BROWSER": s.browser})
        if "REFUND_DESK_TODAY" in os.environ:
            env["REFUND_DESK_TODAY"] = os.environ["REFUND_DESK_TODAY"]
        return cls(StdioServerParameters(command=sys.executable, args=["-m", "refund_desk.mcp_server"], env=env))

    async def _main(self) -> None:
        self._queue = asyncio.Queue()
        try:
            async with Client(self.params) as client:
                self.tool_names = [t.name for t in (await client.list_tools()).tools]
                self._ready.set()
                while True:
                    item = await self._queue.get()
                    if item is None:
                        break
                    name, args, fut = item
                    try:
                        fut.set_result(await client.call_tool(name, args))
                    except BaseException as exc:   # hand the error to the caller's thread
                        fut.set_exception(exc)
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    def call(self, name: str, args: dict) -> dict:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, args, fut))  # type: ignore[union-attr]
        result = fut.result(timeout=self.timeout_s)
        text = "".join(getattr(c, "text", "") for c in result.content or [])
        if result.is_error:
            raise PermanentError(f"MCP tool {name} failed: {text[:300]}")
        data = result.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return data if isinstance(data, dict) else json.loads(text)

    def close(self) -> None:
        if self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        try:
            self._main_future.result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
