"""Tiny stdlib HTML extractor (no browser, no heavy deps -> fast & cheap)."""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

from .models import Page

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "#", "sms:")
TEXT_CAP = 50_000


def normalize(url: str) -> str:
    url, _ = urldefrag(url.strip())
    if url.count("/") == 2:          # "https://host" -> "https://host/"
        url += "/"
    return url


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


def parse_page(url: str, final_url: str, status: int, body: bytes, charset: str = "utf-8") -> Page:
    ex = _Extractor(final_url or url)
    try:
        ex.feed(body.decode(charset or "utf-8", errors="replace"))
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
