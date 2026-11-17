"""SQLite storage. Every weekly run is a snapshot (run_id); reviews accumulate."""
from __future__ import annotations

import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, status TEXT, stats TEXT);
CREATE TABLE IF NOT EXISTS store_runs (
  run_id INTEGER, domain TEXT, name TEXT, url TEXT, base_url TEXT, platform TEXT, status TEXT,
  error TEXT, review_app TEXT, currency TEXT, products INTEGER, reviews INTEGER, seconds REAL, notes TEXT,
  PRIMARY KEY (run_id, domain));
CREATE TABLE IF NOT EXISTS products (
  run_id INTEGER, store_domain TEXT, product_id TEXT, handle TEXT, title TEXT, url TEXT,
  product_type TEXT, vendor TEXT, tags TEXT, description TEXT,
  created_at TEXT, published_at TEXT, updated_at TEXT, currency TEXT,
  min_price REAL, max_price REAL, compare_at_max REAL, on_sale INTEGER, available INTEGER,
  variant_count INTEGER, image_count INTEGER, rating REAL, review_count INTEGER, review_app TEXT,
  PRIMARY KEY (run_id, store_domain, product_id));
CREATE TABLE IF NOT EXISTS variants (
  run_id INTEGER, store_domain TEXT, product_id TEXT, variant_id TEXT, title TEXT, sku TEXT,
  price REAL, compare_at_price REAL, available INTEGER, option1 TEXT, option2 TEXT, option3 TEXT,
  PRIMARY KEY (run_id, store_domain, product_id, variant_id));
CREATE TABLE IF NOT EXISTS images (
  run_id INTEGER, store_domain TEXT, product_id TEXT, handle TEXT, position INTEGER, src TEXT,
  width INTEGER, height INTEGER, local_path TEXT,
  PRIMARY KEY (run_id, store_domain, product_id, position));
CREATE TABLE IF NOT EXISTS reviews (
  store_domain TEXT, product_id TEXT, review_id TEXT, rating REAL, title TEXT, body TEXT, author TEXT,
  date TEXT, verified INTEGER, source TEXT, first_seen_run INTEGER, last_seen_run INTEGER,
  PRIMARY KEY (store_domain, product_id, review_id));
CREATE TABLE IF NOT EXISTS rankings (
  run_id INTEGER, store_domain TEXT, list_name TEXT, product_id TEXT, position INTEGER,
  PRIMARY KEY (run_id, store_domain, list_name, product_id));
"""


def connect(path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def start_run(con) -> int:
    cur = con.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now(),))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id: int, status: str, stats: str) -> None:
    con.execute("UPDATE runs SET finished_at=?, status=?, stats=? WHERE run_id=?", (now(), status, stats, run_id))
    con.commit()


def previous_run(con, run_id: int) -> int | None:
    row = con.execute("SELECT max(run_id) FROM runs WHERE run_id < ? AND status='done'", (run_id,)).fetchone()
    return row[0] if row else None


def _b(v):
    return None if v is None else int(bool(v))


def save_store(con, run_id: int, r) -> None:
    n_reviews = sum(len(p["reviews"]) for p in r.products)
    con.execute("INSERT OR REPLACE INTO store_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, r.domain, r.name, r.url, r.base_url, r.platform, r.status, r.error,
                 r.review_app, r.currency, len(r.products), n_reviews, r.seconds, r.notes))
    for p in r.products:
        con.execute("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, r.domain, p["product_id"], p["handle"], p["title"], p["url"], p["product_type"],
                     p["vendor"], p["tags"], p["description"], p["created_at"], p["published_at"],
                     p["updated_at"], p["currency"], p["min_price"], p["max_price"], p["compare_at_max"],
                     _b(p["on_sale"]), _b(p["available"]), p["variant_count"], p["image_count"],
                     p["rating"], p["review_count"], p["review_app"]))
        con.executemany("INSERT OR REPLACE INTO variants VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(run_id, r.domain, p["product_id"], v["variant_id"], v["title"], v["sku"], v["price"],
                          v["compare_at_price"], _b(v["available"]), v["option1"], v["option2"], v["option3"])
                         for v in p["variants"]])
        con.executemany("INSERT OR REPLACE INTO images VALUES (?,?,?,?,?,?,?,?,NULL)",
                        [(run_id, r.domain, p["product_id"], p["handle"], i["position"], i["src"],
                          i["width"], i["height"]) for i in p["images"]])
        for rv in p["reviews"]:
            con.execute(
                """INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(store_domain, product_id, review_id) DO UPDATE SET last_seen_run=excluded.last_seen_run""",
                (r.domain, p["product_id"], rv["review_id"], rv["rating"], rv["title"], rv["body"], rv["author"],
                 rv["date"], _b(rv["verified"]), rv["source"], run_id, run_id))
    con.executemany("INSERT OR REPLACE INTO rankings VALUES (?,?,?,?,?)",
                    [(run_id, r.domain, ln, pid, pos) for ln, pid, pos in r.rankings])
    con.commit()


def derive_rankings(con, run_id: int) -> None:
    """Review-count based popularity ranks (in-store and across all stores)."""
    con.execute("DELETE FROM rankings WHERE run_id=? AND list_name IN ('reviews:store', 'reviews:all')", (run_id,))
    con.execute("""
        INSERT INTO rankings
        SELECT run_id, store_domain, 'reviews:store', product_id,
               ROW_NUMBER() OVER (PARTITION BY store_domain ORDER BY review_count DESC, rating DESC)
        FROM products WHERE run_id=? AND review_count > 0""", (run_id,))
    con.execute("""
        INSERT INTO rankings
        SELECT run_id, store_domain, 'reviews:all', product_id,
               ROW_NUMBER() OVER (ORDER BY review_count DESC, rating DESC)
        FROM products WHERE run_id=? AND review_count > 0""", (run_id,))
    con.commit()
