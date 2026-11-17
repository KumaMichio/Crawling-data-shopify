"""Crawler settings. Edit values here (or override some from the CLI)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # Inputs
    bookmarks_file: Path = ROOT / "bookmarks_11_15_26.html"
    bookmark_folder: str = "Mahjong Store"
    stores_csv: Path = ROOT / "stores.csv"

    # Outputs
    db_path: Path = ROOT / "data" / "crawl.db"
    output_dir: Path = ROOT / "output"
    images_dir: Path = Path(r"D:\mahjong_images")  # E: is too small for ~15 GB of images
    logs_dir: Path = ROOT / "logs"

    # HTTP behaviour (be polite: Shopify rate-limits per IP across all stores)
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )
    robots_agent: str = "*"
    market_country: str = "US"         # Shopify Markets: show US prices ...
    market_currency: str = "USD"       # ... in USD, regardless of where the crawler runs
    per_domain_interval: float = 1.5   # seconds between requests to one store
    global_interval: float = 0.3       # seconds between any two store requests
    image_interval: float = 0.05       # seconds between image requests per CDN host
    store_concurrency: int = 6         # stores crawled at the same time
    image_concurrency: int = 8
    request_timeout: float = 30.0
    max_retries: int = 4
    max_crawl_delay: float = 10.0      # cap for robots.txt Crawl-delay
    rate_limit_trip_stores: int = 3    # after this many stores stay 429, stop retrying blocked stores

    # What to collect
    fetch_reviews: bool = True         # needs one product-page request per product
    max_reviews_per_product: int = 200
    download_images: bool = True
    image_max_width: int | None = 1200  # resize on CDN (Shopify/Squarespace); None = original
    max_images_per_product: int = 3    # only download the first N images of each product
    min_free_disk_gb: float = 2.0      # stop downloading images below this much free space
    max_products_per_store: int | None = None  # set e.g. 5 for a quick test
    max_generic_pages: int = 400       # product pages fetched for non-Shopify sites
    max_bestseller_collections: int = 3
