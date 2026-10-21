"""Broken links and broken assets (images, scripts, stylesheets, icons)."""
from ..models import Finding
from . import check

# Status codes from bot-hostile sites (LinkedIn 999, rate limits, WAF 403) mean
# "could not verify", not "broken". Reporting them would be noise.
UNVERIFIABLE = {401, 403, 429, 999}
ASSET_SEVERITY = {"script": "high", "css": "high", "img": "medium", "icon": "low"}


@check("links")
async def links(site, cfg):
    out = []
    for url, r in site.resources.items():
        if r.skipped or r.ok or r.status is None and not r.error:
            continue
        internal = site.is_internal(url)
        if internal and "page" in r.kinds and (r.error or r.status >= 500):
            continue                          # availability owns broken internal pages
        sources = sorted(s for s in r.sources if s != "sitemap.xml")
        if not sources:
            continue                          # only referenced by sitemap -> seo check owns it
        if not internal and (r.status in UNVERIFIABLE):
            continue
        status = r.status if r.status else r.error
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
