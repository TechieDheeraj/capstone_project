"""Build all the parts from the settings, in one place (simple dependency injection)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent import Agent, Approver, Listener
from .cache import Cache
from .carrier import CarrierClient
from .config import Settings, load_settings
from .experiments import load_experiments
from .policy import PolicyIndex, load_sections
from .providers import ModelHub
from .shop import Shop, seed
from .store import Store
from .tools import Toolbox


@dataclass
class App:
    settings: Settings
    store: Store
    shop: Shop
    cache: Cache
    toolbox: Toolbox
    hub: ModelHub
    remote: Any = None

    def agent(self, listener: Listener | None = None, approver: Approver | None = None) -> Agent:
        return Agent(self.settings, self.store, self.shop, self.toolbox, self.hub,
                     load_experiments(self.settings.experiments_file), listener, approver)


def build(settings: Settings | None = None, hub: ModelHub | None = None) -> App:
    s = settings or load_settings()
    s.ensure_dirs()
    seed(s.shop_db)                       # creates the demo shop on the first run
    store = Store(s.app_db)
    cache = Cache(store, s.cache_enabled, s.cache_ttl_s)
    shop = Shop(s.shop_db)
    remote = None
    if s.tools_via == "mcp":
        from .mcp_client import McpTools
        remote = McpTools.start(s)
    toolbox = Toolbox(s, shop, PolicyIndex(load_sections(s.data_dir / "policy")), CarrierClient(s), cache, remote)
    return App(s, store, shop, cache, toolbox, hub or ModelHub(s, cache), remote)
