"""Plain data objects shared by every stage of the pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SEVERITIES = ["info", "low", "medium", "high", "critical"]
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class Finding:
    check: str          # which checker produced it (availability, links, ...)
    code: str           # stable machine code, e.g. "broken_link"
    severity: str       # one of SEVERITIES
    url: str            # where the problem is
    message: str        # human readable one-liner
    evidence: str = ""  # short supporting detail (may contain UNTRUSTED site text)

    @property
    def key(self) -> str:
        return f"{self.code}|{self.url}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d


@dataclass
class Resource:
    """Result of fetching one URL (page, asset or link)."""
    url: str
    final_url: str = ""
    status: int | None = None
    ttfb_ms: float = 0.0
    total_ms: float = 0.0
    headers: dict = field(default_factory=dict)
    content_type: str = ""
    size: int = 0
    body: bytes = b""
    redirects: list = field(default_factory=list)
    error: str | None = None
    attempts: int = 1
    skipped: bool = False                       # not fetched (budget / robots)
    rate_limited: bool = False                  # server answered 429 / Retry-After even after backing off
    tls_error: str | None = None                # certificate problem (site may still be assessed insecurely)
    soft404: bool = False                       # 200 OK but it's the site's "not found" page
    kinds: set = field(default_factory=set)     # page, a, img, script, css, icon, sitemap
    sources: set = field(default_factory=set)   # pages that reference it

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400 and not self.soft404

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type


@dataclass
class Page:
    """Parsed view of an HTML page."""
    url: str
    final_url: str
    status: int
    title: str = ""
    meta_description: str = ""
    meta_robots: str = ""
    lang: str = ""
    h1_count: int = 0
    canonical: str = ""
    links: list = field(default_factory=list)          # absolute <a href> urls
    assets: list = field(default_factory=list)         # (kind, absolute url)
    imgs_missing_alt: list = field(default_factory=list)
    forms: list = field(default_factory=list)          # (method, action) – recorded, never submitted
    text: str = ""                                     # visible text (truncated)

    @property
    def client_rendered(self) -> bool:
        """SPA shell: almost no server-rendered text but it ships JS that renders it."""
        return len(self.text) < 50 and any(k == "script" for k, _ in self.assets)
