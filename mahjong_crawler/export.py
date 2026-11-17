"""Write one run to CSV files + an Excel workbook, including week-over-week changes."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter

from . import db
from .config import Settings

log = logging.getLogger(__name__)
ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
EXCEL_CELL_MAX = 32000


def _q(con, sql, *args) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=args)


def products_frame(con, run_id: int) -> pd.DataFrame:
    p = _q(con, "SELECT * FROM products WHERE run_id=?", run_id)
    if p.empty:
        return p
    img = _q(con, """SELECT store_domain, product_id, src AS image_1_url, local_path AS image_1_file
                     FROM images WHERE run_id=? AND position=1""", run_id)
    rk = _q(con, "SELECT store_domain, product_id, list_name, position FROM rankings WHERE run_id=?", run_id)
    p = p.merge(img, on=["store_domain", "product_id"], how="left")
    if not rk.empty:
        rk["kind"] = rk["list_name"].str.replace(r"^(bestseller|featured|catalog):.*$", r"\1", regex=True)
        piv = rk.groupby(["store_domain", "product_id", "kind"])["position"].min().unstack()
        piv = piv.rename(columns={"bestseller": "rank_bestseller_collection", "featured": "rank_featured",
                                  "catalog": "rank_catalog_order", "homepage": "rank_homepage",
                                  "reviews:store": "rank_reviews_in_store", "reviews:all": "rank_reviews_overall",
                                  "woo:popularity": "rank_woo_popularity"}).reset_index()
        p = p.merge(piv, on=["store_domain", "product_id"], how="left")
    first_seen = _q(con, "SELECT store_domain, product_id, min(run_id) AS first_seen_run FROM products GROUP BY 1, 2")
    p = p.merge(first_seen, on=["store_domain", "product_id"], how="left")
    p["is_new_this_run"] = p["first_seen_run"] == run_id
    lead = ["store_domain", "title", "url", "min_price", "max_price", "compare_at_max", "on_sale", "available",
            "rating", "review_count", "rank_bestseller_collection", "rank_featured", "rank_catalog_order",
            "rank_homepage", "rank_reviews_in_store",
            "rank_reviews_overall", "rank_woo_popularity", "product_type", "tags", "image_1_url", "image_1_file"]
    lead = [c for c in lead if c in p.columns]
    return p[lead + [c for c in p.columns if c not in lead]]


def changes_frame(con, run_id: int) -> pd.DataFrame:
    prev = db.previous_run(con, run_id)
    cols = ["store_domain", "product_id", "title", "url", "change", "old", "new"]
    if prev is None:
        return pd.DataFrame(columns=cols)
    ok = _q(con, """SELECT a.domain FROM store_runs a JOIN store_runs b ON a.domain=b.domain
                    WHERE a.run_id=? AND b.run_id=? AND a.status='ok' AND b.status='ok'""", run_id, prev)
    ok_set = set(ok["domain"])
    key = ["store_domain", "product_id"]
    cur = _q(con, "SELECT * FROM products WHERE run_id=?", run_id)
    old = _q(con, "SELECT * FROM products WHERE run_id=?", prev)
    cur, old = cur[cur.store_domain.isin(ok_set)], old[old.store_domain.isin(ok_set)]
    m = cur.merge(old, on=key, how="outer", suffixes=("", "_old"), indicator=True)
    out = []

    def add(rows, change, old_col=None, new_col=None, title="title", url="url"):
        for _, r in rows.iterrows():
            out.append({"store_domain": r.store_domain, "product_id": r.product_id,
                        "title": r[title] if pd.notna(r[title]) else r.get("title_old"),
                        "url": r[url] if pd.notna(r[url]) else r.get("url_old"), "change": change,
                        "old": r[old_col] if old_col else None, "new": r[new_col] if new_col else None})

    add(m[m._merge == "left_only"], "new_product", None, "min_price")
    gone = m[m._merge == "right_only"].copy()
    gone["title"], gone["url"] = gone["title_old"], gone["url_old"]
    add(gone, "removed_product", "min_price_old", None)
    both = m[m._merge == "both"]
    add(both[(both.min_price.round(2) != both.min_price_old.round(2)) & both.min_price.notna() & both.min_price_old.notna()],
        "price_change", "min_price_old", "min_price")
    add(both[(both.on_sale == 1) & (both.on_sale_old != 1)], "sale_started", "min_price_old", "min_price")
    add(both[(both.on_sale != 1) & (both.on_sale_old == 1)], "sale_ended", "min_price_old", "min_price")
    add(both[(both.available == 0) & (both.available_old == 1)], "sold_out", "available_old", "available")
    add(both[(both.available == 1) & (both.available_old == 0)], "back_in_stock", "available_old", "available")
    add(both[both.review_count.fillna(0) > both.review_count_old.fillna(0)], "new_reviews",
        "review_count_old", "review_count")

    rk_sql = "SELECT * FROM rankings WHERE run_id=? AND (list_name LIKE 'bestseller:%' OR list_name LIKE 'featured:%')"
    rk_cur, rk_old = _q(con, rk_sql, run_id), _q(con, rk_sql, prev)
    rk = rk_cur.merge(rk_old, on=["store_domain", "list_name", "product_id"], how="outer", suffixes=("", "_old"))
    pos_new = pd.to_numeric(rk.position, errors="coerce").fillna(-1)
    pos_old = pd.to_numeric(rk.position_old, errors="coerce").fillna(-1)
    rk = rk[rk.store_domain.isin(ok_set) & (pos_new != pos_old)]
    titles = cur.set_index(key)[["title", "url"]]
    for _, r in rk.iterrows():
        t = titles.loc[(r.store_domain, r.product_id)] if (r.store_domain, r.product_id) in titles.index else None
        out.append({"store_domain": r.store_domain, "product_id": r.product_id,
                    "title": t["title"] if t is not None else "", "url": t["url"] if t is not None else "",
                    "change": f"rank_change:{r.list_name}", "old": r.position_old, "new": r.position})
    return pd.DataFrame(out, columns=cols)


def summary_frame(stores: pd.DataFrame, products: pd.DataFrame) -> pd.DataFrame:
    if products.empty:
        return stores
    agg = products.groupby("store_domain").agg(
        products=("product_id", "count"), median_price=("min_price", "median"),
        min_price=("min_price", "min"), max_price=("max_price", "max"),
        on_sale=("on_sale", "sum"), in_stock=("available", "sum"),
        total_reviews=("review_count", "sum"), avg_rating=("rating", "mean"),
        new_this_run=("is_new_this_run", "sum")).reset_index()
    s = stores.rename(columns={"domain": "store_domain"}).drop(columns=["products"], errors="ignore")
    return s.merge(agg, on="store_domain", how="left")


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: ILLEGAL.sub("", v)[:EXCEL_CELL_MAX] if isinstance(v, str) else v)
    return df


def export_run(s: Settings, con, run_id: int) -> Path:
    started = con.execute("SELECT started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    out = s.output_dir / f"{started[:10]}_run{run_id}"
    out.mkdir(parents=True, exist_ok=True)

    stores = _q(con, "SELECT * FROM store_runs WHERE run_id=? ORDER BY domain", run_id)
    products = products_frame(con, run_id)
    sheets = {
        "Summary": summary_frame(stores, products),
        "Products": products,
        "Variants": _q(con, "SELECT * FROM variants WHERE run_id=?", run_id),
        "Images": _q(con, "SELECT * FROM images WHERE run_id=?", run_id),
        "Reviews": _q(con, """SELECT r.store_domain, p.title AS product_title, p.url AS product_url, r.*
                              FROM reviews r JOIN products p ON p.run_id=? AND p.store_domain=r.store_domain
                              AND p.product_id=r.product_id ORDER BY r.store_domain, p.title, r.date DESC""", run_id),
        "Rankings": _q(con, """SELECT k.store_domain, k.list_name, k.position, p.title, p.min_price, p.review_count,
                               p.url, k.product_id FROM rankings k LEFT JOIN products p ON p.run_id=k.run_id
                               AND p.store_domain=k.store_domain AND p.product_id=k.product_id
                               WHERE k.run_id=? ORDER BY k.store_domain, k.list_name, k.position""", run_id),
        "Changes": changes_frame(con, run_id),
    }
    if "store_domain" in sheets["Reviews"].columns:
        sheets["Reviews"] = sheets["Reviews"].loc[:, ~sheets["Reviews"].columns.duplicated()]

    for name, df in sheets.items():
        df.to_csv(out / f"{name.lower()}.csv", index=False, encoding="utf-8-sig")

    xlsx = out / f"mahjong_stores_{started[:10]}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        for name, df in sheets.items():
            _clean(df).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            if ws.max_column and ws.max_row > 1:
                ws.auto_filter.ref = ws.dimensions
            for i, col in enumerate(df.columns, start=1):
                sample = df[col].astype(str).head(200).map(len).max() if len(df) else 0
                width = min(60, max(10, len(str(col)) + 2, int(sample or 0) + 2))
                ws.column_dimensions[get_column_letter(i)].width = width
    log.info("exported run %d -> %s", run_id, out)
    return out
