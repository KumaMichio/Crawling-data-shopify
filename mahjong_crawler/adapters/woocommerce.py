"""WooCommerce Store API (public): products sorted by popularity + product reviews."""
from __future__ import annotations

from ..config import Settings
from ..http import Fetcher
from ..util import text_of


def _money(prices: dict, key: str) -> float | None:
    v = prices.get(key)
    if v in (None, ""):
        return None
    return int(v) / (10 ** int(prices.get("currency_minor_unit", 2)))


async def crawl(f: Fetcher, s: Settings, base: str) -> tuple[list[dict], list[tuple], dict] | None:
    api = f"{base}/wp-json/wc/store/v1"
    raw: list[dict] = []
    for page in range(1, 51):
        r = await f.get(f"{api}/products?per_page=100&page={page}&orderby=popularity&order=desc")
        if r is None or r.status_code != 200:
            if page == 1:
                return None
            break
        try:
            batch = r.json()
        except ValueError:
            if page == 1:
                return None
            break
        if not isinstance(batch, list) or not batch:
            break
        raw += batch
        if len(batch) < 100 or (s.max_products_per_store and len(raw) >= s.max_products_per_store):
            break
    if s.max_products_per_store:
        raw = raw[: s.max_products_per_store]

    products, rankings = [], []
    for pos, p in enumerate(raw, start=1):
        pr = p.get("prices") or {}
        price, regular = _money(pr, "price"), _money(pr, "regular_price")
        imgs = [i.get("src") for i in p.get("images") or [] if i.get("src")]
        pid = str(p.get("id"))
        prod = {
            "product_id": pid, "handle": p.get("slug") or "", "title": text_of(p.get("name")),
            "url": p.get("permalink") or "", "product_type": p.get("type") or "",
            "vendor": "", "tags": ", ".join(t.get("name", "") for t in (p.get("tags") or []) + (p.get("categories") or [])),
            "description": text_of(p.get("description") or p.get("short_description")),
            "created_at": "", "published_at": "", "updated_at": "",
            "currency": pr.get("currency_code") or "USD",
            "min_price": price, "max_price": price,
            "compare_at_max": regular if regular and price and regular > price else None,
            "on_sale": bool(p.get("on_sale")),
            "available": p.get("is_in_stock"),
            "variant_count": max(1, len(p.get("variations") or [])),
            "image_count": len(imgs),
            "variants": [{"variant_id": pid, "title": "", "sku": p.get("sku") or "", "price": price,
                          "compare_at_price": regular if p.get("on_sale") else None,
                          "available": p.get("is_in_stock"), "option1": None, "option2": None, "option3": None}],
            "images": [{"position": i, "src": u, "width": None, "height": None} for i, u in enumerate(imgs, start=1)],
            "rating": float(p["average_rating"]) if p.get("average_rating") not in (None, "", "0") else None,
            "review_count": p.get("review_count"),
            "review_app": "woocommerce",
            "reviews": [],
        }
        if s.fetch_reviews and prod["review_count"]:
            r = await f.get(f"{api}/products/reviews?product_id={pid}&per_page=100")
            if r is not None and r.status_code == 200:
                try:
                    for x in r.json()[: s.max_reviews_per_product]:
                        prod["reviews"].append({
                            "review_id": str(x.get("id")), "rating": x.get("rating"), "title": "",
                            "body": text_of(x.get("review")), "author": x.get("reviewer") or "",
                            "date": x.get("date_created") or "", "verified": x.get("verified"),
                            "source": "woocommerce",
                        })
                except ValueError:
                    pass
        products.append(prod)
        rankings.append(("woo:popularity", pid, pos))
    return products, rankings, {}
