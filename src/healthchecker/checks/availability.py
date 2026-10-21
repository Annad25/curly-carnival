"""Is the site up? Do crawled pages return 2xx? Are redirects sane?"""
from ..models import Finding
from . import check


@check("availability")
async def availability(site, cfg):
    out = []
    home = site.home
    if home is None or home.error or (home.status or 0) >= 500:
        detail = home.error if home else "not fetched"
        out.append(Finding("availability", "site_down", "critical", site.start_url,
                           "Homepage is unreachable or erroring", f"{home.status if home else ''} {detail or ''}".strip()))
        return out
    if home.status >= 400:
        out.append(Finding("availability", "homepage_error", "critical", site.start_url,
                           f"Homepage returns HTTP {home.status}"))

    for url, r in site.resources.items():
        if "page" not in r.kinds or r.skipped or not site.is_internal(url) or url == site.start_url:
            continue
        if r.error:
            out.append(Finding("availability", "unreachable", "high", url, "Page could not be fetched", r.error))
        elif r.status >= 500:
            out.append(Finding("availability", "server_error", "high", url,
                               f"Page returns HTTP {r.status} (after {r.attempts} attempts)",
                               f"linked from: {', '.join(sorted(r.sources)[:3])}"))

    for url, r in site.resources.items():
        hops = len(r.redirects)
        if hops >= cfg.redirect_hops_warn:
            out.append(Finding("availability", "redirect_chain", "low", url,
                               f"{hops} redirects before content", " -> ".join(r.redirects + [r.final_url])[:300]))
        if r.error and "too many redirects" in r.error:
            out.append(Finding("availability", "redirect_loop", "medium", url, "Redirect loop", r.error))
    return out
