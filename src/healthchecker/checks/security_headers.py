"""Security response headers on the homepage (site-wide config lives there)."""
import re
from urllib.parse import urlparse

from ..models import Finding
from . import check


@check("security_headers")
async def security_headers(site, cfg):
    home = site.home
    if not home or not home.ok:
        return []
    h = home.headers
    url = site.start_url
    https = urlparse(home.final_url or url).scheme == "https"
    out = []
    if https and "strict-transport-security" not in h:
        out.append(Finding("security_headers", "missing_hsts", "medium", url,
                           "Strict-Transport-Security header missing (allows SSL-strip downgrade)"))
    csp = h.get("content-security-policy", "")
    if not csp:
        out.append(Finding("security_headers", "missing_csp", "medium", url,
                           "Content-Security-Policy header missing (no XSS mitigation)"))
    if h.get("x-content-type-options", "").lower() != "nosniff":
        out.append(Finding("security_headers", "missing_xcto", "low", url, "X-Content-Type-Options: nosniff missing"))
    if "x-frame-options" not in h and "frame-ancestors" not in csp:
        out.append(Finding("security_headers", "missing_frame_protection", "low", url,
                           "No X-Frame-Options or CSP frame-ancestors (clickjacking)"))
    server = h.get("server", "") + " " + h.get("x-powered-by", "")
    if re.search(r"[A-Za-z]+/\d+(\.\d+)+", server):
        out.append(Finding("security_headers", "server_version_leak", "low", url,
                           "Server software version disclosed", server.strip()))
    cookie = h.get("set-cookie", "")
    if cookie and https and ("secure" not in cookie.lower() or "httponly" not in cookie.lower()):
        out.append(Finding("security_headers", "insecure_cookie", "low", url,
                           "Cookie set without Secure/HttpOnly", cookie[:120]))
    return out
