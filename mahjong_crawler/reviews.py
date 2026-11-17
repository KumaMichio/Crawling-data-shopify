"""Review-app detection, rating summaries, and full review download.

Supported for full review text: Judge.me, Yotpo, Okendo (Shopify), JSON-LD on any site.
Rating/count summary: the above plus Loox, Stamped and any theme that emits JSON-LD.
"""
from __future__ import annotations

import json
import logging
import re

from .http import Fetcher
from .util import jsonld_rating, jsonld_reviews, num, short_hash, soup, text_of

log = logging.getLogger(__name__)

# Markers must be widget scripts/elements: Shopify's own JS mentions e.g. "MetafieldYotpoRating"
# on every store, so a bare app name is not enough.
APP_MARKERS = [
    ("judgeme", ("jdgm-", "judge.me/")),
    ("yotpo", ("staticw2.yotpo.com", "cdn-widgetsrepository.yotpo.com", "yotpo-main-widget",
               "yotpo-widget-instance", "class=\"yotpo ")),
    ("okendo", ("okendo.io", "oke-reviews", "okendo-reviews")),
    ("stamped", ("stamped.io", "stamped-product-reviews")),
    ("loox", ("loox.io", "loox-rating")),
    ("junip", ("junip.co", "junip-")),
    ("reviewsio", ("reviews.io", "reviewsio")),
    ("fera", ("fera.ai",)),
    ("shopify_product_reviews", ("spr-badge", "shopify-product-reviews")),
]


def detect_review_app(html: str) -> str | None:
    low = html.lower()
    for name, markers in APP_MARKERS:
        if any(m in low for m in markers):
            return name
    return None


_ATTR_PAIRS = [
    ("data-average-rating", "data-number-of-reviews"),  # Judge.me
    ("data-rating", "data-raters"),                      # Loox
    ("data-rating", "data-count"),                       # Stamped & others
    ("data-rating", "data-number-of-reviews"),
]


def summary_from_html(html: str) -> tuple[float | None, int | None]:
    rating, count = jsonld_rating(html)
    if rating is not None or count is not None:
        return rating, count
    for r_attr, c_attr in _ATTR_PAIRS:
        m = re.search(rf'{r_attr}=["\']([\d.]+)["\'][^>]*?{c_attr}=["\'](\d+)["\']', html)
        if not m:
            m2 = re.search(rf'{c_attr}=["\'](\d+)["\'][^>]*?{r_attr}=["\']([\d.]+)["\']', html)
            if m2:
                return num(m2.group(2)), int(m2.group(1))
            continue
        return num(m.group(1)), int(m.group(2))
    return None, None


# ---------- Judge.me ----------
def _parse_judgeme_html(fragment: str) -> list[dict]:
    out = []
    for rev in soup(fragment).select(".jdgm-rev"):
        rid = rev.get("data-review-id") or ""
        score = rev.select_one(".jdgm-rev__rating")
        ts = rev.select_one(".jdgm-rev__timestamp")
        author = rev.select_one(".jdgm-rev__author")
        title = rev.select_one(".jdgm-rev__title")
        body = rev.select_one(".jdgm-rev__body")
        body_txt = body.get_text(" ", strip=True) if body else ""
        out.append({
            "review_id": rid or short_hash(author, ts, body_txt[:200]),
            "rating": num(score.get("data-score")) if score else None,
            "title": title.get_text(" ", strip=True) if title else "",
            "body": body_txt,
            "author": author.get_text(" ", strip=True) if author else "",
            "date": (ts.get("data-content") or ts.get_text(strip=True)) if ts else "",
            "verified": (rev.get("data-verified-buyer") == "true") if rev.has_attr("data-verified-buyer") else None,
            "source": "judgeme",
        })
    return out


async def _judgeme(f: Fetcher, html: str, product_id: str, shop: str | None, limit: int) -> list[dict]:
    reviews = _parse_judgeme_html(html)
    if not shop:
        return reviews
    seen = {r["review_id"] for r in reviews}
    page = 1
    while len(reviews) < limit:
        url = (f"https://judge.me/reviews/reviews_for_widget?url={shop}&shop_domain={shop}"
               f"&platform=shopify&page={page}&per_page=25&product_id={product_id}")
        r = await f.get(url, kind="api")
        if r is None or r.status_code != 200:
            break
        try:
            fragment = r.json().get("html", "")
        except ValueError:
            break
        batch = [x for x in _parse_judgeme_html(fragment) if x["review_id"] not in seen]
        if not batch:
            break
        seen.update(x["review_id"] for x in batch)
        reviews += batch
        page += 1
    return reviews[:limit]


