"""Build / load the store list from the browser bookmarks export."""
from __future__ import annotations

import csv
import html
import re
from urllib.parse import urlsplit

from .config import Settings

TOKEN_RE = re.compile(r'<H3[^>]*>(.*?)</H3>|<DL>|</DL>|<A HREF="([^"]*)"[^>]*>(.*?)</A>', re.I | re.S)


def parse_bookmarks(path, folder: str) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r' ICON="[^"]*"', "", text)
    stack: list[str | None] = []
    current: str | None = None
    out: list[dict] = []
    seen: set[str] = set()
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0).upper()
        if tok.startswith("<H3"):
            current = html.unescape(m.group(1))
        elif tok == "<DL>":
            stack.append(current)
            current = None
        elif tok == "</DL>":
            if stack:
                stack.pop()
        elif folder in stack:
            url = html.unescape(m.group(2))
            domain = urlsplit(url).netloc.lower().removeprefix("www.")
            if domain in seen:
                continue
            seen.add(domain)
            out.append({"name": html.unescape(m.group(3)).strip(), "url": url,
                        "domain": domain, "include": "yes", "notes": ""})
    return out


def write_stores(s: Settings, rows: list[dict]) -> None:
    with open(s.stores_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "url", "domain", "include", "notes"])
        w.writeheader()
        w.writerows(rows)


def load_stores(s: Settings) -> list[dict]:
    if not s.stores_csv.exists():
        write_stores(s, parse_bookmarks(s.bookmarks_file, s.bookmark_folder))
    with open(s.stores_csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if (r.get("include") or "yes").strip().lower() not in ("no", "n", "0", "false")]
