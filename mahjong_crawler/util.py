"""Small parsing helpers shared by adapters."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def text_of(html: str | None) -> str:
    if not html:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True)).strip()


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def short_hash(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def abs_url(base: str, href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    return urljoin(base + "/", href)


def base_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


# ---------- JSON-LD ----------
def jsonld_objects(html: str) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, list):
                stack.extend(obj)
            elif isinstance(obj, dict):
                if "@graph" in obj:
                    stack.extend(obj["@graph"] if isinstance(obj["@graph"], list) else [obj["@graph"]])
                out.append(obj)
    return out


def _is_type(obj: dict, name: str) -> bool:
    t = obj.get("@type")
    return t == name or (isinstance(t, list) and name in t)


def jsonld_products(html: str) -> list[dict]:
    objs = jsonld_objects(html)
    return [o for o in objs if _is_type(o, "Product") or _is_type(o, "ProductGroup")]


def jsonld_rating(html: str) -> tuple[float | None, int | None]:
    for p in jsonld_products(html):
        ar = p.get("aggregateRating")
        if isinstance(ar, dict):
            count = ar.get("reviewCount") or ar.get("ratingCount")
            try:
                return num(ar.get("ratingValue")), int(float(count)) if count is not None else None
            except ValueError:
                return num(ar.get("ratingValue")), None
    return None, None


def jsonld_reviews(html: str) -> list[dict]:
    out = []
    for p in jsonld_products(html):
        revs = p.get("review") or []
        if isinstance(revs, dict):
            revs = [revs]
        for r in revs:
            if not isinstance(r, dict):
                continue
            rating = r.get("reviewRating") or {}
            author = r.get("author") or {}
            if isinstance(author, dict):
                author = author.get("name")
            body = r.get("reviewBody") or r.get("description") or ""
            out.append({
                "review_id": short_hash(author, r.get("datePublished"), body[:200]),
                "rating": num(rating.get("ratingValue")) if isinstance(rating, dict) else None,
                "title": r.get("name") or "",
                "body": body,
                "author": author or "",
                "date": r.get("datePublished") or "",
                "verified": None,
                "source": "jsonld",
            })
    return out
