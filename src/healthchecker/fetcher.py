"""Async HTTP fetcher with guardrails and proper handling

* read-only: only GET / HEAD are possible, forms are never submitted
* global request + wall-clock budget
* adaptive concurrency for the target site: halves on 429 / Retry-After, slowly grows back
* robots.txt Crawl-delay honoured; small per-host limit for third-party hosts
* redirects followed manually so the chain is recorded
* one retry on 5xx / network error to suppress flaky false alarms
* TLS failures are recorded; the caller may opt a host into an insecure client so a site
  with a broken certificate can still be assessed (reported as critical, never hidden)
"""
from __future__ import annotations

import asyncio
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx

from .models import Resource

ALLOWED_METHODS = ("GET", "HEAD")
MAX_REDIRECTS = 10
BODY_CAP = 5_000_000        # bytes kept in memory for parsing
READ_CAP = 20_000_000       # stop reading after this (still counted as heavy)
EXTERNAL_PER_HOST = 2       # be gentle with hosts we were not asked to test
MAX_RETRY_AFTER_S = 30
TLS_MARKERS = ("CERTIFICATE_VERIFY_FAILED", "certificate verify failed", "SSLCertVerificationError",
               "hostname mismatch", "self-signed", "self signed", "certificate has expired")


class BudgetExceeded(Exception):
    pass


