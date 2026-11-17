# Crawling data – Shopify mahjong stores

Weekly crawler for ~130 American-mahjong online stores (mostly Shopify, plus Squarespace, Wix and WooCommerce).
It collects **products, prices, variants, stock, images, reviews and rankings**, keeps a weekly history,
and exports **CSV + Excel** with a week-over-week **Changes** sheet.

## Results
Latest results are in [`output/`](output/) – one folder per run, e.g. `output/2026-11-16_run1/`:

| File | Contents |
|---|---|
| `mahjong_stores_<date>.xlsx` | all sheets below in one workbook |
| `summary.csv` | one row per store: platform, status, product count, price range, reviews |
| `products.csv` | product, prices, sale, stock, rating, review count, rank columns, first image |
| `variants.csv` · `images.csv` | per-variant price/stock/SKU · every image URL |
| `reviews.csv` | rating, title, text, author, date |
| `rankings.csv` | bestseller / featured / catalog / homepage / review-count ranks |
| `changes.csv` | vs previous run: new/removed products, price changes, sales, stock, new reviews, rank moves |

Images (~19 GB) are **not** in this repo; image URLs are in `images.csv`.

Known issues in the first run (fixed in code from the next run): 12 stores returned VND instead of USD prices,
and 27 stores were wrongly detected as using Yotpo, so their reviews may be missing.

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Usage
```powershell
.\.venv\Scripts\python -m mahjong_crawler run --stores sundaymahjong.com --limit-products 5   # quick test
.\.venv\Scripts\python -m mahjong_crawler run                                                 # full crawl
.\.venv\Scripts\python -m mahjong_crawler resume      # finish an interrupted run (images + export)
.\.venv\Scripts\python -m mahjong_crawler export --run-id 1
powershell -ExecutionPolicy Bypass -File .\schedule_weekly.ps1                                # every Monday 02:00
```
`run` options: `--stores a.com,b.com`, `--limit-products N`, `--no-images`, `--no-reviews`, `--verbose`.

- **Stores:** edit `stores.csv` (set `include` to `no` to skip a store, add rows for new ones).
- **Settings:** `mahjong_crawler/config.py` – speed, image folder (`D:\mahjong_images`), images per product (3),
  US market / USD pricing, review limits.
- **History:** every run is stored in `data/crawl.db` (SQLite, local only).

## How it works
| Platform | Catalog | Reviews | Rankings |
|---|---|---|---|
| Shopify | `/products.json` (fallback: sitemap + `.js`) | product page → Judge.me / Yotpo / Okendo, JSON-LD, badges | bestseller, featured & catalog collections, homepage |
| WooCommerce | Store API | Store API | popularity |
| Squarespace / Wix / other | sitemap → JSON-LD | JSON-LD | homepage |

Polite by design: ~1 request / 1.5 s per store, respects `robots.txt`, backs off on HTTP 429, and skips
stores that stay rate-limited so the run always finishes. Public catalog data only – use images for
internal research, not in your own listings.
