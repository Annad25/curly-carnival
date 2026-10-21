"""Orchestrator: planner -> checker agents (parallel) -> triage (reasoning) -> report -> hooks."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from .checks import run_checks
from .crawler import crawl
from .fetcher import Fetcher
from .hooks import dispatch
from .report import build_report, diff_with_previous, slug, write_report
from .triage import triage


async def run_health_check(url: str, cfg, write: bool = True) -> dict:
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    fetcher = Fetcher(cfg)
    try:
        site = await crawl(url, cfg, fetcher)
        findings, timings = await run_checks(site, cfg)
    finally:
        await fetcher.close()
    scan_s = time.perf_counter() - t0
    out_dir = Path(cfg.reports_dir) / slug(site.start_url)
    t1 = time.perf_counter()
    triage_out = triage(findings, site.start_url, cfg, out_dir)
    triage_out["seconds"] = round(time.perf_counter() - t1, 2)
    report = build_report(site, findings, triage_out, timings, started, time.perf_counter() - t0, cfg)
    report["stats"]["scan_s"] = round(scan_s, 2)
    report["request_log"] = [{"method": m, "url": u, "status": s} for m, u, s in fetcher.log]
    if write:
        report["diff"] = diff_with_previous(report, out_dir)
        md = write_report(report, out_dir)
        report["report_path"] = str(md)
        report["hooks"] = dispatch(report, str(md), cfg)
    return report
