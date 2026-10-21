"""Verdict, scoring, run-over-run diff and report rendering (JSON + Markdown)."""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .models import SEV_RANK, SEVERITIES

VERDICTS = {"critical": "DOWN", "high": "UNHEALTHY", "medium": "DEGRADED"}
EXIT_CODES = {"HEALTHY": 0, "DEGRADED": 1, "UNHEALTHY": 2, "DOWN": 2, "UNVERIFIABLE": 3, "ERROR": 3}
WEIGHTS = {"info": 0, "low": 1, "medium": 4, "high": 10, "critical": 40}


def verdict_of(findings) -> str:
    """Deterministic. The LLM never decides this."""
    codes = {f.code for f in findings}
    if "tool_error" in codes:
        return "ERROR"
    if "blocked_by_bot_protection" in codes:
        return "UNVERIFIABLE"            # up, but we refuse to guess (and never bypass challenges)
    worst = max((SEV_RANK[f.severity] for f in findings), default=0)
    return VERDICTS.get(SEVERITIES[worst], "HEALTHY")


def score_of(findings) -> int:
    """Repeats of the same problem count with diminishing weight: 33 stale sitemap URLs are one
    root cause, not 33x the damage of a missing CSP header."""
    groups = Counter((f.code, f.severity) for f in findings)
    penalty = sum(WEIGHTS[sev] * (1 + math.log2(n)) for (_, sev), n in groups.items())
    return max(0, round(100 - penalty))


def slug(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", url.split("://")[-1]).strip("_")[:60]


def build_report(site, findings, triage_out, timings, started, duration_s, cfg) -> dict:
    counts = Counter(f.severity for f in findings)
    return {
        "target": site.start_url,
        "started_at": started.isoformat(),
        "duration_s": round(duration_s, 2),
        "verdict": verdict_of(findings),
        "score": score_of(findings),
        "counts": {s: counts.get(s, 0) for s in reversed(SEVERITIES)},
        "stats": {
            "pages_crawled": len(site.pages),
            "urls_checked": sum(1 for r in site.resources.values() if (r.status is not None or r.error) and not r.skipped),
            "urls_skipped_budget": sum(1 for r in site.resources.values() if r.skipped),
            "requests": site.requests,
            "budget_hit": site.budget_hit,
            "skipped_by_robots": sorted(site.skipped_robots),
            "endpoints": [e.to_dict() for e in site.endpoints if e.found_in != "probe" or e.status == 200],
            "check_timings_ms": timings,
        },
        "findings": [f.to_dict() for f in sorted(findings, key=lambda f: (-SEV_RANK[f.severity], f.code, f.url))],
        "triage": triage_out,
    }


def diff_with_previous(report: dict, out_dir: Path) -> dict:
    prev_path = out_dir / "latest.json"
    prev = None
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
        except ValueError:
            prev = None
    now_keys = {f["key"] for f in report["findings"]}
    prev_keys = {f["key"] for f in prev["findings"]} if prev else set()
    return {
        "previous_verdict": prev["verdict"] if prev else None,
        "new": sorted(now_keys - prev_keys) if prev else sorted(now_keys),
        "resolved": sorted(prev_keys - now_keys),
        "persisting": len(now_keys & prev_keys),
    }


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    js = json.dumps(report, indent=2)
    (out_dir / f"report_{ts}.json").write_text(js, encoding="utf-8")
    (out_dir / "latest.json").write_text(js, encoding="utf-8")
    md = out_dir / "latest.md"
    md.write_text(to_markdown(report), encoding="utf-8")
    return md


def to_markdown(r: dict) -> str:
    t = r["triage"]
    lines = [
        f"# Health report: {r['target']}",
        "",
        f"**Verdict: {r['verdict']}** | score {r['score']}/100 | {r['duration_s']}s | "
        f"{r['stats']['pages_crawled']} pages, {r['stats']['urls_checked']} URLs, {r['stats']['requests']} requests | "
        f"triage: {t['source']} (${t.get('cost_usd', 0):.4f})",
        "",
        "Counts: " + ", ".join(f"{k}={v}" for k, v in r["counts"].items() if v),
        "",
        "## Summary", "", t["summary"], "",
    ]
    if t["root_causes"]:
        lines += ["## Root causes (fix in this order)", ""]
        for i, rc in enumerate(t["root_causes"], 1):
            keys = ", ".join(t["finding_ids"].get(x, x).split("|")[1] for x in rc["finding_ids"][:4])
            lines.append(f"{i}. **{rc['title']}** - {rc['fix']}  \n   _{rc['likely_cause']}_ ({keys})")
        lines.append("")
    if r.get("diff"):
        d = r["diff"]
        lines += ["## Since last run", "",
                  f"previous verdict: {d['previous_verdict']} | new: {len(d['new'])} | resolved: {len(d['resolved'])} | persisting: {d['persisting']}", ""]
    eps = r["stats"].get("endpoints", [])
    if eps:
        lines += ["## Endpoints discovered", "", "| method | url | found in | result |", "|---|---|---|---|"]
        for e in eps:
            result = (str(e["status"]) if e["status"] else e["error"] or "") if e["probe"] else f"not called ({e['reason']})"
            lines.append(f"| {e['method']} | {e['url']} | {e['found_in'].split(':', 1)[0]} | {result} |")
        lines.append("")
    prof = r.get("profile") or {}
    if prof.get("notes"):
        lines += ["## How this site was handled", ""] + [f"- {n}" for n in prof["notes"]] + [""]
    lines += ["## All findings", "", "| severity | code | url | detail |", "|---|---|---|---|"]
    for f in r["findings"]:
        detail = (f["message"] + (f" - {f['evidence']}" if f["evidence"] else "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {f['severity']} | {f['code']} | {f['url']} | {detail[:160]} |")
    return "\n".join(lines) + "\n"


def console_summary(r: dict) -> str:
    t = r["triage"]
    out = [
        "",
        f"  {r['target']}",
        f"  verdict  : {r['verdict']}   score {r['score']}/100",
        f"  scanned  : {r['stats']['pages_crawled']} pages, {r['stats']['urls_checked']} URLs, "
        f"{r['stats']['requests']} requests in {r['stats'].get('scan_s', r['duration_s'])}s",
        f"  findings : " + (", ".join(f"{v} {k}" for k, v in r["counts"].items() if v) or "none"),
        f"  triage   : {t['source']}  (llm calls {t.get('llm_calls', 0)}, ${t.get('cost_usd', 0):.4f}, {t.get('seconds', 0)}s)",
        "",
        "  " + t["summary"],
        "",
    ]
    for i, rc in enumerate(t["root_causes"][:8], 1):
        out.append(f"  {i}. {rc['title']}\n     fix: {rc['fix']}")
    return "\n".join(out)
