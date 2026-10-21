"""Async HTTP fetcher with guardrails baked in.

* read-only: only GET / HEAD are possible, forms are never submitted
* global request + wall-clock budget, bounded concurrency
* redirects followed manually so the chain is recorded
* one retry on 5xx / network error to suppress flaky false alarms
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urljoin

import httpx

from .models import Resource

ALLOWED_METHODS = ("GET", "HEAD")
MAX_REDIRECTS = 10
BODY_CAP = 5_000_000        # bytes kept in memory for parsing
READ_CAP = 20_000_000       # stop reading after this (still counted as heavy)


class BudgetExceeded(Exception):
    pass


class Fetcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = httpx.AsyncClient(
            timeout=cfg.timeout_s,
            headers={"User-Agent": cfg.user_agent},
            limits=httpx.Limits(max_connections=cfg.concurrency * 2),
            follow_redirects=False,
        )
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.requests = 0
        self.budget_hit = False
        self.deadline = time.monotonic() + cfg.time_budget_s
        self.log: list[tuple[str, str, int | None]] = []

    async def close(self):
        await self.client.aclose()

    def _check_budget(self):
        if self.requests >= self.cfg.max_requests or time.monotonic() > self.deadline:
            self.budget_hit = True
            raise BudgetExceeded()

    async def fetch(self, url: str, method: str = "GET", keep_body: bool = True, retries: int = 1) -> Resource:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"guardrail: method {method} not allowed (read-only checker)")
        attempt = 0
        while True:
            try:
                # hard wall-clock cap per request: a slow-drip server can't stall the run
                res = await asyncio.wait_for(self._fetch_once(url, method, keep_body), self.cfg.timeout_s * 2)
            except asyncio.TimeoutError:
                res = Resource(url=url, final_url=url, error=f"timeout after {self.cfg.timeout_s * 2:.0f}s")
            transient = res.error and not res.skipped or (res.status or 0) >= 500
            if transient and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.5)
                continue
            res.attempts = attempt + 1
            return res

    async def check(self, url: str) -> Resource:
        """Cheap existence check: HEAD, falling back to GET for servers that reject HEAD."""
        res = await self.fetch(url, "HEAD")
        if res.skipped:
            return res
        if res.error or res.status in (403, 405, 501) or (res.status or 0) >= 500:
            res = await self.fetch(url, "GET", keep_body=False)
        return res

    async def _fetch_once(self, url: str, method: str, keep_body: bool) -> Resource:
        res = Resource(url=url)
        current = url
        t0 = time.perf_counter()
        try:
            for _ in range(MAX_REDIRECTS):
                self._check_budget()
                self.requests += 1
                async with self.sem:
                    async with self.client.stream(method, current) as resp:
                        self.log.append((method, current, resp.status_code))
                        if resp.is_redirect and "location" in resp.headers:
                            res.redirects.append(current)
                            current = urljoin(current, resp.headers["location"])
                            continue
                        res.ttfb_ms = (time.perf_counter() - t0) * 1000
                        res.status = resp.status_code
                        res.final_url = str(resp.url)
                        res.headers = {k.lower(): v for k, v in resp.headers.items()}
                        res.content_type = res.headers.get("content-type", "").lower()
                        textual = any(t in res.content_type for t in ("html", "xml", "text", "javascript", "ecmascript", "json"))
                        if method == "GET" and keep_body and textual:
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
                        res.total_ms = (time.perf_counter() - t0) * 1000
                        return res
            res.error = f"too many redirects (>{MAX_REDIRECTS})"
        except BudgetExceeded:
            res.error, res.skipped = "skipped: crawl budget exhausted", True
        except httpx.HTTPError as e:
            res.error = f"{type(e).__name__}: {e}"[:300]
            self.log.append((method, current, None))
        res.final_url = res.final_url or current
        res.total_ms = (time.perf_counter() - t0) * 1000
        return res
