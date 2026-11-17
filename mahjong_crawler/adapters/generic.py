"""Non-Shopify sites (Squarespace, Wix, WooCommerce fallback, custom):
discover product URLs from sitemaps, then read JSON-LD / Squarespace JSON per page."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..config import Settings
from ..http import Fetcher
from .. import reviews as rv
from ..util import abs_url, jsonld_products, num, short_hash, soup, text_of

log = logging.getLogger(__name__)

PRODUCT_URL_RE = re.compile(r"/product-page/|/products?/[^/]+/?$|/(shop|store)/(p/)?[^/]+/?$|/p/[^/]+/?$", re.I)


async def _sitemap_urls(f: Fetcher, base: str, sitemaps: list[str]) -> list[str]:
    queue = list(dict.fromkeys(sitemaps or [f"{base}/sitemap.xml"]))
    product_urls: list[str] = []
    other_urls: list[str] = []
    visited: set[str] = set()
    while queue and len(visited) < 25:
        sm = queue.pop(0)
        if sm in visited:
            continue
        visited.add(sm)
        r = await f.get(sm)
        if r is None or r.status_code != 200:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        locs = [e.text.strip() for e in root.iter() if e.tag.endswith("}loc") or e.tag == "loc" if e.text]
        if root.tag.endswith("sitemapindex"):
            # prefer product sitemaps, but keep others (Squarespace has one flat sitemap)
            prod = [u for u in locs if "product" in u.lower()]
            queue = prod + queue if prod else queue + locs
            if prod:
                queue = [q for q in queue if "product" in q.lower()]
            continue
        is_product_map = "product" in sm.lower()
        for u in locs:
            if urlsplit(u).netloc.lower().removeprefix("www.") != urlsplit(base).netloc.lower().removeprefix("www."):
                continue
            (product_urls if is_product_map or PRODUCT_URL_RE.search(urlsplit(u).path) else other_urls).append(u)
    return list(dict.fromkeys(product_urls))


def _offers(offers) -> list[dict]:
    if isinstance(offers, dict):
        if offers.get("@type") == "AggregateOffer" and offers.get("offers"):
            return _offers(offers["offers"])
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _images(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    out = []
    for x in v or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict) and (x.get("url") or x.get("contentUrl")):
            out.append(x.get("url") or x.get("contentUrl"))
    return out


def from_jsonld(page_url: str, html: str, currency_default: str = "USD") -> dict | None:
    prods = jsonld_products(html)
    if not prods:
        return None
    p = prods[0]
    variants, imgs = [], _images(p.get("image"))
    variant_nodes = p.get("hasVariant") if isinstance(p.get("hasVariant"), list) else [p]
    currency = currency_default
    for vn in variant_nodes:
        for o in _offers(vn.get("offers")):
            price = num(o.get("price") or o.get("lowPrice"))
            currency = o.get("priceCurrency") or currency
            variants.append({
                "variant_id": str(vn.get("sku") or o.get("sku") or short_hash(page_url, len(variants))),
                "title": vn.get("name") if vn is not p else "",
                "sku": vn.get("sku") or o.get("sku") or "",
                "price": price,
                "compare_at_price": num(o.get("highPrice")) if o.get("@type") == "AggregateOffer" else None,
                "available": ("InStock" in str(o.get("availability", ""))) if o.get("availability") else None,
                "option1": None, "option2": None, "option3": None,
            })
        if vn is not p:
            imgs += _images(vn.get("image"))
    prices = [v["price"] for v in variants if v["price"] is not None]
    brand = p.get("brand")
    imgs = list(dict.fromkeys(imgs))
    return {
        "product_id": str(p.get("productID") or p.get("sku") or short_hash(page_url)),
        "handle": urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1],
        "title": p.get("name") or "",
        "url": page_url,
        "product_type": p.get("category") or "",
        "vendor": brand.get("name", "") if isinstance(brand, dict) else (brand or ""),
        "tags": "",
        "description": text_of(p.get("description")),
        "created_at": "", "published_at": "", "updated_at": "",
        "currency": currency,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "compare_at_max": None,
        "on_sale": False,
        "available": any(bool(v["available"]) for v in variants) if variants else None,
        "variant_count": len(variants),
        "image_count": len(imgs),
        "variants": variants,
        "images": [{"position": i, "src": abs_url(page_url, u), "width": None, "height": None}
                   for i, u in enumerate(imgs, start=1)],
        "reviews": [], "rating": None, "review_count": None, "review_app": None,
    }


def from_squarespace(page_url: str, data: dict) -> dict | None:
    item = data.get("item")
    if not isinstance(item, dict):
        return None
    sc = item.get("structuredContent") or {}
    raw_variants = item.get("variants") or sc.get("variants") or []
    variants = []
    currency = "USD"
    for v in raw_variants:
        pm = v.get("priceMoney") or {}
        spm = v.get("salePriceMoney") or {}
        price = num(pm.get("value")) if pm else (num(v.get("price")) / 100 if num(v.get("price")) is not None else None)
        sale = num(spm.get("value")) if spm else (num(v.get("salePrice")) / 100 if num(v.get("salePrice")) else None)
        currency = pm.get("currency") or currency
        on_sale = bool(v.get("onSale"))
        stock = v.get("stock") or {}
        available = bool(v.get("unlimited") or stock.get("unlimited") or (v.get("qtyInStock") or stock.get("quantity") or 0) > 0)
        attrs = v.get("attributes") or {}
        variants.append({
            "variant_id": str(v.get("id")),
            "title": " / ".join(str(x) for x in attrs.values()),
            "sku": v.get("sku") or "",
            "price": sale if on_sale and sale else price,
            "compare_at_price": price if on_sale and sale else None,
            "available": available,
            "option1": None, "option2": None, "option3": None,
        })
    imgs = [x.get("assetUrl") for x in item.get("items") or [] if isinstance(x, dict) and x.get("assetUrl")]
    if not imgs and item.get("assetUrl"):
        imgs = [item["assetUrl"]]
    prices = [v["price"] for v in variants if v["price"] is not None]
    def ms(v):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat() if v else ""

    return {
        "product_id": str(item.get("id")),
        "handle": item.get("urlId") or "",
        "title": item.get("title") or "",
        "url": page_url,
        "product_type": str(sc.get("productType") or ""),
        "vendor": "",
        "tags": ", ".join((item.get("tags") or []) + (item.get("categories") or [])),
        "description": text_of(item.get("body") or item.get("excerpt")),
        "created_at": ms(item.get("addedOn")), "published_at": ms(item.get("publishOn")),
        "updated_at": ms(item.get("updatedOn")),
        "currency": currency,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "compare_at_max": max([v["compare_at_price"] for v in variants if v["compare_at_price"]], default=None),
        "on_sale": any(v["compare_at_price"] for v in variants),
        "available": any(v["available"] for v in variants) if variants else None,
        "variant_count": len(variants),
        "image_count": len(imgs),
        "variants": variants,
        "images": [{"position": i, "src": u, "width": None, "height": None} for i, u in enumerate(imgs, start=1)],
        "reviews": [], "rating": None, "review_count": None, "review_app": None,
    }


async def crawl(f: Fetcher, s: Settings, base: str, home_html: str, platform: str) -> tuple[list[dict], list[tuple], dict]:
    rob = await f.robots(base)
    urls = await _sitemap_urls(f, base, rob.sitemaps)
    cap = min(s.max_generic_pages, s.max_products_per_store or s.max_generic_pages)
    products: list[dict] = []
    review_app = None
    for url in urls:
        if len(products) >= cap:
            break
        prod = None
        if platform == "squarespace":
            r = await f.get(url + ("&" if "?" in url else "?") + "format=json")
            if r is not None and r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                try:
                    prod = from_squarespace(url, r.json())
                except ValueError:
                    prod = None
        r = await f.get(url)
        if r is None or r.status_code != 200:
            if prod:
                products.append(prod)
            continue
        html = r.text
        if prod is None:
            prod = from_jsonld(str(r.url), html)
        if prod is None:
            continue
        if s.fetch_reviews:
            review_app = review_app or rv.detect_review_app(html)
            info = await rv.collect(f, html, review_app, prod["product_id"], None, s.max_reviews_per_product, shopify=False)
            prod.update(rating=info["rating"], review_count=info["review_count"], reviews=info["reviews"],
                        review_app=review_app)
        products.append(prod)

    # Drop a shared " — Store Name" suffix that some builders append to every title
    if len(products) >= 2:
        tails = {re.split(r"\s[—–|-]\s", p["title"])[-1] for p in products if re.search(r"\s[—–|-]\s", p["title"])}
        if len(tails) == 1 and all(re.search(r"\s[—–|-]\s", p["title"]) for p in products):
            tail = tails.pop()
            for p in products:
                p["title"] = re.sub(rf"\s[—–|-]\s{re.escape(tail)}$", "", p["title"])

    # homepage order as a light ranking signal
    rankings: list[tuple] = []
    by_url = {urlsplit(p["url"]).path.rstrip("/"): p["product_id"] for p in products}
    pos, seen = 0, set()
    for a in soup(home_html).find_all("a", href=True):
        path = urlsplit(abs_url(base, a["href"])).path.rstrip("/")
        if path in by_url and path not in seen:
            seen.add(path)
            pos += 1
            rankings.append(("homepage", by_url[path], pos))
    return products, rankings, {"candidate_urls": len(urls), "review_app": review_app}
