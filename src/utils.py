from __future__ import annotations

import re
from typing import Any

SKIP_IMAGE_PATTERNS = [
    "icon", "logo", "avatar", "banner", "qr_code", "qrcode",
    "loading", "pixel", "track", "beacon", "1x1", "btn", "button",
    "arrow", "back_top", "share", "collect", "cart",
]

_PLATFORM_PATTERNS = re.compile(
    r"(?P<taobao>taobao\.com)|(?P<alibaba>alibaba\.com|1688\.com)|(?P<xiaohongshu>xiaohongshu\.com|xhslink\.com)",
    re.IGNORECASE,
)


def detect_platform(url: str) -> str | None:
    m = _PLATFORM_PATTERNS.search(url)
    if m:
        for name in ("taobao", "alibaba", "xiaohongshu"):
            if m.group(name):
                return name
    return None


def make_handle_from_title(title: str, fallback: str = "") -> str:
    """Generate a filesystem-safe handle from a product title.

    Returns a URL/filesystem-safe slug. Non-ASCII characters (e.g. Chinese)
    are stripped; if the result is empty, the fallback is used.
    """
    base = title or fallback or "product"
    handle = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if not handle:
        handle = fallback[:40] if fallback else "product"
    return handle[:60]


def sanitize_filename(name: str) -> str:
    """Sanitize a string for safe use as a filename component."""
    name = re.sub(r"[^\w\s\-.]", "", name)
    name = re.sub(r"\s+", "-", name)
    name = name.strip("-.")
    return name or "sku"


def clean_price(v: Any) -> float:
    """Normalize price strings like '$18.99', '¥99.00', '18.99' → float."""
    if isinstance(v, str):
        v = v.strip().replace("$", "").replace(",", "").replace("¥", "").replace("￥", "")
        if not v:
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return 0.0
