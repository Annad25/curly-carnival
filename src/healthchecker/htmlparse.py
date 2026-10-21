"""Tiny stdlib HTML extractor (no browser, no heavy deps -> fast & cheap)."""
from __future__ import annotations

from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

from .models import Page

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "#", "sms:")
TEXT_CAP = 50_000


TRACKING_PARAMS = re.compile(r"^(utm_\w+|gclid|fbclid|msclkid|mc_cid|mc_eid|_ga|_gl|ref|ref_src|igshid|"
                             r"sessionid|session_id|sid|phpsessid|jsessionid|cfid|cftoken)$", re.I)
DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize(url: str) -> str:
    """Canonical form used as the dedupe key everywhere: lower-case scheme/host, no default port,
    no fragment, no tracking/session params, sorted query. Session ids in URLs are the classic
    crawler trap - every visit mints 'new' URLs for the same page."""
    url, _ = urldefrag(url.strip())
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return url
    host = parts.hostname.lower()
    if parts.port and parts.port != DEFAULT_PORTS.get(parts.scheme):
        host = f"{host}:{parts.port}"
    path = re.sub(r";jsessionid=[^/?]*", "", parts.path, flags=re.I) or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not TRACKING_PARAMS.match(k)))
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def decode_html(body: bytes, content_type: str = "") -> str:
    """Charset: HTTP header -> <meta charset> -> utf-8 -> cp1252 (never raises)."""
    candidates = []
    if "charset=" in content_type:
        candidates.append(content_type.split("charset=")[-1].split(";")[0].strip(" \"'"))
    if m := META_CHARSET_RE.search(body[:4096]):
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp1252"]
    for enc in candidates:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


class _Extractor(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.p = {"title": "", "desc": "", "robots": "", "lang": "", "h1": 0, "canonical": "",
                  "links": [], "assets": [], "noalt": [], "forms": []}
        self._in_title = False
        self._skip_depth = 0
        self._text: list[str] = []
        self._text_len = 0

    def _abs(self, href: str | None) -> str | None:
        if not href:
            return None
        href = href.strip()
        if not href or href.lower().startswith(SKIP_SCHEMES):
            return None
        return normalize(urljoin(self.base, href))

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        p = self.p
        if tag in ("script", "style", "noscript", "template"):
            self._skip_depth += 1
            if tag == "script" and (u := self._abs(a.get("src"))):
                p["assets"].append(("script", u))
        elif tag == "html":
            p["lang"] = a.get("lang", "")
        elif tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            p["h1"] += 1
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "description":
                p["desc"] = a.get("content", "")
            elif name == "robots":
                p["robots"] = a.get("content", "").lower()
        elif tag == "link":
            rel = a.get("rel", "").lower()
            u = self._abs(a.get("href"))
            if u and "canonical" in rel:
                p["canonical"] = u
            elif u and "stylesheet" in rel:
                p["assets"].append(("css", u))
            elif u and "icon" in rel:
                p["assets"].append(("icon", u))
        elif tag == "a":
            if u := self._abs(a.get("href")):
                p["links"].append(u)
        elif tag == "img":
            src = self._abs(a.get("src"))
            if src:
                p["assets"].append(("img", src))
            if "alt" not in a and a.get("role") != "presentation" and a.get("aria-hidden") != "true":
                p["noalt"].append(src or "<inline img>")
        elif tag == "form":
            p["forms"].append((a.get("method", "get").lower(), self._abs(a.get("action")) or self.base))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.p["title"] += data
        elif not self._skip_depth and self._text_len < TEXT_CAP:
            s = data.strip()
            if s:
                self._text.append(s)
                self._text_len += len(s) + 1


def parse_page(url: str, final_url: str, status: int, body: bytes, content_type: str = "") -> Page:
    ex = _Extractor(final_url or url)
    try:
        ex.feed(decode_html(body, content_type))
        ex.close()
    except Exception:  # malformed html must never crash a health run
        pass
    p = ex.p
    return Page(
        url=url, final_url=final_url, status=status,
        title=" ".join(p["title"].split()), meta_description=p["desc"].strip(), meta_robots=p["robots"],
        lang=p["lang"], h1_count=p["h1"], canonical=p["canonical"],
        links=list(dict.fromkeys(p["links"])), assets=list(dict.fromkeys(p["assets"])),
        imgs_missing_alt=p["noalt"], forms=p["forms"], text=" ".join(ex._text)[:TEXT_CAP],
    )
