"""Crawl one store end to end: detect platform -> catalog -> reviews -> rankings."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .adapters import generic, shopify, woocommerce
from .config import Settings
from .http import Fetcher, RateLimited
from . import reviews as rv
from .util import base_of

log = logging.getLogger(__name__)

UNSUPPORTED_HOSTS = ("shopmy.us", "coreli.ai")


@dataclass
class StoreResult:
    domain: str
    name: str
    url: str
    base_url: str = ""
    platform: str = ""
    status: str = "ok"
    error: str = ""
    review_app: str = ""
    currency: str = ""
    notes: str = ""
    seconds: float = 0.0
    products: list[dict] = field(default_factory=list)
    rankings: list[tuple] = field(default_factory=list)


def detect_platform(html: str, headers) -> str:
    h = html.lower()
    hd = {k.lower(): v.lower() for k, v in headers.items()}
    if "x-shopid" in hd or "x-shopify-stage" in hd or "shopify" in hd.get("powered-by", ""):
        return "shopify"
    if "x-wix-request-id" in hd or "static.wixstatic.com" in h or "wix.com website builder" in h:
        return "wix"
    if "squarespace" in hd.get("server", "") or "static1.squarespace.com" in h:
        return "squarespace"
    if "cdn.shopify.com" in h or "shopify.theme" in h or "/cdn/shop/" in h:
        return "shopify"
    if "woocommerce" in h:
        return "woocommerce"
    if "bigcommerce" in h:
        return "bigcommerce"
    return "other"


async def _shopify_reviews(f: Fetcher, s: Settings, products: list[dict], shop: str | None) -> str:
    """Visit product pages to read rating summary + reviews. Returns review app name."""
    app: str | None = None
    probed = 0
    for p in products:
        r = await f.get(p["url"])
        if r is None or r.status_code != 200:
            continue
        html = r.text
        if app is None:
            app = rv.detect_review_app(html)
            probed += 1
        info = await rv.collect(f, html, app, p["product_id"], shop, s.max_reviews_per_product, shopify=True)
        p.update(rating=info["rating"], review_count=info["review_count"],
                 reviews=info["reviews"], review_app=app)
        # No review widget and no structured rating on the first pages -> stop spending requests
        if app is None and probed >= 2 and not any(x["review_count"] for x in products[:probed]):
            return "none"
    return app or "none"


async def crawl_store(f: Fetcher, s: Settings, store: dict) -> StoreResult:
    t0 = time.monotonic()
    res = StoreResult(domain=store["domain"], name=store["name"], url=store["url"])
    try:
        if any(h in store["domain"] for h in UNSUPPORTED_HOSTS):
            res.status, res.platform, res.notes = "skipped", "aggregator", "curator page, not a store"
            return res
        home = await f.get(store["url"])
        if home is None:
            res.status, res.error = "blocked_or_unreachable", "robots.txt disallow or network error"
            return res
        if home.status_code >= 400:
            res.status, res.error = "http_error", f"HTTP {home.status_code}"
            return res
        html = home.text
        res.base_url = base_of(str(home.url))
        res.platform = detect_platform(html, home.headers)

        if res.platform == "shopify":
            products, rankings, meta = await shopify.crawl(f, s, res.base_url, html)
            res.currency = meta["currency"]
            res.notes = f"ranking collections: {meta['ranking_collections'] or '-'}"
            if s.fetch_reviews and products:
                res.review_app = await _shopify_reviews(f, s, products, meta["shop"])
        else:
            out = None
            if res.platform == "woocommerce":
                out = await woocommerce.crawl(f, s, res.base_url)
                if out:
                    res.review_app = "woocommerce"
            if out is None:
                out = await generic.crawl(f, s, res.base_url, html, res.platform)
                res.review_app = out[2].get("review_app") or "none"
            products, rankings, _ = out
            res.currency = products[0]["currency"] if products else ""

        res.products, res.rankings = products, rankings
        if not products:
            res.status = "no_products"
    except RateLimited as e:
        res.status, res.error = "rate_limited", str(e)
    except Exception as e:
        log.exception("%s failed", store["domain"])
        res.status, res.error = "error", repr(e)[:500]
    finally:
        res.seconds = round(time.monotonic() - t0, 1)
    return res
