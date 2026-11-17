"""Shopify stores: /products.json catalog, bestseller collections, homepage order."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from ..config import Settings
from ..http import Fetcher, RateLimited
from ..util import num, text_of

log = logging.getLogger(__name__)

BESTSELLER_RE = re.compile(r"best|top[-\s]?sell|popular|favou?rite|fave|trending|most[-\s]?loved|customer[-\s]?pick", re.I)
FEATURED_RE = re.compile(r"featured|frontpage|^new|new-arrival|just-in|latest", re.I)
CATALOG_RE = re.compile(r"^(all|shop-all|all-products|shop|products)$", re.I)
PRODUCT_LINK_RE =re.compile(r'href="(?:https?://[^"/]+)?(?:/[a-z]{2}(?:-[a-z]{2})?)?/(?:collections/[^"/]+/)?products/([^"?#/]+)', re.I)


def _tags(v) -> str:
    if isinstance(v, list):
        return ", ".join(v)
    return v or ""


def normalize(p: dict, base: str, currency: str, cents: bool = False) -> dict:
    """Works for both /products.json items and /products/<handle>.js responses."""
    div = 100.0 if cents else 1.0

    def price(v):
        n = num(v)
        return round(n / div, 2) if n is not None else None

    variants = []
    for v in p.get("variants") or []:
        variants.append({
            "variant_id": str(v.get("id")),
            "title": v.get("title"),
            "sku": v.get("sku") or "",
            "price": price(v.get("price")),
            "compare_at_price": price(v.get("compare_at_price")),
            "available": v.get("available"),
            "option1": v.get("option1"), "option2": v.get("option2"), "option3": v.get("option3"),
        })
    images = []
    for i, img in enumerate(p.get("images") or [], start=1):
        src = img.get("src") if isinstance(img, dict) else img
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        images.append({"position": i, "src": src,
                       "width": img.get("width") if isinstance(img, dict) else None,
                       "height": img.get("height") if isinstance(img, dict) else None})
    prices = [v["price"] for v in variants if v["price"] is not None]
    compares = [v["compare_at_price"] for v in variants if v["compare_at_price"]]
    return {
        "product_id": str(p.get("id")),
        "handle": p.get("handle") or "",
        "title": p.get("title") or "",
        "url": f"{base}/products/{p.get('handle')}",
        "product_type": p.get("product_type") or p.get("type") or "",
        "vendor": p.get("vendor") or "",
        "tags": _tags(p.get("tags")),
        "description": text_of(p.get("body_html") or p.get("description")),
        "created_at": p.get("created_at") or "",
        "published_at": p.get("published_at") or "",
        "updated_at": p.get("updated_at") or "",
        "currency": currency,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "compare_at_max": max(compares) if compares else None,
        "on_sale": any(v["compare_at_price"] and v["price"] is not None and v["compare_at_price"] > v["price"]
                       for v in variants),
        "available": any(bool(v["available"]) for v in variants) if variants else None,
        "variant_count": len(variants),
        "image_count": len(images),
        "variants": variants,
        "images": images,
        "reviews": [],
        "rating": None, "review_count": None, "review_app": None,
    }


async def _paged_products(f: Fetcher, url: str, cap: int | None, max_pages: int = 40) -> list[dict] | None:
    items: list[dict] = []
    for page in range(1, max_pages + 1):
        r = await f.get(f"{url}?limit=250&page={page}")
        if r is None or r.status_code != 200:
            return items if page > 1 else None
        try:
            batch = r.json().get("products", [])
        except ValueError:
            return items if page > 1 else None
        items += batch
        if len(batch) < 250 or (cap and len(items) >= cap):
            break
    return items


async def _sitemap_handles(f: Fetcher, base: str) -> list[str]:
    r = await f.get(f"{base}/sitemap.xml")
    if r is None or r.status_code != 200:
        return []
    handles: list[str] = []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    child_urls = [e.text for e in root.iter() if e.tag.endswith("loc") and e.text and "sitemap_products" in e.text]
    for cu in child_urls:
        cr = await f.get(cu)
        if cr is None or cr.status_code != 200:
            continue
        try:
            croot = ET.fromstring(cr.content)
        except ET.ParseError:
            continue
        for e in croot.iter():
            if e.tag.endswith("loc") and e.text and "/products/" in e.text:
                handles.append(e.text.rstrip("/").rsplit("/products/", 1)[1])
    return handles


async def crawl(f: Fetcher, s: Settings, base: str, home_html: str) -> tuple[list[dict], list[tuple], dict]:
    m = re.search(r'Shopify\.currency\s*=\s*\{"active":"(\w+)"', home_html)
    currency = m.group(1) if m else "USD"
    m = re.search(r'Shopify\.shop\s*=\s*"([^"]+)"', home_html)
    shop = m.group(1) if m else None
    cap = s.max_products_per_store

    raw = await _paged_products(f, f"{base}/products.json", cap)
    if raw is None and urlsplit(base).netloc.lower() in f.limited_hosts:
        raise RateLimited(f"{base} answered 429 to /products.json after retries")
    products: list[dict]
    if raw:
        products = [normalize(p, base, currency) for p in raw]
    else:
        # Fallback: product sitemap + /products/<handle>.js (prices in cents)
        log.info("%s: /products.json unavailable, using sitemap fallback", base)
        products = []
        for h in (await _sitemap_handles(f, base))[: cap or None]:
            r = await f.get(f"{base}/products/{h}.js")
            if r is not None and r.status_code == 200:
                try:
                    products.append(normalize(r.json(), base, currency, cents=True))
                except ValueError:
                    pass
    if cap:
        products = products[:cap]

    # ---- rankings ----
    rankings: list[tuple] = []
    by_handle = {p["handle"]: p["product_id"] for p in products}
    seen: set[str] = set()
    pos = 0
    for h in PRODUCT_LINK_RE.findall(home_html):
        if h in by_handle and h not in seen:
            seen.add(h)
            pos += 1
            rankings.append(("homepage", by_handle[h], pos))

    collections = await _paged_collections(f, base)
    picks: list[tuple[str, str]] = []  # (kind, handle)
    for c in collections:
        label = f"{c.get('handle', '')} {c.get('title', '')}"
        if BESTSELLER_RE.search(label):
            picks.append(("bestseller", c["handle"]))
    picks = picks[: s.max_bestseller_collections]
    picks += [("featured", c["handle"]) for c in collections if FEATURED_RE.search(c.get("handle", ""))][:2]
    picks += [("catalog", c["handle"]) for c in collections if CATALOG_RE.match(c.get("handle", ""))][:1]
    for kind, handle in picks:
        items = await _paged_products(f, f"{base}/collections/{handle}/products.json", None, max_pages=2) or []
        for i, p in enumerate(items, start=1):
            rankings.append((f"{kind}:{handle}", str(p.get("id")), i))

    meta = {"currency": currency, "shop": shop, "collections": len(collections),
            "ranking_collections": ", ".join(f"{k}:{h}" for k, h in picks)}
    return products, rankings, meta


async def _paged_collections(f: Fetcher, base: str) -> list[dict]:
    out: list[dict] = []
    for page in range(1, 6):
        r = await f.get(f"{base}/collections.json?limit=250&page={page}")
        if r is None or r.status_code != 200:
            break
        try:
            batch = r.json().get("collections", [])
        except ValueError:
            break
        out += batch
        if len(batch) < 250:
            break
    return out
