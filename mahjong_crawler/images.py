"""Download product images once (files are reused across weekly runs)."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Settings
from .http import Fetcher
from .util import short_hash

log = logging.getLogger(__name__)


def sized_url(src: str, max_width: int | None) -> str:
    if not max_width:
        return src
    p = urlsplit(src)
    q = dict(parse_qsl(p.query))
    if "cdn.shopify.com" in p.netloc or "/cdn/shop/" in p.path:
        q["width"] = str(max_width)
    elif "squarespace-cdn.com" in p.netloc:
        q["format"] = f"{max_width}w"
    else:
        return src
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def local_path(s: Settings, domain: str, handle: str, pos: int, src: str) -> Path:
    path = urlsplit(src).path
    ext = Path(path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
        ext = ".jpg"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", handle or "product")[:80].strip("-") or "product"
    return s.images_dir / domain / safe / f"{pos:02d}_{short_hash(path)}{ext}"


async def download_for_run(f: Fetcher, s: Settings, con, run_id: int) -> dict:
    rows = con.execute("SELECT store_domain, product_id, handle, position, src FROM images "
                       "WHERE run_id=? AND position<=?", (run_id, s.max_images_per_product)).fetchall()
    stats = {"total": len(rows), "downloaded": 0, "cached": 0, "failed": 0, "stopped": ""}
    sem = asyncio.Semaphore(s.image_concurrency)
    updates: list[tuple] = []
    s.images_dir.mkdir(parents=True, exist_ok=True)

    def disk_ok() -> bool:
        return shutil.disk_usage(s.images_dir).free > s.min_free_disk_gb * 1024 ** 3

    async def one(domain, pid, handle, pos, src):
        if stats["stopped"]:
            return
        dest = local_path(s, domain, handle, pos, src)
        if dest.exists() and dest.stat().st_size > 0:
            stats["cached"] += 1
        else:
            try:
                async with sem:
                    r = await f.get(sized_url(src, s.image_max_width), kind="image")
                if r is None or r.status_code != 200 or not r.content:
                    stats["failed"] += 1
                    return
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
            except OSError as e:  # disk full, path problems
                stats["stopped"] = f"write error: {e}"
                return
            except Exception as e:  # bad URL etc. - skip this image only
                stats["failed"] += 1
                log.debug("image failed %s: %r", src, e)
                return
            stats["downloaded"] += 1
        updates.append((str(dest), run_id, domain, pid, pos))

    for i in range(0, len(rows), 500):
        if not disk_ok():
            stats["stopped"] = f"less than {s.min_free_disk_gb} GB free on disk"
        if stats["stopped"]:
            log.warning("image download stopped: %s (%d/%d done)", stats["stopped"], i, len(rows))
            break
        await asyncio.gather(*(one(*row) for row in rows[i:i + 500]))
        log.info("images: %d/%d processed", min(i + 500, len(rows)), len(rows))
    con.executemany("UPDATE images SET local_path=? WHERE run_id=? AND store_domain=? AND product_id=? AND position=?",
                    updates)
    con.commit()
    return stats
