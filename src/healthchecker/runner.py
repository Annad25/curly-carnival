"""Orchestrator: profile+crawl -> checker agents (parallel) -> triage (reasoning) -> report -> hooks.

Failure policy: a bad *input* raises ValueError (the caller's mistake, reported as such);
anything that goes wrong *inside* the run becomes a report with verdict ERROR - a monitoring
tool must never die with a traceback and leave the on-call person with nothing.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .checks import run_checks
from .crawler import Site, crawl
from .fetcher import Fetcher
from .hooks import dispatch
from .models import Finding
from .profile import normalize_input
from .report import build_report, diff_with_previous, slug, write_report
from .triage import triage


async def run_health_check(url: str, cfg, write: bool = True) -> dict:
    start_url = normalize_input(url)                 # ValueError -> caller: "that's not a URL"
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    fetcher = Fetcher(cfg)
    try:
        site = await crawl(start_url, cfg, fetcher)
        findings, timings = await run_checks(site, cfg)
    except Exception as e:                           # noqa: BLE001 - deliberate safety net
        u = urlparse(start_url)
        site = Site(start_url=start_url, origin=f"{u.scheme}://{u.netloc}", hosts={u.netloc})
        site.requests = fetcher.requests
        findings = [Finding("runner", "tool_error", "info", start_url, f"Health check crashed: {type(e).__name__}: {e}",
                            traceback.format_exc(limit=3)[-600:])]
        timings = {}
    finally:
        await fetcher.close()
    scan_s = time.perf_counter() - t0
    out_dir = Path(cfg.reports_dir) / slug(site.start_url)
    t1 = time.perf_counter()
    triage_out = triage([f for f in findings if f.code != "tool_error"], site.start_url, cfg, out_dir)
    triage_out["seconds"] = round(time.perf_counter() - t1, 2)
    report = build_report(site, findings, triage_out, timings, started, time.perf_counter() - t0, cfg)
    report["stats"]["scan_s"] = round(scan_s, 2)
    prof = getattr(site, "profile", None)
    report["profile"] = {
        "input": url, "start_url": site.start_url, "hosts": sorted(site.hosts), "scope": site.scope,
        "notes": prof.notes if prof else [], "latency_ms": round(prof.latency_ms) if prof else None,
    }
    report["request_log"] = [{"method": m, "url": u, "status": s} for m, u, s in fetcher.log]
    if write:
        report["diff"] = diff_with_previous(report, out_dir)
        md = write_report(report, out_dir)
        report["report_path"] = str(md)
        report["hooks"] = dispatch(report, str(md), cfg)
    return report
