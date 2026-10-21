"""API endpoints the site's own JavaScript calls, GET form targets, health probes, security.txt.

A page can render fine while the API behind it is dead - users then see a broken
app and every other check says "healthy". This closes that gap without a browser.
"""
from ..models import Finding
from . import check


@check("endpoints")
async def endpoints(site, cfg):
    out = []
    for e in site.endpoints:
        if not e.probe:
            continue
        broken = e.error is not None or (e.status or 0) >= 500
        status = e.status or e.error
        if e.found_in.startswith("js:"):
            if broken:
                out.append(Finding("endpoints", "api_endpoint_down", "high", e.url,
                                   f"API endpoint used by the site's JS is failing ({status})",
                                   f"called via GET in {e.found_in[3:]}"))
            elif e.status in (404, 410):
                out.append(Finding("endpoints", "api_endpoint_missing", "medium", e.url,
                                   f"API endpoint the site's JS calls with GET returns {e.status}",
                                   f"called in {e.found_in[3:]}"))
        elif e.found_in.startswith("form:"):
            if broken or (e.status or 0) >= 400:
                out.append(Finding("endpoints", "form_target_broken", "medium", e.url,
                                   f"Form submits to a URL that returns {status}", f"form on {e.found_in[5:]}"))
        elif broken:                                    # health probes: only care if they exist and fail
            out.append(Finding("endpoints", "api_endpoint_down", "high", e.url, f"Health endpoint failing ({status})"))
    if site.security_txt_status is not None and site.security_txt_status != 200:
        out.append(Finding("endpoints", "missing_security_txt", "low", site.origin + "/.well-known/security.txt",
                           "No security.txt - researchers have no way to report vulnerabilities (RFC 9116)"))
    return out
