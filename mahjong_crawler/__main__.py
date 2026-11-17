"""CLI.

  python -m mahjong_crawler init-stores [--force]     build stores.csv from the bookmarks file
  python -m mahjong_crawler run [options]             crawl + images + export
  python -m mahjong_crawler export [--run-id N]       re-export a finished run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime

from . import db, export, images
from .config import Settings
from .crawl import crawl_store
from .http import Fetcher
from .stores import load_stores, parse_bookmarks, write_stores

log = logging.getLogger("mahjong_crawler")


def setup_logging(s: Settings, verbose: bool) -> None:
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(s.logs_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run(s: Settings, only: list[str] | None) -> int:
    stores = load_stores(s)
    if only:
        stores = [x for x in stores if any(o in x["domain"] for o in only)]
    con = db.connect(s.db_path)
    run_id = db.start_run(con)
    log.info("run %d: crawling %d stores", run_id, len(stores))
    t0 = time.monotonic()
    done = 0
    async with Fetcher(s) as f:
        sem = asyncio.Semaphore(s.store_concurrency)

        async def one(store):
            nonlocal done
            async with sem:
                res = await crawl_store(f, s, store)
            db.save_store(con, run_id, res)
            done += 1
            log.info("[%d/%d] %-32s %-11s %-22s products=%-4d reviews=%-5d %5.0fs %s",
                     done, len(stores), res.domain, res.platform or "-", res.status, len(res.products),
                     sum(len(p["reviews"]) for p in res.products), res.seconds, res.error)

        await asyncio.gather(*(one(x) for x in stores))
        db.derive_rankings(con, run_id)
        img_stats = {}
        if s.download_images:
            log.info("downloading images ...")
            try:
                img_stats = await images.download_for_run(f, s, con, run_id)
            except Exception as e:  # images must never cost us the export
                log.exception("image download failed")
                img_stats = {"error": repr(e)}
            log.info("images: %s", img_stats)
        stats = dict(f.stats, images=img_stats, minutes=round((time.monotonic() - t0) / 60, 1))
    db.finish_run(con, run_id, "done", json.dumps(stats))
    out = export.export_run(s, con, run_id)
    log.info("run %d finished in %.1f min. stats=%s", run_id, stats["minutes"], stats)
    print(f"\nOutput folder: {out}")
    return run_id


async def resume(s: Settings, run_id: int) -> None:
    """Finish a run whose crawl completed but images/export did not (e.g. disk full, PC shut down)."""
    con = db.connect(s.db_path)
    t0 = time.monotonic()
    async with Fetcher(s) as f:
        db.derive_rankings(con, run_id)
        img_stats = {}
        if s.download_images:
            log.info("downloading images for run %d ...", run_id)
            img_stats = await images.download_for_run(f, s, con, run_id)
            log.info("images: %s", img_stats)
    db.finish_run(con, run_id, "done", json.dumps({"resumed": True, "images": img_stats,
                                                   "minutes": round((time.monotonic() - t0) / 60, 1)}))
    print(f"\nOutput folder: {export.export_run(s, con, run_id)}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mahjong_crawler")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init-stores")
    p_init.add_argument("--force", action="store_true", help="overwrite existing stores.csv")
    p_run = sub.add_parser("run")
    p_run.add_argument("--stores", help="comma-separated domain filter, e.g. sundaymahjong.com,ladymahj.com")
    p_run.add_argument("--limit-products", type=int, help="max products per store (for testing)")
    p_run.add_argument("--no-images", action="store_true")
    p_run.add_argument("--no-reviews", action="store_true")
    p_run.add_argument("--verbose", action="store_true")
    p_res = sub.add_parser("resume", help="finish an interrupted run (images + export)")
    p_res.add_argument("--run-id", type=int, help="default: latest unfinished run")
    p_res.add_argument("--no-images", action="store_true")
    p_exp = sub.add_parser("export")
    p_exp.add_argument("--run-id", type=int)
    args = ap.parse_args()

    s = Settings()
    if args.cmd == "init-stores":
        if s.stores_csv.exists() and not args.force:
            print(f"{s.stores_csv} exists (use --force to rebuild)")
            return
        rows = parse_bookmarks(s.bookmarks_file, s.bookmark_folder)
        write_stores(s, rows)
        print(f"wrote {len(rows)} stores to {s.stores_csv}")
    elif args.cmd == "run":
        if args.limit_products:
            s.max_products_per_store = args.limit_products
        if args.no_images:
            s.download_images = False
        if args.no_reviews:
            s.fetch_reviews = False
        setup_logging(s, args.verbose)
        only = [x.strip().lower() for x in args.stores.split(",")] if args.stores else None
        asyncio.run(run(s, only))
    elif args.cmd == "resume":
        if args.no_images:
            s.download_images = False
        setup_logging(s, False)
        con = db.connect(s.db_path)
        run_id = args.run_id or con.execute("SELECT max(run_id) FROM runs WHERE status='running'").fetchone()[0]
        if run_id is None:
            print("no unfinished run")
            return
        asyncio.run(resume(s, run_id))
    elif args.cmd == "export":
        setup_logging(s, False)
        con = db.connect(s.db_path)
        run_id = args.run_id or con.execute("SELECT max(run_id) FROM runs WHERE status='done'").fetchone()[0]
        print(export.export_run(s, con, run_id))


if __name__ == "__main__":
    main()
