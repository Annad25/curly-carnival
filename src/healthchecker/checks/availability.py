"""Is the site up? Do crawled pages return 2xx? Are redirects sane?"""
from ..models import Finding
from . import check


@check("availability")
async def availability(site, cfg):
    out = []
    prof = getattr(site, "profile", None)
    if prof and prof.blocked:
        return [Finding("availability", "blocked_by_bot_protection", "info", site.start_url,
                        f"Site is up but automated checks are challenged by {prof.blocked}",
                        f"HTTP {site.home.status if site.home else '?'} - challenges are never bypassed; "
                        "allow-list this checker's User-Agent/IP to monitor it")]
    if prof and prof.unreachable:
        return [Finding("availability", "site_down", "critical", site.start_url,
                        "Site is unreachable (DNS / connection failure)", prof.unreachable)]
    if prof:
        if prof.auth_required:
            out.append(Finding("availability", "auth_required", "info", site.start_url,
                               "Homepage redirects to a login - only public pages could be checked"))
        if prof.robots_disallow_all:
            out.append(Finding("availability", "robots_disallow_all", "info", site.start_url,
                               "robots.txt disallows crawling - only the homepage was checked"))
        if prof.soft404:
            out.append(Finding("availability", "soft_404", "low", site.start_url,
                               "Missing pages return 200 instead of 404 (soft 404) - search engines flag this "
                               "and uptime monitors can't see broken links"))
    if getattr(site, "rate_limited", 0):
        out.append(Finding("availability", "rate_limited", "info", site.start_url,
                           f"Server rate-limited the checker {site.rate_limited} times - slowed down automatically; "
                           "affected URLs were not judged"))
    traps = sum(getattr(site, "trap_skipped", {}).values())
    if traps >= 20:
        top = max(site.trap_skipped, key=site.trap_skipped.get)
        out.append(Finding("availability", "crawl_trap_avoided", "info", site.start_url,
                           f"{traps} near-duplicate / infinite URLs skipped (e.g. {top})",
                           "faceted navigation or calendars create endless URLs - wastes search engines' crawl budget too"))
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
        if r.error and "too many redirects" in r.error:
            continue                          # reported once below as redirect_loop
        if r.error:
            out.append(Finding("availability", "unreachable", "high", url, "Page could not be fetched", r.error))
        elif r.status >= 500:
            out.append(Finding("availability", "server_error", "high", url,
                               f"Page returns HTTP {r.status} (after {r.attempts} attempts)",
                               f"linked from: {', '.join(sorted(r.sources)[:3])}"))

    for url, r in site.resources.items():
        hops = len(r.redirects)
        if hops >= cfg.redirect_hops_warn and not r.error and site.is_internal(url):   # third-party chains aren't ours to fix
            out.append(Finding("availability", "redirect_chain", "low", url,
                               f"{hops} redirects before content", " -> ".join(r.redirects + [r.final_url])[:300]))
        if r.error and "too many redirects" in r.error:
            out.append(Finding("availability", "redirect_loop", "medium", url, "Redirect loop", r.error))
    if site.budget_hit:
        skipped = sum(1 for r in site.resources.values() if r.skipped)
        out.append(Finding("availability", "coverage_incomplete", "info", site.start_url,
                           f"Request budget ({cfg.max_requests}) reached - {skipped} URLs not checked",
                           "raise max_requests / max_pages for full coverage"))
    return out