class AdaptiveLimiter:
    """A semaphore whose size can shrink when the server pushes back and grow when it's happy."""

    def __init__(self, limit: int):
        self.max = self.limit = max(1, limit)
        self.active = 0
        self.ok_streak = 0
        self.cond = asyncio.Condition()

    async def __aenter__(self):
        async with self.cond:
            await self.cond.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def __aexit__(self, *exc):
        async with self.cond:
            self.active -= 1
            self.cond.notify_all()

    def shrink(self):
        self.limit = max(1, self.limit // 2)
        self.ok_streak = 0

    def success(self):
        self.ok_streak += 1
        if self.ok_streak >= 25 and self.limit < self.max:
            self.limit += 1
            self.ok_streak = 0


def _retry_after_s(value: str | None) -> float:
    if not value:
        return 5.0
    try:
        return min(MAX_RETRY_AFTER_S, max(0.0, float(value)))
    except ValueError:
        try:
            return min(MAX_RETRY_AFTER_S, max(0.0, parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError):
            return 5.0


class Fetcher:
    def __init__(self, cfg):
        self.cfg = cfg
        headers = {"User-Agent": cfg.user_agent,                       # honest UA - we never impersonate a browser
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "en;q=0.9,*;q=0.5"}
        common = dict(timeout=cfg.timeout_s, headers=headers, follow_redirects=False,
                      limits=httpx.Limits(max_connections=cfg.concurrency * 4))
        self.client = httpx.AsyncClient(**common)
        self._insecure_client = None
        self._common = common
        self.site_hosts: set[str] = set()       # set by the profiler: these share the adaptive limiter
        self.insecure_hosts: set[str] = set()   # hosts with a broken cert we still want to assess
        self.site_limiter = AdaptiveLimiter(cfg.concurrency)
        self.host_limiters: dict[str, AdaptiveLimiter] = {}
        self.pause_until: dict[str, float] = {}
        self.crawl_delay = 0.0
        self._delay_lock = asyncio.Lock()
        self._last_start = 0.0
        self.requests = 0
        self.rate_limited = 0
        self.budget_hit = False
        self.deadline = time.monotonic() + cfg.time_budget_s
        self.reserve = 0                        # small extra allowance for confirmation re-tests after the budget
        self.log: list[tuple[str, str, int | None]] = []

    async def close(self):
        await self.client.aclose()
        if self._insecure_client:
            await self._insecure_client.aclose()

    def set_crawl_delay(self, seconds: float):
        self.crawl_delay = max(0.0, min(seconds, 10.0))        # cap: a 60s delay would make checks useless
        if self.crawl_delay:
            self.site_limiter = AdaptiveLimiter(1)

    def _client_for(self, host: str) -> httpx.AsyncClient:
        if host in self.insecure_hosts:
            if self._insecure_client is None:
                self._insecure_client = httpx.AsyncClient(verify=False, **self._common)
            return self._insecure_client
        return self.client

    def _limiter_for(self, host: str) -> AdaptiveLimiter:
        if host in self.site_hosts or not self.site_hosts:
            return self.site_limiter
        if host not in self.host_limiters:
            self.host_limiters[host] = AdaptiveLimiter(EXTERNAL_PER_HOST)
        return self.host_limiters[host]

    def _check_budget(self):
        if self.reserve > 0:
            self.reserve -= 1
            return
        if self.requests >= self.cfg.max_requests or time.monotonic() > self.deadline:
            self.budget_hit = True
            raise BudgetExceeded()

    async def _politeness(self, host: str):
        wait = self.pause_until.get(host, 0) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        if self.crawl_delay and host in self.site_hosts:
            async with self._delay_lock:
                gap = self._last_start + self.crawl_delay - time.monotonic()
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_start = time.monotonic()

    async def fetch(self, url: str, method: str = "GET", keep_body: bool = True, retries: int = 1) -> Resource:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"guardrail: method {method} not allowed (read-only checker)")
        host = urlparse(url).netloc
        limiter = self._limiter_for(host)
        attempt, throttled = 0, False
        while True:
            # Take a concurrency slot FIRST, then start the clock: the hard cap (so a slow-drip
            # server can't stall the run) must only cover network time, never time spent queued.
            async with limiter:
                await self._politeness(host)
                try:
                    res = await asyncio.wait_for(self._fetch_once(url, method, keep_body), self.cfg.timeout_s * 2)
                except asyncio.TimeoutError:
                    res = Resource(url=url, final_url=url, error=f"timeout after {self.cfg.timeout_s * 2:.0f}s")
                    self.log.append((method, url, None))
            # server pushback: honour Retry-After, slow down, try once more
            if res.status == 429 or (res.status == 503 and "retry-after" in res.headers):
                res.rate_limited = True
                self.rate_limited += 1
                limiter.shrink()
                self.pause_until[host] = time.monotonic() + _retry_after_s(res.headers.get("retry-after"))
                if not throttled and not res.skipped:
                    throttled = True
                    continue
                return res
            transient = (res.error and not res.skipped and not res.tls_error) or (res.status or 0) >= 500
            if transient and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.5)
                continue
            if res.ok:
                limiter.success()
            res.attempts = attempt + 1
            return res

    async def check(self, url: str) -> Resource:
        """Cheap existence check: HEAD, falling back to GET for servers that reject or mishandle HEAD."""
        res = await self.fetch(url, "HEAD")
        if res.skipped or res.rate_limited:
            return res
        if res.error or res.status in (400, 403, 404, 405, 501) or (res.status or 0) >= 500:
            res = await self.fetch(url, "GET", keep_body=False)
        return res

    async def _fetch_once(self, url: str, method: str, keep_body: bool) -> Resource:
        res = Resource(url=url)
        current = url
        net_ms = 0.0        # time on the wire only (caller already holds a concurrency slot)
        hop_t0 = time.perf_counter()
        try:
            for _ in range(MAX_REDIRECTS):
                self._check_budget()
                self.requests += 1
                hop_t0 = time.perf_counter()
                client = self._client_for(urlparse(current).netloc)
                async with client.stream(method, current) as resp:
                    self.log.append((method, current, resp.status_code))
                    if resp.is_redirect and "location" in resp.headers:
                        res.redirects.append(current)
                        current = urljoin(current, resp.headers["location"])
                        net_ms += (time.perf_counter() - hop_t0) * 1000
                        continue
                    res.ttfb_ms = net_ms + (time.perf_counter() - hop_t0) * 1000
                    res.status = resp.status_code
                    res.final_url = str(resp.url)
                    res.headers = {k.lower(): v for k, v in resp.headers.items()}
                    res.content_type = res.headers.get("content-type", "").lower()
                    textual = any(t in res.content_type for t in ("html", "xml", "text", "javascript", "ecmascript", "json"))
                    gz = "gzip" in res.content_type or urlparse(current).path.endswith(".gz")   # sitemaps
                    if method == "GET" and keep_body and (textual or gz or not res.content_type):
                        buf = bytearray()
                        size = 0
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if len(buf) < BODY_CAP:
                                buf.extend(chunk[: BODY_CAP - len(buf)])
                            if size > READ_CAP:
                                break
                        res.body, res.size = bytes(buf), size
                    else:
                        res.size = int(res.headers.get("content-length", 0) or 0)
                    res.total_ms = net_ms + (time.perf_counter() - hop_t0) * 1000
                    return res
            res.error = f"too many redirects (>{MAX_REDIRECTS})"
        except BudgetExceeded:
            res.error, res.skipped = "skipped: crawl budget exhausted", True
        except httpx.HTTPError as e:
            res.error = f"{type(e).__name__}: {e}"[:300]
            if any(m in str(e) for m in TLS_MARKERS):
                res.tls_error = res.error
            self.log.append((method, current, None))
        except (ValueError, UnicodeError) as e:      # malformed URLs / headers from the wild
            res.error = f"invalid request: {e}"[:300]
        res.final_url = res.final_url or current
        res.total_ms = net_ms + (time.perf_counter() - hop_t0) * 1000
        return res
