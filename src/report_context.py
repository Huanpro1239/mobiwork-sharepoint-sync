"""Catalogue snapshots scoped to a single coordinated report run.

Successful reads are shared in memory. Failures are never cached, and callers
receive independent copies so mappings from one report cannot change another.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from mobiwork import MobiWorkClient


CATALOGUES = {
    "customers": (("customer_catalogue_start_date", "customer_address_provinces"),
                  ("customer_catalogue", "customer_catalogue_audit")),
    "products": (("products", "unit_conversions"),
                 ("products", "unit_conversions", "product_catalogue_count")),
    "employees": (("employees", "npp_units"),
                  ("employees", "employees_explicit", "npp_units", "supervisor_candidates",
                   "sales_structure_audit")),
}


@dataclass
class CatalogueCache:
    _client: MobiWorkClient | None = field(default=None, repr=False)
    _values: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    loads: dict[str, int] = field(default_factory=dict, init=False)

    @property
    def client(self) -> MobiWorkClient:
        if self._client is None:
            self._client = MobiWorkClient.from_env()
        return self._client

    def get(self, key: str, loader: Callable[[], Any]) -> Any:
        if key not in self._values:
            self._values[key] = copy.deepcopy(loader())
            name = key.split(":", 1)[0]
            self.loads[name] = self.loads.get(name, 0) + 1
        return copy.deepcopy(self._values[key])

    def enrich(self, kind: str, cfg: dict[str, Any], loader: Callable) -> dict[str, Any]:
        dependencies, outputs = CATALOGUES[kind]
        defaults = {"customer_catalogue_start_date": "01/01/1900"}
        key = kind + ":" + json.dumps({name: cfg.get(name, defaults.get(name, {})) for name in dependencies},
                                      sort_keys=True, ensure_ascii=False)

        def load() -> dict[str, Any]:
            enriched = loader(self.client, cfg)
            return {name: enriched[name] for name in outputs if name in enriched}

        return {**cfg, **self.get(key, load)}


def enrich_cached(cache: CatalogueCache | None, kind: str, client: MobiWorkClient,
                  cfg: dict[str, Any], loader: Callable) -> dict[str, Any]:
    return loader(client, cfg) if cache is None else cache.enrich(kind, cfg, loader)
