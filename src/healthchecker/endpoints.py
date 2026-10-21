"""Endpoint discovery: find the API calls a page's own JavaScript makes, without a browser.

Recognised patterns (static regex over the bundles we already download):
  axios.create({baseURL: "api"}) + client.get("/items")     -> GET  <page>/api/items
  fetch("/api/search?q=" + q)                               -> GET  /api/search
  fetch("/api/signup", {method: "POST", ...})               -> POST /api/signup
  "/api/whatever" string literals                           -> ?    /api/whatever

Safety rule: we only replay what the site itself does with GET. Anything else
(POST/PUT/DELETE/unknown) is inventoried, never called. Paths that look like they
change state (logout, delete, pay, ...) are never called, whatever the method.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlparse

PATH = r"(?:https?://[A-Za-z0-9.\-:]+)?/?[A-Za-z0-9_\-./]*[A-Za-z0-9_\-]"
BASE_RE = re.compile(r"""baseURL\s*:\s*["'`]([^"'`$\s]*)["'`]""")
CLIENT_RE = re.compile(r"""\b[\w$]+\.(get|post|put|patch|delete)\(\s*["'`](""" + PATH + r""")(?:\?[^"'`]*)?["'`]""")
FETCH_RE = re.compile(r"""\bfetch\(\s*["'`](""" + PATH + r""")(?:\?[^"'`]*)?["'`]\s*(,\s*\{[^}]{0,300})?""")
LITERAL_RE = re.compile(r"""["'`](/api/[A-Za-z0-9_\-./]*[A-Za-z0-9_\-])(?:\?[^"'`]*)?["'`]""")
METHOD_RE = re.compile(r"""method\s*:\s*["'`](\w+)["'`]""", re.I)
UNSAFE_RE = re.compile(r"(logout|log-out|signout|sign-out|delete|remove|destroy|unsubscribe|reset|cancel|"
                       r"pay|checkout|purchase|order|charge|refund|admin)", re.I)

HEALTH_PROBES = ["/health", "/healthz", "/api/health"]
MAX_ENDPOINTS = 25


@dataclass
class Endpoint:
    url: str
    method: str            # GET / POST / ... / UNKNOWN
    found_in: str          # js:<bundle>, form:<page>, probe
    probe: bool            # will we call it?
    reason: str = ""       # why not probed
    status: int | None = None
    error: str | None = None

    def to_dict(self):
        return asdict(self)


def _looks_like_path(p: str) -> bool:
    # drop things like "/" alone, file assets and regex-ish noise
    return len(p) > 1 and not re.search(r"\.(js|css|png|jpe?g|svg|gif|ico|woff2?|map|html?)$", p)


def extract_from_js(js: str, bundle_url: str, page_url: str) -> list[Endpoint]:
    bases = BASE_RE.findall(js)
    base = bases[0] if bases else ""
    found: dict[tuple[str, str], Endpoint] = {}

    def add(method: str, path: str, via_client: bool):
        if not _looks_like_path(path):
            return
        if via_client and base and not path.startswith(("http://", "https://")):
            path = base.rstrip("/") + "/" + path.lstrip("/")      # axios combineURLs
        url = urljoin(page_url, path)                              # relative bases resolve against the document
        found.setdefault((method, url), Endpoint(url, method, f"js:{bundle_url}", False))

    for method, path in CLIENT_RE.findall(js):
        # require a leading "/" or full URL: bare words are header names etc. (headers.get("set-cookie"))
        if path.startswith(("/", "http")):
            add(method.upper(), path, True)
    for path, opts in FETCH_RE.findall(js):
        m = METHOD_RE.search(opts or "")
        add(m.group(1).upper() if m else "GET", path, False)
    known_paths = {urlparse(e.url).path for e in found.values()}
    for path in LITERAL_RE.findall(js):
        if urlparse(urljoin(page_url, path)).path not in known_paths:
            add("UNKNOWN", path, False)
    return list(found.values())


def plan(endpoints: list[Endpoint], hosts) -> list[Endpoint]:
    """Apply the safety rules and decide which endpoints get a GET."""
    hosts = {hosts} if isinstance(hosts, str) else set(hosts)
    out, seen = [], set()
    for e in endpoints:
        key = (e.method, e.url)
        if key in seen:
            continue
        seen.add(key)
        if urlparse(e.url).netloc not in hosts:
            e.reason = "third-party host"
        elif UNSAFE_RE.search(urlparse(e.url).path):
            e.reason = "path looks state-changing"
        elif e.method != "GET":
            e.reason = f"{e.method} - inventoried only, never called"
        else:
            e.probe = True
        out.append(e)
    probed = [e for e in out if e.probe][:MAX_ENDPOINTS]
    for e in out:
        if e.probe and e not in probed:
            e.probe, e.reason = False, "over endpoint budget"
    return out
