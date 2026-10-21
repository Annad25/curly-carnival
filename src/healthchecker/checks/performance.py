"""Server-side speed and page weight (no browser needed)."""
from ..models import Finding
from . import check


@check("performance")
async def performance(site, cfg):
    out = []
    for p in site.pages:
        r = site.resources.get(p.url)
        if not r or not site.is_internal(p.final_url):
            continue
        if r.ttfb_ms > cfg.very_slow_ms:
            out.append(Finding("performance", "slow_response", "high", p.url, f"Time to first byte {r.ttfb_ms:.0f} ms"))
        elif r.ttfb_ms > cfg.slow_ms:
            out.append(Finding("performance", "slow_response", "medium", p.url, f"Time to first byte {r.ttfb_ms:.0f} ms"))
        if r.size > cfg.heavy_bytes:
            out.append(Finding("performance", "page_too_heavy", "medium", p.url, f"HTML document is {r.size / 1e6:.1f} MB"))
        if r.size > cfg.compress_min_bytes and not r.headers.get("content-encoding"):
            out.append(Finding("performance", "no_compression", "low", p.url,
                               f"{r.size // 1024} KB served without gzip/brotli"))
    return out
