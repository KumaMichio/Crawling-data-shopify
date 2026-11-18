"""Rule-based product categories from title + product_type (first matching rule wins)."""
from __future__ import annotations

import re

import pandas as pd

# (key, Vietnamese label, regex). Order matters: specific phrases before loose words like "tiles".
RULES = [
    ("gift_card", "Thẻ quà tặng", r"gift ?card|e-?gift|gift certificate"),
    ("lesson_event", "Lớp học / sự kiện", r"\blessons?\b|\bclass(es)?\b|workshop|\btickets?\b|\bevents?\b|tournament"
                                         r"|registration|\bcamp\b|clinic|private party|mahj night|deposit"),
    ("tile_set", "Bộ quân (tile set)", r"tiles? set|mah[- ]?j(ongg?)? sets?\b|complete set|full set|starter set"
                                        r"|deluxe set|\bset of \d+ tiles|complete game|just the tiles"),
    ("mat", "Thảm chơi (mat)", r"\bmats?\b|table ?cover|table ?cloth|playing surface|neoprene"),
    ("rack", "Giá đỡ (rack)", r"\bracks?\b|pushers?\b"),
    ("bag", "Túi / hộp đựng", r"\b(bag|pouch|tote|case|carrier|backpack|clutch|purse|handbag|wristlet|organizer)s?\b"),
    ("jewelry", "Trang sức", r"necklace|earring|bracelet|\brings?\b|pendant|bangle|wedding band|gemstone|diamond"),
    ("accessory", "Phụ kiện chơi", r"card holder|card stand|\bdice\b|\bdie\b|jokers?\b|wind (indicator|marker)|\bcoins?\b"
                                   r"|dauber|stickers?|scor(e|ing)|counters?|markers?|tile cleaner|shufflers?|mixers?"
                                   r"|tile takers?|tile tray|replacement|(flower|news|animal|extra|blank|spare) tiles"),
    ("home_gift", "Quà tặng / đồ gia dụng", r"(greeting|christmas|holiday|birthday|thank you|note|valentine) cards?"),
    ("card", "Thẻ luật chơi (card)", r"\bcards?\b|nmjl|national mah ?jongg"),
    ("table", "Bàn chơi", r"\btables?\b"),
    ("apparel", "Quần áo", r"shirt|\btee\b|sweatshirt|hoodie|\bhats?\b|\bcaps?\b|socks?|pajama|sweater|crewneck"
                           r"|apparel|clothing|jacket|\bdress\b|visor|beanie"),
    ("jewelry", "Trang sức", r"\bcharms?\b|jewel"),
    ("home_gift", "Quà tặng / đồ gia dụng", r"napkin|coaster|candle|glass|tumbler|\bmugs?\b|\bcups?\b|plate|tray|pillow"
                                            r"|\bprints?\b|ornament|decor|towel|puzzle|\bbooks?\b|notebook"
                                            r"|stationery|kitchen|dining|drinkware|blanket|gift|frame|shower curtain"
                                            r"|key ?chain|magnets?|\btags?\b|stir stick|needle minder"),
    ("tile_set", "Bộ quân (tile set)", r"\btiles?\b|mahjong sets|tile sets"),
]
_COMPILED = [(k, label, re.compile(rx, re.I)) for k, label, rx in RULES]
LABELS = {k: label for k, label, _ in RULES} | {"other": "Khác"}
ORDER = list(dict.fromkeys(k for k, _, _ in RULES)) + ["other"]


def classify(title: str | None, product_type: str | None = None) -> str:
    text = f"{title or ''} | {product_type or ''}"
    for key, _, rx in _COMPILED:
        if rx.search(text):
            return key
    return "other"


def add_category(products: pd.DataFrame) -> pd.DataFrame:
    p = products.copy()
    types = p["product_type"] if "product_type" in p else [None] * len(p)
    p["category"] = [classify(t, pt) for t, pt in zip(p["title"], types)]
    p["category_vi"] = p["category"].map(LABELS)
    return p


def priced(products: pd.DataFrame) -> pd.DataFrame:
    """Products usable for price comparison: USD, price > 0, not gift cards."""
    return products[(products["currency"] == "USD") & (products["min_price"] > 0) & (products["category"] != "gift_card")]
