"""Settings. Every value comes from an environment variable with a safe default.

Why environment variables: the same code runs on a laptop, in CI and on a
server. Only the environment changes. Secrets (the API key) never go in code.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- Models -------------------------------------------------------------
    # "claude"  = Anthropic API (needs ANTHROPIC_API_KEY)
    # "local"   = any OpenAI-compatible server (llama.cpp, Ollama, vLLM)
    # "offline" = the scripted stand-in model (no key, no GPU; for tests and demos)
    model_mode: str = field(default_factory=lambda: _env(
        "MODEL_MODE", "claude" if os.environ.get("ANTHROPIC_API_KEY") else "offline"))
    small_model: str = field(default_factory=lambda: _env("SMALL_MODEL", "claude-haiku-4-5"))
    large_model: str = field(default_factory=lambda: _env("LARGE_MODEL", "claude-opus-5-5"))
    # Extended thinking for the "large" route. Set LARGE_THINKING=off to disable.
    large_thinking: str = field(default_factory=lambda: _env("LARGE_THINKING", "adaptive"))
    local_base_url: str = field(default_factory=lambda: _env("LOCAL_BASE_URL", "http://127.0.0.1:8080/v1"))
    local_model: str = field(default_factory=lambda: _env("LOCAL_MODEL", "qwen3-4b"))
    local_temperature: float = field(default_factory=lambda: _float("LOCAL_TEMPERATURE", 0.0))
    # Fallback when the main provider is down: "local", "offline" or "none".
    fallback: str = field(default_factory=lambda: _env("FALLBACK", "none"))
    max_output_tokens: int = field(default_factory=lambda: _int("MAX_OUTPUT_TOKENS", 4096))
    model_timeout_s: float = field(default_factory=lambda: _float("MODEL_TIMEOUT_S", 60.0))

    # --- Prompts ------------------------------------------------------------
    prompt_version: str = field(default_factory=lambda: _env("PROMPT_VERSION", "v3"))

    # --- Budgets for one run (one ticket) ------------------------------------
    max_steps: int = field(default_factory=lambda: _int("MAX_STEPS", 12))
    max_tokens: int = field(default_factory=lambda: _int("MAX_TOKENS", 120_000))
    max_seconds: float = field(default_factory=lambda: _float("MAX_SECONDS", 180.0))
    max_cost_usd: float = field(default_factory=lambda: _float("MAX_COST_USD", 0.50))

    # --- Caching --------------------------------------------------------------
    cache_enabled: bool = field(default_factory=lambda: _bool("CACHE_ENABLED", True))
    cache_ttl_s: int = field(default_factory=lambda: _int("CACHE_TTL_S", 24 * 3600))

    # --- Tools and the outside world -----------------------------------------
    carrier_base_url: str = field(default_factory=lambda: _env("CARRIER_BASE_URL", "http://127.0.0.1:8901"))
    # Hosts that tools may contact. Everything else is blocked (least privilege).
    allowed_hosts: tuple[str, ...] = field(default_factory=lambda: tuple(
        h.strip() for h in _env("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()))
    # "local" = tools run in this process; "mcp" = read tools come from the MCP server.
    tools_via: str = field(default_factory=lambda: _env("TOOLS_VIA", "local"))
    # "auto" = Playwright if installed, else an HTTP form reader.
    browser: str = field(default_factory=lambda: _env("BROWSER", "auto"))

    # --- Files ---------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "data"))))
    var_dir: Path = field(default_factory=lambda: Path(_env("VAR_DIR", str(ROOT / "var"))))
    prompts_dir: Path = field(default_factory=lambda: Path(_env("PROMPTS_DIR", str(ROOT / "prompts"))))
    experiments_file: Path = field(default_factory=lambda: Path(_env(
        "EXPERIMENTS_FILE", str(ROOT / "experiments.json"))))

    # --- Web server ----------------------------------------------------------
    # DEMO_MODE=1 is for a public link: offline model only, no real email, rate limits.
    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", False))
    rate_limit_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MIN", 10))

    # --- Test hooks ------------------------------------------------------------
    # Simulate a crash to show that runs resume: "model:3" = after the 3rd model call,
    # "refund" = right after a refund ran (before the checkpoint). Empty = off.
    crash_at: str = field(default_factory=lambda: _env("CRASH_AT", ""))

    @property
    def shop_db(self) -> Path:
        return self.var_dir / "shop.db"

    @property
    def app_db(self) -> Path:
        return self.var_dir / "app.db"

    def ensure_dirs(self) -> None:
        self.var_dir.mkdir(parents=True, exist_ok=True)


def today() -> dt.date:
    """The business date. REFUND_DESK_TODAY fixes it for repeatable tests."""
    fixed = os.environ.get("REFUND_DESK_TODAY")
    return dt.date.fromisoformat(fixed) if fixed else dt.date.today()


def load_settings() -> Settings:
    s = Settings()
    if s.demo_mode:
        # A public demo must never spend money or reach real customers.
        s.model_mode = "offline"
        s.fallback = "none"
    return s
