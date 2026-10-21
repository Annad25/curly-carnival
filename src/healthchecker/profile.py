"""Site profiler: a handful of requests that tell the crawler what kind of site it is facing.

Real websites are not uniform. Before crawling we answer:
  * what is the site's real identity?  (input normalisation, redirects, www/apex twins, sub-path scope)
  * is it reachable at all?            (DNS / connection / https->http fallback)
  * is its certificate valid?          (if not: report critical, keep assessing over an insecure client)
  * are we being blocked?              (WAF / bot challenge -> UNVERIFIABLE, never "DOWN", never bypassed)
  * is it behind a login?              (report it instead of crawling a login page 40 times)
  * does it answer 200 for anything?   (soft-404 -> switch broken-link detection to content comparison)
  * what do its rules say?             (robots.txt validity, Disallow-all, Crawl-delay)
  * how fast is it?                    (timeouts scale with observed latency)
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .fetcher import Fetcher
from .htmlparse import decode_html, normalize, parse_page
from .models import Resource

LOGIN_PATH_RE = re.compile(r"/(login|log-in|signin|sign-in|sign_in|auth|sso|oauth|account/login|users/sign_in|"
                           r"wp-login\.php|session/new)\b", re.I)

# (vendor, regex on body, statuses where the marker counts). Challenge pages are small; real pages aren't.
BLOCK_SIGNATURES = [
    ("Cloudflare", re.compile(r"(Just a moment\.\.\.|cf-chl-|challenge-platform|Attention Required! \| Cloudflare|"
                              r"cf_chl_opt|Enable JavaScript and cookies to continue)", re.I)),
    ("Imperva/Incapsula", re.compile(r"(Incapsula incident ID|_Incapsula_Resource|Request unsuccessful\. Incapsula)", re.I)),
    ("Akamai", re.compile(r"(Access Denied.{0,300}Reference #|errors\.edgesuite\.net|akamai-bot)", re.I | re.S)),
    ("DataDome", re.compile(r"(captcha-delivery\.com|geo\.captcha-delivery|datadome)", re.I)),
    ("PerimeterX/HUMAN", re.compile(r"(px-captcha|_pxCaptcha|perimeterx|human-challenge)", re.I)),
    ("Sucuri", re.compile(r"Sucuri WebSite Firewall", re.I)),
    ("AWS WAF/CloudFront", re.compile(r"(Request blocked\..{0,200}CloudFront|aws-waf-token|awswaf)", re.I | re.S)),
    ("generic captcha", re.compile(r"(g-recaptcha|h-captcha|hcaptcha\.com|turnstile|are you a robot|verify you are human)", re.I)),
]


@dataclass
class Profile:
    input_url: str
    start_url: str = ""
    hosts: set = field(default_factory=set)
    scope: str = "/"
    home: Resource | None = None
    unreachable: str | None = None
    tls_error: str | None = None
    http_fallback: bool = False
    blocked: str | None = None
    auth_required: bool = False
    soft404: bool = False
    soft404_sig: tuple = (frozenset(), "")   # (word set, final url) of the site's "not found" page
    robots: RobotFileParser | None = None
    robots_status: int | None = None
    robots_invalid: bool = False
    robots_disallow_all: bool = False
    crawl_delay: float = 0.0
    latency_ms: float = 0.0
    notes: list = field(default_factory=list)


def normalize_input(raw: str) -> str:
    """'Example.com', 'http://x/docs', 'https://bücher.de' ... -> canonical absolute URL. Raises ValueError."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty URL")
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"unsupported scheme '{parts.scheme}' (only http/https)")
    if parts.username or parts.password:
        raise ValueError("credentials in the URL are not supported (never pass secrets on the command line)")
    host = parts.hostname
    if not host or " " in host or "." not in host and host != "localhost":
        raise ValueError(f"not a valid host: {raw!r}")
    try:
        host = host.encode("idna").decode("ascii")          # internationalised domains
    except UnicodeError as e:
        raise ValueError(f"invalid international domain: {e}") from e
    netloc = host + (f":{parts.port}" if parts.port else "")
    return normalize(urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, "")))


def twin(host: str) -> str:
    """www.example.com <-> example.com are the same site for crawling purposes."""
    name, _, port = host.partition(":")
    other = name[4:] if name.startswith("www.") else "www." + name
    return other + (f":{port}" if port else "")


def detect_block(res: Resource) -> str | None:
    if res is None or res.status is None:
        return None
    if res.headers.get("cf-mitigated", "").lower() == "challenge":
        return "Cloudflare"
    small = len(res.body) < 30_000
    suspicious_status = res.status in (401, 403, 405, 429, 503)
    if not (suspicious_status or small):
        return None
    text = res.body[:30_000].decode("utf-8", "replace")
    for vendor, rx in BLOCK_SIGNATURES:
        if rx.search(text):
            # a captcha widget on a big normal 200 page (e.g. a contact form) is not a block
            if res.status == 200 and (vendor == "generic captcha" or len(text) > 15_000):
                continue
            return vendor
    return None


