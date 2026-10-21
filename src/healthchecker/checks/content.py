"""Content-level breakage a status code won't show: soft errors, leaked templates,
mixed content, missing alt text, injected/defaced text."""
import re
from urllib.parse import urlparse

from ..guardrails import INJECTION_RE
from ..models import Finding
from . import assessable, check

SOFT_ERROR_RE = re.compile(
    r"(Traceback \(most recent call last\)|Internal Server Error|Fatal error:|Uncaught (Type|Reference)Error"
    r"|Exception in thread|Stack trace:|An error occurred while processing your request|Application error)", re.I)
TEMPLATE_RE = re.compile(r"(\{\{\s*[\w.\[\]]+\s*\}\}|\$\{[\w.]+\}|\[object Object\]|\{%\s*\w+)")
PLACEHOLDER_RE = re.compile(r"\blorem ipsum\b", re.I)


def _snip(text: str, m: re.Match) -> str:
    return text[max(0, m.start() - 40): m.end() + 40]


@check("content")
async def content(site, cfg):
    if not assessable(site):
        return []
    out = []
    for p in site.pages:
        if not site.is_internal(p.final_url):
            continue
        t = p.text
        if m := SOFT_ERROR_RE.search(t):
            out.append(Finding("content", "soft_error", "high", p.url,
                               "Page returns 200 but shows an error message", _snip(t, m)))
        if m := TEMPLATE_RE.search(t):
            out.append(Finding("content", "template_leak", "medium", p.url,
                               "Unrendered template / JS value visible to users", _snip(t, m)))
        if m := PLACEHOLDER_RE.search(t):
            out.append(Finding("content", "placeholder_text", "low", p.url, "Placeholder text on page", _snip(t, m)))
        if m := INJECTION_RE.search(t):
            out.append(Finding("content", "suspicious_injected_text", "medium", p.url,
                               "Page contains prompt-injection style text (possible defacement / SEO spam)", _snip(t, m)))
        if p.client_rendered:
            out.append(Finding("content", "client_rendered", "info", p.url,
                               "Content is rendered by JavaScript - static crawl sees an empty shell "
                               "(its JS bundles are still checked by the links checker)"))
        elif len(t) < 50:
            out.append(Finding("content", "empty_page", "medium", p.url,
                               f"Page has almost no visible text ({len(t)} chars) - blank render?"))
        if p.imgs_missing_alt:
            out.append(Finding("content", "img_missing_alt", "low", p.url,
                               f"{len(p.imgs_missing_alt)} image(s) without alt text", ", ".join(p.imgs_missing_alt[:3])))
        if urlparse(p.final_url).scheme == "https":
            insecure = [u for _, u in p.assets if u.startswith("http://")]
            if insecure:
                out.append(Finding("content", "mixed_content", "medium", p.url,
                                   f"{len(insecure)} asset(s) loaded over http on an https page", ", ".join(insecure[:3])))
    return out