# ---------- Yotpo ----------
YOTPO_KEY_RE = re.compile(
    r'(?:staticw2\.yotpo\.com/|cdn-widgetsrepository\.yotpo\.com/v1/loader/|data-appkey=["\'])([A-Za-z0-9]{20,60})')


async def _yotpo(f: Fetcher, html: str, product_id: str, limit: int) -> tuple[list[dict], float | None, int | None]:
    m = YOTPO_KEY_RE.search(html)
    if not m:
        return [], None, None
    key, reviews, page, avg, total = m.group(1), [], 1, None, None
    while len(reviews) < limit:
        url = f"https://api-cdn.yotpo.com/v1/widget/{key}/products/{product_id}/reviews.json?per_page=150&page={page}"
        r = await f.get(url, kind="api")
        if r is None or r.status_code != 200:
            break
        try:
            resp = r.json().get("response", {})
        except ValueError:
            break
        bl = resp.get("bottomline") or {}
        avg, total = num(bl.get("average_score")), bl.get("total_review")
        batch = resp.get("reviews") or []
        for x in batch:
            reviews.append({
                "review_id": str(x.get("id")),
                "rating": num(x.get("score")),
                "title": x.get("title") or "",
                "body": text_of(x.get("content")),
                "author": (x.get("user") or {}).get("display_name", ""),
                "date": x.get("created_at") or "",
                "verified": x.get("verified_buyer"),
                "source": "yotpo",
            })
        if not batch or (total is not None and len(reviews) >= total):
            break
        page += 1
    return reviews[:limit], avg, total


# ---------- Okendo ----------
OKENDO_RE = re.compile(r'(?:oke:subscriber_id"\s+content="|"subscriberId"\s*:\s*")([0-9a-f-]{36})')


async def _okendo(f: Fetcher, html: str, product_id: str, limit: int) -> list[dict]:
    m = OKENDO_RE.search(html)
    if not m:
        return []
    url = f"https://api.okendo.io/v1/stores/{m.group(1)}/products/shopify-{product_id}/reviews?limit=25"
    reviews: list[dict] = []
    while url and len(reviews) < limit:
        r = await f.get(url, kind="api")
        if r is None or r.status_code != 200:
            break
        try:
            data = r.json()
        except ValueError:
            break
        for x in data.get("reviews") or []:
            reviews.append({
                "review_id": str(x.get("reviewId")),
                "rating": num(x.get("rating")),
                "title": x.get("title") or "",
                "body": x.get("body") or "",
                "author": (x.get("reviewer") or {}).get("displayName", ""),
                "date": x.get("dateCreated") or "",
                "verified": (x.get("reviewer") or {}).get("isVerified"),
                "source": "okendo",
            })
        nxt = data.get("nextUrl")
        url = f"https://api.okendo.io/v1{nxt}" if nxt else None
    return reviews[:limit]


async def collect(f: Fetcher, html: str, app: str | None, product_id: str,
                  shop: str | None, limit: int, shopify: bool) -> dict:
    """Return {'rating', 'review_count', 'reviews'} for one product page."""
    rating, count = summary_from_html(html)
    reviews: list[dict] = []
    try:
        if shopify and app == "judgeme" and (count is None or count > 0):
            reviews = await _judgeme(f, html, product_id, shop, limit)
        elif shopify and app == "yotpo":
            reviews, avg, total = await _yotpo(f, html, product_id, limit)
            rating = rating if rating is not None else avg
            count = count if count is not None else total
        elif shopify and app == "okendo":
            reviews = await _okendo(f, html, product_id, limit)
    except Exception as e:  # never let a review widget break the crawl
        log.debug("review fetch failed for %s: %r", product_id, e)
    if not reviews:
        reviews = jsonld_reviews(html)
    if count == 0:
        rating = None  # widgets report 0.0 for "no reviews yet"
    if count is None and reviews:
        count = len(reviews)
        rated = [r["rating"] for r in reviews if r["rating"] is not None]
        rating = rating if rating is not None else (round(sum(rated) / len(rated), 2) if rated else None)
    return {"rating": rating, "review_count": count, "reviews": reviews}
