"""Planner / crawler agent: decides WHAT to fetch within the budget, then fetches it.

1. robots.txt  -> what we are allowed to touch (guardrail)
2. sitemap.xml -> the pages the owner says matter (priority seeds)
3. BFS over internal links up to max_pages / max_depth, in parallel batches
4. cheap HEAD checks for everything else that was referenced (assets, external links,
   sitemap URLs beyond the page budget)
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .endpoints import HEALTH_PROBES, Endpoint, extract_from_js, plan
from .fetcher import Fetcher
from .htmlparse import normalize, parse_page
from .models import Page, Resource

LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


@dataclass
class Site:
    start_url: str
    origin: str
    host: str
    resources: dict = field(default_factory=dict)       # url -> Resource
    pages: list = field(default_factory=list)           # parsed Page objects
    robots_status: int | None = None
    sitemap_status: int | None = None
    sitemap_urls: list = field(default_factory=list)
    skipped_robots: set = field(default_factory=set)
    skipped_budget: set = field(default_factory=set)
    endpoints: list = field(default_factory=list)       # Endpoint objects (API calls, forms, probes)
    security_txt_status: int | None = None
    budget_hit: bool = False
    requests: int = 0

    @property
    def home(self) -> Resource | None:
        return self.resources.get(self.start_url)

    def is_internal(self, url: str) -> bool:
        return urlparse(url).netloc == self.host


async def crawl(start_url: str, cfg, fetcher: Fetcher) -> Site:
    start_url = normalize(start_url)
    u = urlparse(start_url)
    site = Site(start_url=start_url, origin=f"{u.scheme}://{u.netloc}", host=u.netloc)

    # --- 1. robots.txt
    robots = RobotFileParser()
    r = await fetcher.fetch(site.origin + "/robots.txt")
    site.robots_status = r.status
    lines = r.body.decode("utf-8", "replace").splitlines() if r.ok else []
    robots.parse(lines)

    def allowed(url: str) -> bool:
        if any(p in url for p in cfg.ignore_patterns):
            return False
        if cfg.respect_robots and site.is_internal(url) and not robots.can_fetch(cfg.user_agent, url):
            site.skipped_robots.add(url)
            return False
        return True

    # --- 2. sitemap(s)
    sitemaps = robots.site_maps() or [site.origin + "/sitemap.xml"]
    for sm in sitemaps[:3]:
        r = await fetcher.fetch(sm)
        site.sitemap_status = site.sitemap_status if site.sitemap_status == 200 else r.status
        if r.ok:
            locs = LOC_RE.findall(r.body.decode("utf-8", "replace"))
            for loc in locs:
                if loc.endswith(".xml"):     # sitemap index: one level deep
                    sub = await fetcher.fetch(loc)
                    if sub.ok:
                        site.sitemap_urls += LOC_RE.findall(sub.body.decode("utf-8", "replace"))
                else:
                    site.sitemap_urls.append(loc)
    site.sitemap_urls = list(dict.fromkeys(normalize(x) for x in site.sitemap_urls))

    def ref(url: str, kind: str, source: str) -> Resource:
        res = site.resources.get(url)
        if res is None:
            res = site.resources[url] = Resource(url=url)
        res.kinds.add(kind)
        res.sources.add(source)
        return res

    # --- 3. BFS crawl of HTML pages
    seen: set[str] = set()
    parsed_finals: set[str] = set()
    frontier = [(start_url, 0)]
    for sm_url in site.sitemap_urls:
        if site.is_internal(sm_url):
            ref(sm_url, "sitemap", "sitemap.xml")
            frontier.append((sm_url, 1))

    async def fetch_page(url: str) -> Resource:
        res = await fetcher.fetch(url)
        known = site.resources.get(url)
        if known:
            res.kinds, res.sources = known.kinds, known.sources
        res.kinds.add("page")
        site.resources[url] = res
        return res

    while frontier and len(seen) < cfg.max_pages and not fetcher.budget_hit:
        batch, nxt = [], []
        for url, depth in frontier:
            if url in seen or not allowed(url):
                continue
            if len(seen) >= cfg.max_pages:
                site.skipped_budget.add(url)
                continue
            seen.add(url)
            batch.append((url, depth))
        results = await asyncio.gather(*(fetch_page(url) for url, _ in batch))
        for (url, depth), res in zip(batch, results):
            if not (res.ok and res.is_html) or res.final_url in parsed_finals:
                continue
            parsed_finals.add(res.final_url)
            charset = res.content_type.split("charset=")[-1].strip() if "charset=" in res.content_type else "utf-8"
            page = parse_page(url, res.final_url, res.status, res.body, charset)
            site.pages.append(page)
            for kind, a in page.assets:
                ref(a, kind, url)
            for link in page.links:
                ref(link, "a", url)
                if site.is_internal(link) and depth + 1 <= cfg.max_depth and link not in seen:
                    nxt.append((link, depth + 1))
        frontier = nxt

    # --- 3.5 endpoint discovery: API calls in the site's own JS, GET forms, health probes
    await discover_endpoints(site, cfg, fetcher, allowed)

    # --- 4. cheap checks for everything referenced but not fetched yet
    todo, external = [], 0
    for url, res in list(site.resources.items()):
        if res.status is not None or res.error or url in seen or not url.startswith("http"):
            continue
        if not allowed(url):
            continue
        if not site.is_internal(url):
            if not cfg.check_external or external >= cfg.max_external:
                continue
            external += 1
        todo.append(url)

    async def head(url: str):
        r = await fetcher.check(url)
        r.kinds, r.sources = site.resources[url].kinds, site.resources[url].sources
        site.resources[url] = r

    await asyncio.gather(*(head(u) for u in todo))
    site.budget_hit = fetcher.budget_hit

    # --- 5. confirm slowness before reporting it: re-time slow pages one at a time.
    # During the crawl 8 big downloads compete for the same bandwidth/server, which inflates
    # TTFB; a lone re-test shows what a real visitor gets. Keep the faster measurement.
    await confirm_slow(site, cfg, fetcher)
    site.requests = fetcher.requests
    return site


async def discover_endpoints(site: Site, cfg, fetcher: Fetcher, allowed) -> None:
    scripts = [u for u, r in site.resources.items()
               if "script" in r.kinds and site.is_internal(u) and r.status is None and allowed(u)][: cfg.max_bundles]

    async def get_bundle(url):
        r = await fetcher.fetch(url)                       # full GET: we need the source to scan
        r.kinds, r.sources = site.resources[url].kinds, site.resources[url].sources
        site.resources[url] = r
        return url, r

    found: list[Endpoint] = []
    for url, r in await asyncio.gather(*(get_bundle(u) for u in scripts)):
        if r.ok and r.body:
            page_url = sorted(r.sources)[0] if r.sources else site.start_url
            found += extract_from_js(r.body.decode("utf-8", "replace"), url, page_url)
    for p in site.pages:
        for method, action in p.forms:
            if site.is_internal(action) and normalize(action) not in (p.url, p.final_url):
                found.append(Endpoint(action, method.upper(), f"form:{p.url}", False))
    found += [Endpoint(site.origin + h, "GET", "probe", False) for h in HEALTH_PROBES]

    site.endpoints = plan(found, site.host)
    for e in site.endpoints:
        if e.probe and not allowed(e.url):
            e.probe, e.reason = False, "disallowed by robots.txt / ignore list"

    async def probe(e: Endpoint):
        r = await fetcher.fetch(e.url, "GET", keep_body=False)
        e.status, e.error = r.status, r.error

    await asyncio.gather(*(probe(e) for e in site.endpoints if e.probe))
    st = await fetcher.fetch(site.origin + "/.well-known/security.txt", keep_body=False)
    site.security_txt_status = st.status


async def confirm_slow(site: Site, cfg, fetcher: Fetcher, limit: int = 20) -> None:
    slow = sorted((r for r in site.resources.values() if "page" in r.kinds and r.ok and r.ttfb_ms > cfg.slow_ms),
                  key=lambda r: -r.ttfb_ms)[:limit]
    fetcher.reserve = len(slow) * 3          # allowed even if the crawl budget is used up (+redirect hops)
    for r in slow:                           # sequential on purpose: no contention with ourselves
        again = await fetcher.fetch(r.url, "GET", keep_body=False, retries=0)
        if again.ok and again.ttfb_ms:
            r.ttfb_ms = min(r.ttfb_ms, again.ttfb_ms)
    fetcher.reserve = 0


def page_of(site: Site, url: str) -> Page | None:
    return next((p for p in site.pages if p.url == url), None)
