"""Planner / crawler agent: decides WHAT to fetch within the budget, then fetches it.

0. profile      -> identity, reachability, TLS, block/login/soft-404 detection, robots rules (profile.py)
1. sitemaps     -> robots Sitemap: lines or /sitemap.xml; gzip + nested indexes; sampled if huge
2. crawl        -> section-aware (round-robin across /blog, /docs, ...) instead of plain BFS, so the page
                   budget covers the site's structure, with crawler-trap protection
3. endpoints    -> API calls in the site's own JS, GET forms, health probes (endpoints.py)
4. link checks  -> HEAD/GET for referenced assets & links, in priority tiers so a tight budget is spent
                   on what matters most (page assets > internal links > sitemap sample > external)
5. confirm      -> re-time slow pages alone before calling them slow
"""
from __future__ import annotations

import asyncio
import re
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .endpoints import HEALTH_PROBES, Endpoint, extract_from_js, plan
from .fetcher import Fetcher
from .htmlparse import decode_html, normalize, parse_page
from .models import Page, Resource
from .profile import Profile, looks_like_soft404, profile_site

LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)\s*(?:\]\]>)?\s*</loc>", re.I)
MAX_SITEMAP_URLS = 50_000          # sitemap protocol limit per file; we stop collecting beyond this
MAX_GUNZIP = 50_000_000


@dataclass
class Site:
    start_url: str
    origin: str
    hosts: set = field(default_factory=set)
    scope: str = "/"
    profile: Profile | None = None
    resources: dict = field(default_factory=dict)       # url -> Resource
    pages: list = field(default_factory=list)           # parsed Page objects
    robots_status: int | None = None
    sitemap_status: int | None = None
    sitemap_invalid: list = field(default_factory=list)  # sitemap URLs that weren't XML
    sitemap_urls: list = field(default_factory=list)    # the (sampled) URLs we check
    sitemap_total: int = 0
    skipped_robots: set = field(default_factory=set)
    skipped_budget: set = field(default_factory=set)
    trap_skipped: dict = field(default_factory=dict)    # path -> number of query variants not crawled
    trap_urls: set = field(default_factory=set)         # those URLs: neither crawled nor link-checked
    endpoints: list = field(default_factory=list)
    security_txt_status: int | None = None
    budget_hit: bool = False
    requests: int = 0
    rate_limited: int = 0

    @property
    def host(self) -> str:
        return urlparse(self.start_url).netloc

    @property
    def home(self) -> Resource | None:
        return self.resources.get(self.start_url)

    def is_internal(self, url: str) -> bool:
        return urlparse(url).netloc in self.hosts

    def in_scope(self, url: str) -> bool:
        return self.is_internal(url) and (urlparse(url).path or "/").startswith(self.scope)


def section_of(url: str) -> str:
    seg = [s for s in urlparse(url).path.split("/") if s]
    return seg[0].lower() if seg else "/"


def is_trap(url: str) -> bool:
    """Shapes of URL that are infinite by construction."""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if len(url) > 400 or len(segs) > 10:
        return True
    if any(segs.count(s) >= 3 for s in set(segs)):                  # /a/b/a/b/a/b relative-link loops
        return True
    return bool(re.search(r"/(19|20)\d\d/\d\d?/\d\d?/.*(19|20)\d\d/", p.path))   # nested calendars


