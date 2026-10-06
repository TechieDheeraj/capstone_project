"""Run risky tool code in a sandbox with least-privilege access.

Practice: "Run every tool call in a sandbox with least-privilege access."

Least privilege in this project has four layers:

  1. Scopes:   each tool declares the scopes it needs ("orders:read"). A run
               gets a fixed grant. A tool outside the grant is refused.
  2. Data:     read tools use a read-only database connection (shop.py).
  3. Network:  tools may contact only hosts in ALLOWED_HOSTS.
  4. Process:  code that parses untrusted content (the browser fallback)
               runs in a child process (this file) with:
               - an EMPTY environment: no API keys, no tokens;
               - a CPU-time limit, a memory limit and a file-size limit;
               - a wall-clock timeout; the parent kills it after that;
               - a temporary working folder that is deleted afterwards.

What this sandbox does NOT do: block the network at the OS level, or hide
the file system. For that, run the worker in a container with no network
except the allowlist, a read-only file system and a non-root user (see the
Dockerfile), or in a microVM (gVisor, Firecracker).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 25.0
    cpu_s: int = 20
    memory_mb: int = 1024     # not applied to a real browser: Chromium reserves much virtual memory
    file_mb: int = 5


class SandboxError(Exception):
    pass


def _preexec(limits: Limits, apply_memory: bool):
    def apply() -> None:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_s, limits.cpu_s))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.file_mb << 20, limits.file_mb << 20))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        if apply_memory:
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_mb << 20, limits.memory_mb << 20))
        os.setsid()   # own process group: the parent can kill the whole tree
    return apply


def run_module(module: str, payload: dict, limits: Limits = Limits(), apply_memory: bool = True) -> dict:
    """Run `python -m module` with JSON in and JSON out, inside the limits."""
    # Only what the child needs. Note what is NOT here: ANTHROPIC_API_KEY and other tokens.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": str(ROOT), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    with tempfile.TemporaryDirectory(prefix="sandbox-") as tmp:
        env["HOME"] = tmp  # a browser needs a home folder; give it an empty, temporary one
        use_preexec = sys.platform != "win32"
        proc = subprocess.Popen(
            [sys.executable, "-m", module], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=tmp, env=env,
            preexec_fn=_preexec(limits, apply_memory) if use_preexec else None,
        )
        try:
            stdout, stderr = proc.communicate(json.dumps(payload), timeout=limits.timeout_s)
        except subprocess.TimeoutExpired as exc:
            # Kill the whole process group: a browser starts child processes of its own.
            if use_preexec:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.communicate()
            raise SandboxError(f"timeout after {limits.timeout_s} s") from exc
    if proc.returncode != 0:
        raise SandboxError(f"exit code {proc.returncode}: {stderr.strip()[-300:]}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SandboxError(f"bad output: {stdout[:200]!r}") from exc
