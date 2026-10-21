"""Broken links and broken assets (images, scripts, stylesheets, icons)."""
from ..models import Finding
from . import assessable, check

# Status codes from bot-hostile sites (LinkedIn 999, rate limits, WAF 403) mean
# "could not verify", not "broken". Reporting them would be noise.
UNVERIFIABLE = {401, 403, 429, 999}
ASSET_SEVERITY = {"script": "high", "css": "high", "img": "medium", "icon": "low"}


@check("links")
async def links(site, cfg):
    out = []
    if not assessable(site):
        return out
    for url, r in site.resources.items():
        if r.skipped or r.rate_limited or r.ok or r.status is None and not r.error:
            continue                          # rate-limited = we couldn't verify it, not "broken"
        internal = site.is_internal(url)
        if internal and "page" in r.kinds and (r.error or r.status >= 500):
            continue                          # availability owns broken internal pages
        sources = sorted(s for s in r.sources if s != "sitemap.xml")
        if not sources:
            continue                          # only referenced by sitemap -> seo check owns it
        if not internal and (r.status in UNVERIFIABLE):
            continue
        if internal and r.status in (401, 403) and not r.kinds & {"script", "css", "img", "icon"}:
            out.append(Finding("links", "access_denied", "low", url, f"Internal link requires access ({r.status})",
                               f"linked from: {', '.join(sources[:3])}"))
            continue                          # members-only / staff areas are normal, not broken
        status = "soft 404 - returns 200 with the not-found page" if r.soft404 else (r.status if r.status else r.error)
        asset_kinds = [k for k in ("script", "css", "img", "icon") if k in r.kinds]
        if asset_kinds:
            sev = ASSET_SEVERITY[asset_kinds[0]]
            out.append(Finding("links", "broken_asset", sev, url,
                               f"Broken {asset_kinds[0]} ({status})", f"used on: {', '.join(sources[:3])}"))
        else:
            out.append(Finding("links", "broken_link", "medium" if internal else "low", url,
                               f"Broken {'internal' if internal else 'external'} link ({status})",
                               f"linked from: {', '.join(sources[:3])}"))
    return out
