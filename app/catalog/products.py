"""Structured product lookup from ``Products.json`` (read-only).

The catalog is structured data, not RAG: exact product IDs and
deterministic keyword matching over name, category, description and
specs. The file is never modified. There is no returnability metadata
in the catalog, so nothing here can say whether a product is returnable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.db.connection import PROJECT_ROOT

PRODUCTS_JSON = PROJECT_ROOT / "data" / "source" / "Products.json"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"a", "an", "the", "is", "are", "of", "for", "do", "does", "have",
                   "has", "it", "this", "that", "what", "which", "how", "much", "does",
                   "and", "or", "to", "in", "on", "with", "me", "my", "i", "you", "your",
                   "can", "price", "cost", "spec", "specs", "specification", "details",
                   "about", "tell", "show", "any", "there"})


class CatalogError(Exception):
    """The catalog file is missing or invalid."""


@dataclass(frozen=True)
class ProductView:
    """A product as exposed to tools. ``price_inr`` is whole rupees."""

    product_id: str
    name: str
    category: str
    price_inr: int
    description: str
    specs: tuple[tuple[str, str], ...]
    stock_status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "category": self.category,
            "price": format_inr(self.price_inr),
            "description": self.description,
            "specs": {key: value for key, value in self.specs},
            "stock_status": self.stock_status,
        }


def rupees_to_paise(rupees: int) -> int:
    return rupees * 100


def format_inr(rupees: int) -> str:
    """Format whole rupees with the rupee sign, for example ``₹2,499`` (no float math)."""
    return f"₹{rupees:,}"


def format_paise(paise: int) -> str:
    """Format integer paise: whole rupees without decimals, else two."""
    rupees, rest = divmod(paise, 100)
    return f"₹{rupees:,}" if rest == 0 else f"₹{rupees:,}.{rest:02d}"


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP]


class Catalog:
    """Validated, read-only product list."""

    def __init__(self, products: list[ProductView]) -> None:
        self._products = {p.product_id: p for p in products}

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Catalog":
        """Load and validate the catalog. ``path`` defaults to ``PRODUCTS_JSON`` looked up at call time."""
        path = PRODUCTS_JSON if path is None else Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError("product catalog could not be read") from exc
        if not isinstance(raw, list) or not raw:
            raise CatalogError("product catalog must be a non-empty list")
        products: list[ProductView] = []
        seen: set[str] = set()
        for entry in raw:
            try:
                product_id = entry["id"]
                price = entry["price_inr"]
                if not isinstance(product_id, str) or product_id in seen:
                    raise ValueError("bad id")
                if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
                    raise ValueError("bad price")
                seen.add(product_id)
                specs = entry.get("specs") or {}
                products.append(ProductView(
                    product_id=product_id,
                    name=str(entry["name"]),
                    category=str(entry["category"]),
                    price_inr=price,
                    description=str(entry.get("description", "")),
                    specs=tuple((str(k), str(v)) for k, v in specs.items()),
                    stock_status=str(entry.get("stock_status", "unknown")),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise CatalogError("invalid product entry") from exc
        return cls(products)

    def get_product(self, product_id: str) -> Optional[ProductView]:
        if not isinstance(product_id, str):
            return None
        return self._products.get(product_id.strip().upper())

    def all_products(self) -> list[ProductView]:
        return sorted(self._products.values(), key=lambda p: p.product_id)

    def name_for(self, product_id: str) -> Optional[str]:
        product = self.get_product(product_id)
        return product.name if product else None

    def search_products(self, query: str, limit: int = 5) -> list[ProductView]:
        """Deterministic keyword match. Empty or stop-word-only queries match nothing."""
        wanted = set(_tokens(query))
        if not wanted:
            return []
        scored: list[tuple[int, str, ProductView]] = []
        for product in self._products.values():
            haystack = set(_tokens(" ".join(
                [product.name, product.category, product.description]
                + [f"{k} {v}" for k, v in product.specs]
            )))
            name_tokens = set(_tokens(product.name))
            score = 3 * len(wanted & name_tokens) + len(wanted & haystack)
            if score:
                scored.append((score, product.product_id, product))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    return Catalog.load()
