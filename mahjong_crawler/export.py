"""Write one run to CSV files + an Excel workbook, including week-over-week changes."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd
from openpyxl.utils import get_column_letter

from . import db
from .categories import LABELS, ORDER, add_category, priced
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
    p = add_category(p)
    lead = ["store_domain", "title", "category_vi", "url", "currency", "min_price", "max_price", "compare_at_max", "on_sale", "available",
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
        products=("product_id", "count"),
        on_sale=("on_sale", "sum"), in_stock=("available", "sum"),
        total_reviews=("review_count", "sum"), avg_rating=("rating", "mean"),
        new_this_run=("is_new_this_run", "sum")).reset_index()
    # prices: USD only, ignoring $0 items and gift cards
    pr = priced(products).groupby("store_domain").agg(
        median_price=("min_price", "median"), min_price=("min_price", "min"),
        max_price=("max_price", "max")).reset_index()
    main = (products.assign(n=1).groupby(["store_domain", "category_vi"]).n.sum().reset_index()
            .sort_values("n", ascending=False).drop_duplicates("store_domain")
            .rename(columns={"category_vi": "main_category"})[["store_domain", "main_category"]])
    agg = agg.merge(pr, on="store_domain", how="left").merge(main, on="store_domain", how="left")
    s = stores.rename(columns={"domain": "store_domain"}).drop(columns=["products"], errors="ignore")
    return s.merge(agg, on="store_domain", how="left")


def categories_frame(products: pd.DataFrame) -> pd.DataFrame:
    """Price statistics per category across all stores (USD, no $0 items, no gift cards)."""
    if products.empty:
        return pd.DataFrame()
    pr = priced(products)
    g = pr.groupby("category")
    out = pd.DataFrame({
        "products": products.groupby("category").size(),
        "stores_selling": products.groupby("category").store_domain.nunique(),
        "priced_products": g.size(),
        "price_p25": g.min_price.quantile(0.25), "median_price": g.min_price.median(),
        "price_p75": g.min_price.quantile(0.75), "min_price": g.min_price.min(), "max_price": g.max_price.max(),
        "on_sale_pct": g.on_sale.mean() * 100, "in_stock_pct": g.available.mean() * 100,
        "total_reviews": products.groupby("category").review_count.sum(),
    }).reindex([k for k in ORDER if k in set(products.category)])
    out.insert(0, "category", out.index.map(LABELS))
    return out.reset_index(drop=True).round(1)


def price_compare_frame(products: pd.DataFrame) -> pd.DataFrame:
    """One row per (store, category): price range vs the market median of that category."""
    if products.empty:
        return pd.DataFrame()
    pr = priced(products)
    g = pr.groupby(["category", "store_domain"]).min_price
    d = pd.DataFrame({"n": g.size(), "lo": g.min(), "med": g.median(), "hi": g.max()}).reset_index()
    market = pr.groupby("category").min_price.median()
    d["mkt"] = d.category.map(market)
    d["diff"] = ((d.med / d.mkt - 1) * 100).round(0)
    d["rank"] = d.groupby("category").med.rank(ascending=False, method="min").astype(int)
    d["of"] = d.category.map(d.groupby("category").size())
    d["_o"] = d.category.map({k: i for i, k in enumerate(ORDER)})
    d = d.sort_values(["_o", "med"], ascending=[True, False])
    return pd.DataFrame({
        "Loại": d.category.map(LABELS), "Website": d.store_domain, "Số SP": d.n,
        "Giá thấp nhất": d.lo, "Giá trung vị": d.med.round(2), "Giá cao nhất": d.hi,
        "Trung vị thị trường": d.mkt.round(2), "Chênh so thị trường (%)": d["diff"],
        "Hạng giá (1 = đắt nhất)": d["rank"], "Số shop bán loại này": d["of"]})


def store_category_frame(products: pd.DataFrame) -> pd.DataFrame:
    """Median price of each category in each store, plus product counts (USD, no $0 items, no gift cards)."""
    if products.empty:
        return pd.DataFrame()
    pr = priced(products)
    cats = [k for k in ORDER if k in set(pr.category) and k != "gift_card"]
    med = pr.pivot_table(index="store_domain", columns="category", values="min_price", aggfunc="median")
    cnt = products.pivot_table(index="store_domain", columns="category", values="product_id", aggfunc="count")
    out = pd.DataFrame(index=sorted(set(products.store_domain)))
    for k in cats:
        out[f"{LABELS[k]} – giá TB"] = med.get(k)
        out[f"{LABELS[k]} – số SP"] = cnt.get(k)
    out = out.round(2)
    out.index.name = "store_domain"
    return out.reset_index()


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: ILLEGAL.sub("", v)[:EXCEL_CELL_MAX] if isinstance(v, str) else v)
    return df


def _write_xlsx(path: Path, sheets: dict[str, pd.DataFrame], link_cols: set[str] = frozenset(),
                wrap_cols: set[str] = frozenset()) -> None:
    from openpyxl.styles import Alignment, Font
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            _clean(df).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            if ws.max_column and ws.max_row > 1:
                ws.auto_filter.ref = ws.dimensions
            for i, col in enumerate(df.columns, start=1):
                letter = get_column_letter(i)
                sample = df[col].astype(str).head(200).map(len).max() if len(df) else 0
                width = min(60, max(10, len(str(col)) + 2, int(sample or 0) + 2))
                if col in wrap_cols:
                    width = 80 if col == "Nội dung" else 35
                    for cell in ws[letter][1:]:
                        cell.alignment = Alignment(wrap_text=True, vertical="top")
                if col in link_cols:
                    width = 14
                    for cell in ws[letter][1:]:
                        if isinstance(cell.value, str) and cell.value.startswith("http"):
                            cell.hyperlink = cell.value
                            cell.value = "Mở trang"
                            cell.font = Font(color="0563C1", underline="single")
                ws.column_dimensions[letter].width = width


def reviews_sheets(con, run_id: int, products: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Review-focused workbook: stores -> products with review counts -> review texts."""
    if products.empty:
        return {"Cửa hàng": pd.DataFrame()}
    stores = _q(con, "SELECT domain AS store_domain, name FROM store_runs WHERE run_id=?", run_id)
    rv = _q(con, """SELECT r.store_domain, r.product_id, r.rating, r.title AS review_title, r.body, r.author, r.date, r.verified
                    FROM reviews r JOIN products p ON p.run_id=? AND p.store_domain=r.store_domain
                    AND p.product_id=r.product_id""", run_id)
    rv["date"] = rv["date"].astype(str).str[:10]
    p = products.copy()
    p["review_count"] = p["review_count"].fillna(0).astype(int)

    # per-product star breakdown from downloaded reviews
    stars = rv.assign(star=rv.rating.round().clip(1, 5)).pivot_table(
        index=["store_domain", "product_id"], columns="star", values="author", aggfunc="size", fill_value=0)
    stars = stars.reindex(columns=[5.0, 4.0, 3.0, 2.0, 1.0], fill_value=0)
    stars.columns = ["5 sao", "4 sao", "3 sao", "2 sao", "1 sao"]
    stars = stars.astype("Int64")
    last = rv.groupby(["store_domain", "product_id"]).date.max().rename("Review mới nhất")
    p = p.merge(stars.reset_index(), on=["store_domain", "product_id"], how="left") \
         .merge(last.reset_index(), on=["store_domain", "product_id"], how="left") \
         .merge(stores, on="store_domain", how="left")

    st = p.groupby("store_domain").agg(
        name=("name", "first"), sp=("product_id", "count"), sp_rv=("review_count", lambda x: int((x > 0).sum())),
        total=("review_count", "sum")).reset_index()
    rated = p[p.review_count > 0]
    st = st.merge((rated.rating * rated.review_count).groupby(rated.store_domain).sum()
                  .div(rated.groupby("store_domain").review_count.sum()).round(2).rename("avg").reset_index(),
                  on="store_domain", how="left")
    top = rated.sort_values("review_count", ascending=False).drop_duplicates("store_domain")
    st = st.merge(top[["store_domain", "title", "review_count"]].rename(
        columns={"title": "top", "review_count": "top_n"}), on="store_domain", how="left")
    st = st.merge(rv.groupby("store_domain").size().rename("dl").reset_index(), on="store_domain", how="left")
    st = st.sort_values(["total", "sp"], ascending=False)
    order = {d: i for i, d in enumerate(st.store_domain)}
    st_out = pd.DataFrame({
        "Cửa hàng": st.name, "Website": st.store_domain, "Số sản phẩm": st.sp,
        "SP có review": st.sp_rv, "Tổng số review": st.total, "Điểm TB (sao)": st["avg"],
        "SP nhiều review nhất": st.top, "Số review của SP đó": st.top_n,
        "Review đã tải nội dung": st.dl.fillna(0).astype(int)})

    p["_o"] = p.store_domain.map(order)
    p = p.sort_values(["_o", "review_count", "title"], ascending=[True, False, True])
    p_out = pd.DataFrame({
        "Cửa hàng": p.name, "Website": p.store_domain, "Sản phẩm": p.title, "Loại": p.category_vi,
        "Giá (USD)": p.min_price.where(p.currency == "USD"), "Còn hàng": p.available.map({1: "Có", 0: "Hết"}),
        "Số review": p.review_count, "Điểm TB (sao)": p.rating,
        "5 sao": p["5 sao"], "4 sao": p["4 sao"], "3 sao": p["3 sao"], "2 sao": p["2 sao"], "1 sao": p["1 sao"],
        "Review mới nhất": p["Review mới nhất"], "Link sản phẩm": p.url})

    info = p.set_index(["store_domain", "product_id"])[["name", "title", "url", "review_count", "_o"]]
    r = rv.join(info, on=["store_domain", "product_id"])
    r = r.sort_values(["_o", "review_count", "title", "date"], ascending=[True, False, True, False])
    r_out = pd.DataFrame({
        "Cửa hàng": r.name, "Website": r.store_domain, "Sản phẩm": r.title, "Số sao": r.rating,
        "Tiêu đề": r.review_title, "Nội dung": r.body, "Người viết": r.author,
        "Ngày": r.date, "Đã mua hàng": r.verified.map({1: "Có", 0: "Không"}), "Link sản phẩm": r.url})
    return {"Cửa hàng": st_out, "Sản phẩm": p_out, "Nội dung review": r_out}


def export_run(s: Settings, con, run_id: int) -> Path:
    started = con.execute("SELECT started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    out = s.output_dir / f"{started[:10]}_run{run_id}"
    out.mkdir(parents=True, exist_ok=True)

    stores = _q(con, "SELECT * FROM store_runs WHERE run_id=? ORDER BY domain", run_id)
    products = products_frame(con, run_id)
    sheets = {
        "Summary": summary_frame(stores, products),
        "Categories": categories_frame(products),
        "So_sanh_gia": price_compare_frame(products),
        "Store_Category_Price": store_category_frame(products),
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

    _write_xlsx(out / f"mahjong_stores_{started[:10]}.xlsx", sheets)
    _write_xlsx(out / f"mahjong_reviews_{started[:10]}.xlsx", reviews_sheets(con, run_id, products),
                link_cols={"Link sản phẩm"}, wrap_cols={"Nội dung", "Tiêu đề"})
    log.info("exported run %d -> %s", run_id, out)
    return out
