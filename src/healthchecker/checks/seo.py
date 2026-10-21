"""Discoverability: robots.txt, sitemap integrity, titles, descriptions, indexing."""
from ..models import Finding
from . import assessable, check


@check("seo")
async def seo(site, cfg):
    out = []
    if not assessable(site):
        return out
    root = site.origin + "/"
    prof = getattr(site, "profile", None)
    if prof and prof.robots_invalid:
        out.append(Finding("seo", "invalid_robots", "low", site.origin + "/robots.txt",
                           "robots.txt returns an HTML page, not robots rules (catch-all route?)"))
    elif site.robots_status != 200:
        out.append(Finding("seo", "missing_robots", "low", site.origin + "/robots.txt",
                           f"robots.txt not available (HTTP {site.robots_status})"))
    if site.sitemap_status != 200 and site.sitemap_invalid:
        out.append(Finding("seo", "invalid_sitemap", "low", site.sitemap_invalid[0],
                           "Sitemap URL returns something that isn't a sitemap (HTML page?)"))
    elif site.sitemap_status != 200:
        out.append(Finding("seo", "missing_sitemap", "low", root + "sitemap.xml",
                           f"sitemap.xml not available (HTTP {site.sitemap_status})"))
    if getattr(site, "sitemap_total", 0) > len(site.sitemap_urls):
        out.append(Finding("seo", "sitemap_sampled", "info", root + "sitemap.xml",
                           f"Sitemap lists {site.sitemap_total} URLs - checked a stratified sample of {len(site.sitemap_urls)}"))
    for url in site.sitemap_urls:
        r = site.resources.get(url)
        if r and not r.skipped and (r.error or (r.status or 0) >= 400):
            out.append(Finding("seo", "sitemap_broken_url", "medium", url,
                               f"Sitemap lists a URL that returns {r.status or r.error}"))

    for p in site.pages:
        if not site.is_internal(p.final_url):
            continue
        if not p.title:
            out.append(Finding("seo", "missing_title", "medium", p.url, "Page has no <title>"))
        elif len(p.title) > 70:
            out.append(Finding("seo", "long_title", "info", p.url, f"Title is {len(p.title)} chars"))
        if not p.meta_description:
            out.append(Finding("seo", "missing_meta_description", "low", p.url, "No meta description"))
        if not p.lang:
            out.append(Finding("seo", "missing_lang", "low", p.url, "<html> has no lang attribute (a11y + SEO)"))
        if p.h1_count == 0 and not p.client_rendered:
            out.append(Finding("seo", "missing_h1", "low", p.url, "Page has no <h1>"))
        res = site.resources.get(p.url)
        noindex = "noindex" in p.meta_robots or (res and "noindex" in res.headers.get("x-robots-tag", ""))
        if noindex and p.url == site.start_url:
            out.append(Finding("seo", "homepage_noindex", "high", p.url, "Homepage is marked noindex"))
    return out