async def profile_site(raw_url: str, cfg, fetcher: Fetcher) -> Profile:
    prof = Profile(input_url=raw_url)
    start = normalize_input(raw_url)
    u = urlparse(start)
    fetcher.site_hosts |= {u.netloc, twin(u.netloc)}

    # --- reachability, TLS, identity
    home = await fetcher.fetch(start)
    if home.tls_error:
        prof.tls_error = home.tls_error
        fetcher.insecure_hosts |= {u.netloc, twin(u.netloc)}
        prof.notes.append("certificate invalid - assessed over an unverified connection")
        home = await fetcher.fetch(start)
    if home.error and not home.tls_error and u.scheme == "https":
        alt = await fetcher.fetch(start.replace("https://", "http://", 1))
        if alt.ok:
            prof.http_fallback = True
            prof.notes.append("https unreachable - site only works over plain http")
            home, start = alt, alt.url
    if home.error and not home.skipped:
        prof.unreachable = home.error
        prof.start_url, prof.home = start, home
        prof.hosts = {u.netloc, twin(u.netloc)}
        return prof

    final = urlparse(home.final_url or start)
    prof.hosts = {u.netloc, twin(u.netloc), final.netloc, twin(final.netloc)}
    fetcher.site_hosts |= prof.hosts
    if final.netloc != u.netloc:
        prof.notes.append(f"{u.netloc} redirects to {final.netloc} - both treated as this site")
    # an explicit sub-path input (e.g. /docs/) scopes the crawl; a redirect from / does not
    if u.path not in ("", "/"):
        prof.scope = u.path if u.path.endswith("/") else u.path.rsplit("/", 1)[0] + "/"
    prof.start_url = normalize(start)
    prof.home = home
    prof.latency_ms = home.ttfb_ms

    # --- blocked? (site is up, but we can't assess it honestly - and we never bypass challenges)
    prof.blocked = detect_block(home)
    if prof.blocked:
        prof.notes.append(f"automated requests are challenged by {prof.blocked}")
        return prof

    # --- login wall?
    if home.status == 401 or (LOGIN_PATH_RE.search(final.path) and not LOGIN_PATH_RE.search(u.path)):
        prof.auth_required = True
        prof.notes.append("homepage requires authentication - only public pages were checked")

    # --- robots.txt (served as HTML by many SPA hosts -> treat as absent, flag it)
    origin = f"{final.scheme}://{final.netloc}"
    robots = RobotFileParser()
    r = await fetcher.fetch(origin + "/robots.txt")
    prof.robots_status = r.status
    lines: list[str] = []
    if r.ok:
        text = decode_html(r.body, r.content_type)
        if "html" in r.content_type or text.lstrip()[:1] == "<":
            prof.robots_invalid = True
        else:
            lines = text.splitlines()
    robots.parse(lines)
    prof.robots = robots
    prof.robots_disallow_all = bool(lines) and not robots.can_fetch(cfg.user_agent, prof.start_url)
    delay = robots.crawl_delay(cfg.user_agent) or robots.crawl_delay("*") if lines else None
    if delay:
        prof.crawl_delay = float(delay)
        fetcher.set_crawl_delay(float(delay))
        prof.notes.append(f"robots.txt asks for Crawl-delay {delay}s - honoured (capped at 10s)")

    # --- soft-404 probe: a URL that cannot exist
    probe_url = f"{origin}{prof.scope}hc-missing-{secrets.token_hex(6)}"
    probe = None
    if not lines or robots.can_fetch(cfg.user_agent, probe_url):
        probe = await fetcher.fetch(probe_url, retries=0)
    if probe is not None and probe.status == 200 and not probe.error:
        prof.soft404 = True
        sig_page = parse_page(probe_url, probe.final_url, 200, probe.body, probe.content_type)
        prof.soft404_sig = (words(sig_page.text), normalize(probe.final_url))
        prof.notes.append("site answers 200 for missing pages (soft 404) - broken links detected by content")

    # --- latency-aware timeouts: a healthy-but-slow site shouldn't time out on us
    if prof.latency_ms > 1500:
        cfg.timeout_s = max(cfg.timeout_s, min(30.0, prof.latency_ms / 1000 * 4))
        fetcher.client.timeout = cfg.timeout_s
    return prof


def words(text: str) -> frozenset:
    return frozenset(re.findall(r"[a-z0-9]{3,}", text.lower()))


def looks_like_soft404(res: Resource, page_text: str, prof: Profile) -> bool:
    """Compare against the probe page by content, not title: SPAs often share one <title> site-wide."""
    if not prof.soft404:
        return False
    sig_words, sig_final = prof.soft404_sig
    final = normalize(res.final_url or res.url)
    if sig_final != prof.start_url and final == sig_final and normalize(res.url) != sig_final:
        return True                                   # every missing URL redirects to one "/404" page
    if sig_final == prof.start_url:
        return final == prof.start_url and normalize(res.url) != prof.start_url   # missing -> homepage
    w = words(page_text)
    if not sig_words or not w:
        return False
    return len(w & sig_words) / len(w | sig_words) >= 0.9      # near-identical content = the 404 template