def decode_sitemap(res: Resource) -> tuple[str, list[str]] | None:
    """-> ("index"|"urlset", locs) or None if this isn't a sitemap (e.g. an SPA serving HTML)."""
    body = res.body
    if body[:2] == b"\x1f\x8b":                                      # .xml.gz served raw
        try:
            body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, MAX_GUNZIP)
        except zlib.error:
            return None
    text = decode_html(body, res.content_type)
    head = text[:5000]
    if "<sitemapindex" in head:
        return "index", LOC_RE.findall(text)
    if "<urlset" in head:
        return "urlset", LOC_RE.findall(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and all(ln.startswith(("http://", "https://")) for ln in lines[:50]):
        return "urlset", lines                                       # plain-text sitemap (allowed by the protocol)
    return None


async def crawl(start_url: str, cfg, fetcher: Fetcher) -> Site:
    prof = await profile_site(start_url, cfg, fetcher)
    u = urlparse(prof.start_url)
    site = Site(start_url=prof.start_url, origin=f"{u.scheme}://{u.netloc}", hosts=prof.hosts,
                scope=prof.scope, profile=prof, robots_status=prof.robots_status)
    if prof.home is not None:
        prof.home.kinds.add("page")
        site.resources[site.start_url] = prof.home
    if prof.unreachable or prof.blocked:
        return _finish(site, fetcher)                                  # nothing more we can honestly assess

    robots = prof.robots

    def allowed(url: str) -> bool:
        if any(p in url for p in cfg.ignore_patterns):
            return False
        if cfg.respect_robots and site.is_internal(url) and robots is not None \
                and not robots.can_fetch(cfg.user_agent, url):
            site.skipped_robots.add(url)
            return False
        return True

    def ref(url: str, kind: str, source: str) -> Resource:
        res = site.resources.get(url)
        if res is None:
            res = site.resources[url] = Resource(url=url)
        res.kinds.add(kind)
        res.sources.add(source)
        return res

    # --- 1. sitemaps
    await load_sitemaps(site, cfg, fetcher, robots)
    for sm_url in site.sitemap_urls:
        if site.is_internal(sm_url):
            ref(sm_url, "sitemap", "sitemap.xml")

    # --- 2. section-aware crawl
    queues: dict[str, deque] = defaultdict(deque)
    queued: set[str] = set()
    variants: dict[str, int] = defaultdict(int)
    per_section: dict[str, int] = defaultdict(int)
    section_cap = max(5, int(cfg.max_pages * cfg.max_section_share))

    def enqueue(url: str, depth: int):
        if url in queued or not site.in_scope(url) or depth > cfg.max_depth:
            return
        p = urlparse(url)
        if is_trap(url):
            site.trap_skipped[p.path] = site.trap_skipped.get(p.path, 0) + 1
            site.trap_urls.add(url)
            return
        if p.query:
            key = p.netloc + p.path
            if variants[key] >= cfg.max_query_variants:               # ?page=1..inf, ?sort=, calendars
                site.trap_skipped[p.path] = site.trap_skipped.get(p.path, 0) + 1
                site.trap_urls.add(url)
                return
            variants[key] += 1
        queued.add(url)
        queues[section_of(url)].append((url, depth))

    queued.add(site.start_url)
    home_res = prof.home
    crawled: set[str] = set()
    parsed_finals: set[str] = set()

    def absorb(url: str, depth: int, res: Resource):
        """Turn a fetched HTML page into a Page and queue what it links to."""
        if not (res.ok and res.is_html) or normalize(res.final_url) in parsed_finals:
            return
        page = parse_page(url, res.final_url, res.status, res.body, res.content_type)
        if url != site.start_url and looks_like_soft404(res, page.text, prof):
            res.soft404 = True
            return
        parsed_finals.add(normalize(res.final_url))
        site.pages.append(page)
        for kind, a in page.assets:
            ref(a, kind, url)
        for link in page.links:
            ref(link, "a", url)
            if allowed(link):
                enqueue(link, depth + 1)

    crawled.add(site.start_url)
    absorb(site.start_url, 0, home_res)
    for sm_url in site.sitemap_urls:
        enqueue(sm_url, 1)

    async def fetch_page(url: str) -> Resource:
        res = await fetcher.fetch(url)
        known = site.resources.get(url)
        if known:
            res.kinds, res.sources = known.kinds, known.sources
        res.kinds.add("page")
        site.resources[url] = res
        return res

    batch_size = max(2, cfg.concurrency * 2)
    while len(crawled) < cfg.max_pages and any(queues.values()) and not fetcher.budget_hit:
        batch = []
        while len(batch) < batch_size and len(crawled) + len(batch) < cfg.max_pages and any(queues.values()):
            for sec in list(queues):                                  # round-robin across sections
                if not queues[sec] or len(batch) >= batch_size or len(crawled) + len(batch) >= cfg.max_pages:
                    continue
                url, depth = queues[sec].popleft()
                if per_section[sec] >= section_cap:
                    site.skipped_budget.add(url)
                    continue
                per_section[sec] += 1
                batch.append((url, depth))
        if not batch:
            break
        results = await asyncio.gather(*(fetch_page(url) for url, _ in batch))
        for (url, depth), res in zip(batch, results):
            crawled.add(url)
            absorb(url, depth, res)
    for q in queues.values():
        site.skipped_budget |= {url for url, _ in q}

    # --- 3. endpoint discovery
    await discover_endpoints(site, cfg, fetcher, allowed)

    # --- 4. link / asset checks in priority tiers
    pending = [(url, r) for url, r in site.resources.items()
               if r.status is None and not r.error and url.startswith("http") and url not in crawled
               and url not in site.trap_urls and allowed(url)]

    def tier(item) -> int:
        url, r = item
        if not site.is_internal(url):
            return 4
        if r.kinds & {"script", "css"}:
            return 0
        if r.kinds & {"img", "icon"}:
            return 1
        if "a" in r.kinds:
            return 2
        return 3                                                       # sitemap-only

    external_budget = cfg.max_external if cfg.check_external else 0
    tiers: dict[int, list] = defaultdict(list)
    for item in sorted(pending, key=tier):
        t = tier(item)
        if t == 4:
            if external_budget <= 0:
                continue
            external_budget -= 1
        tiers[t].append(item[0])

    async def check(url: str):
        old = site.resources[url]
        if prof.soft404 and site.is_internal(url) and "a" in old.kinds:
            r = await fetcher.fetch(url)                               # need the body to spot soft 404s
            if r.ok and r.is_html:
                pg = parse_page(url, r.final_url, r.status, r.body, r.content_type)
                r.soft404 = looks_like_soft404(r, pg.text, prof)
            r.body = b""
        else:
            r = await fetcher.check(url)
        r.kinds, r.sources = old.kinds, old.sources
        site.resources[url] = r

    for t in sorted(tiers):
        if fetcher.budget_hit:
            for url in tiers[t]:
                site.resources[url].skipped = True
                site.resources[url].error = "skipped: crawl budget exhausted"
            continue
        await asyncio.gather(*(check(u) for u in tiers[t]))

    # --- 5. confirm slowness before reporting it
    await confirm_slow(site, cfg, fetcher)
    return _finish(site, fetcher)


def _finish(site: Site, fetcher: Fetcher) -> Site:
    site.budget_hit = fetcher.budget_hit
    site.requests = fetcher.requests
    site.rate_limited = fetcher.rate_limited
    for url, r in site.resources.items():      # bodies were only needed for parsing; free the memory
        if url != site.start_url:
            r.body = b""
    return site


async def load_sitemaps(site: Site, cfg, fetcher: Fetcher, robots) -> None:
    declared = (robots.site_maps() if robots else None) or []
    default = site.origin + "/sitemap.xml"
    if not declared and robots is not None and not robots.can_fetch(cfg.user_agent, default):
        return                                    # undeclared + disallowed: don't guess
    todo = deque((normalize(s), 0) for s in declared) or deque([(default, 0)])
    seen, urls, files, valid = set(), [], 0, False
    while todo and files < cfg.max_sitemap_files and len(urls) < MAX_SITEMAP_URLS:
        sm, depth = todo.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        files += 1
        r = await fetcher.fetch(sm)
        if not valid:
            site.sitemap_status = r.status
        if not r.ok:
            continue
        parsed = decode_sitemap(r)
        if parsed is None:
            site.sitemap_invalid.append(sm)                         # 200 but not a sitemap (HTML catch-all)
            continue
        valid = True
        kind, locs = parsed
        if kind == "index":
            if depth < 2:
                todo.extend((normalize(x), depth + 1) for x in locs)
        else:
            urls.extend(normalize(x) for x in locs[: MAX_SITEMAP_URLS - len(urls)])
    site.sitemap_status = 200 if valid else site.sitemap_status
    urls = list(dict.fromkeys(urls))
    site.sitemap_total = len(urls)
    site.sitemap_urls = stratified_sample(urls, cfg.max_sitemap_checks)


def stratified_sample(urls: list[str], k: int) -> list[str]:
    """Round-robin across sections so a 20k-URL sitemap is represented, not just its first section."""
    if len(urls) <= k:
        return urls
    groups: dict[str, deque] = defaultdict(deque)
    for u in urls:
        groups[section_of(u)].append(u)
    out: list[str] = []
    while len(out) < k:
        for g in list(groups.values()):
            if g and len(out) < k:
                out.append(g.popleft())
    return out


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

    site.endpoints = plan(found, site.hosts)
    for e in site.endpoints:
        if e.probe and not allowed(e.url):
            e.probe, e.reason = False, "disallowed by robots.txt / ignore list"

    async def probe(e: Endpoint):
        r = await fetcher.fetch(e.url, "GET", keep_body=False)
        e.status, e.error = r.status, r.error

    await asyncio.gather(*(probe(e) for e in site.endpoints if e.probe))
    if not allowed(site.origin + "/.well-known/security.txt"):
        return
    st = await fetcher.fetch(site.origin + "/.well-known/security.txt")
    body = decode_html(st.body, st.content_type) if st.ok else ""
    # a 200 HTML page (SPA catch-all) is not a security.txt
    site.security_txt_status = st.status if (st.ok and "contact:" in body.lower()) or not st.ok else 404


async def confirm_slow(site: Site, cfg, fetcher: Fetcher, limit: int = 20) -> None:
    slow = sorted((r for r in site.resources.values() if "page" in r.kinds and r.ok and r.ttfb_ms > cfg.slow_ms),
                  key=lambda r: -r.ttfb_ms)[:limit]
    fetcher.reserve = len(slow) * 6          # allowed even if the crawl budget is used up (+redirect hops)
    for r in slow:                           # sequential on purpose: no contention with ourselves
        for _ in range(2):                   # first hit may be a cold CDN cache: best of up to 3 samples
            again = await fetcher.fetch(r.url, "GET", keep_body=False, retries=0)
            if again.ok and again.ttfb_ms:
                r.ttfb_ms = min(r.ttfb_ms, again.ttfb_ms)
            if r.ttfb_ms <= cfg.slow_ms:
                break
    fetcher.reserve = 0


def page_of(site: Site, url: str) -> Page | None:
    return next((p for p in site.pages if p.url == url), None)
