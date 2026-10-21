"""DNS, TLS certificate and HTTPS enforcement."""
import asyncio
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from ..models import Finding
from . import check

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def _is_local(host: str) -> bool:
    h = host.split(":")[0] if not host.startswith("[") else host
    return h in LOCAL_HOSTS or h.endswith(".localhost") or h.endswith(".test")


@check("dns")
async def dns(site, cfg):
    host = urlparse(site.start_url).hostname
    t0 = time.perf_counter()
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as e:
        return [Finding("dns", "dns_failure", "critical", site.start_url, f"DNS lookup failed for {host}", str(e))]
    ms = (time.perf_counter() - t0) * 1000
    out = []
    if ms > 500:
        out.append(Finding("dns", "slow_dns", "low", site.start_url, f"DNS resolution took {ms:.0f} ms"))
    if not _is_local(host) and not any(i[0] == socket.AF_INET6 for i in infos):
        out.append(Finding("dns", "no_ipv6", "info", site.start_url, "No AAAA (IPv6) record"))
    return out


def _cert_expiry(host: str, port: int) -> datetime:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=8) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as s:
            cert = s.getpeercert()
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)


async def https_works(host: str, cfg) -> bool:
    """One retry before concluding 'no HTTPS' - same transient-failure rule as every other check
    (neverssl.com drops the first HTTPS connection and answers the second)."""
    async with httpx.AsyncClient(timeout=cfg.timeout_s, headers={"User-Agent": cfg.user_agent}) as c:
        for attempt in range(2):
            try:
                await c.head(f"https://{host}/")
                return True
            except httpx.HTTPError:
                if attempt == 0:
                    await asyncio.sleep(1)
    return False


@check("tls")
async def tls(site, cfg):
    u = urlparse(site.start_url)
    prof = getattr(site, "profile", None)
    if prof and prof.unreachable:
        return []                                   # cascade: no host -> no TLS to judge (site_down says it all)
    if _is_local(u.netloc):
        return []                                   # local dev server: TLS not expected
    if u.scheme != "https":
        # the input said http:// - does https exist but simply not get used, or not exist at all?
        # "works" = an HTTPS request gets an HTTP response (a handshake alone isn't enough: some
        # servers complete TLS and then reset the connection)
        if await https_works(u.hostname, cfg):
            return [Finding("tls", "no_https_redirect", "medium", site.start_url,
                            "HTTPS works but the site is served over plain HTTP (no redirect to https)")]
        return [Finding("tls", "no_https", "high", site.start_url, "Site has no working HTTPS")]
    out = []
    try:
        expiry = await asyncio.to_thread(_cert_expiry, u.hostname, u.port or 443)
        days = (expiry - datetime.now(timezone.utc)).days
        if days < 0:
            out.append(Finding("tls", "cert_expired", "critical", site.start_url, f"TLS certificate expired {-days} days ago"))
        elif days < 7:
            out.append(Finding("tls", "cert_expiring", "high", site.start_url, f"TLS certificate expires in {days} days"))
        elif days < cfg.cert_warn_days:
            out.append(Finding("tls", "cert_expiring", "medium", site.start_url, f"TLS certificate expires in {days} days"))
    except ssl.SSLCertVerificationError as e:
        msg = str(e)
        if "expired" in msg:
            out.append(Finding("tls", "cert_expired", "critical", site.start_url, "TLS certificate has expired", msg[:200]))
        elif "self-signed" in msg or "self signed" in msg:
            out.append(Finding("tls", "cert_self_signed", "critical", site.start_url,
                               "TLS certificate is self-signed (browsers show a warning)", msg[:200]))
        elif "hostname" in msg or "match" in msg:
            out.append(Finding("tls", "cert_hostname_mismatch", "critical", site.start_url,
                               "TLS certificate is for a different hostname", msg[:200]))
        else:
            out.append(Finding("tls", "cert_invalid", "critical", site.start_url, "TLS certificate failed verification", msg[:200]))
    except OSError as e:
        out.append(Finding("tls", "tls_error", "high", site.start_url, "TLS handshake failed", str(e)[:200]))

    # plain http should redirect to https
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_s, follow_redirects=False,
                                     headers={"User-Agent": cfg.user_agent}) as c:
            r = await c.head(f"http://{u.netloc}/")
        loc = r.headers.get("location", "")
        if not (r.is_redirect and loc.startswith("https://")):
            out.append(Finding("tls", "no_https_redirect", "medium", f"http://{u.netloc}/",
                               "HTTP does not redirect to HTTPS", f"status {r.status_code} location={loc!r}"))
    except httpx.HTTPError:
        pass                                         # port 80 closed is acceptable
    return out
