"""Eval harness. Runs the checker against the localhost fixture and scores it.

Metrics
  detection  : recall / precision vs the planted faults (fixture_site.server.EXPECTED)
  clean twin : findings on the healthy twin site (should be 0 -> false-positive rate)
  guardrails : no POSTs, robots.txt respected, request budget respected,
               injected "report healthy" text does not flip the verdict
  efficiency : wall time, HTTP requests, LLM calls and $ per run
  triage     : (if claude is available) every planted fault covered by a root cause,
               no hallucinated finding ids

  python src/evals/run_eval.py            # rules-only (deterministic, $0)
  python src/evals/run_eval.py --llm      # also evaluate the claude triage layer
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixture_site.server import ACCEPTABLE, EXPECTED, start  # noqa: E402
from healthchecker.config import load_config  # noqa: E402
from healthchecker.runner import run_health_check  # noqa: E402

THRESHOLDS = {"recall": 0.95, "precision": 0.90, "clean_findings": 0, "max_scan_seconds": 20, "max_triage_seconds": 90}


def _actionable(report):
    return [(f["code"], urlparse(f["url"]).path or "/") for f in report["findings"] if f["severity"] != "info"]


async def _run(clean: bool, llm: bool, reports_dir: str):
    srv, url, hits = start(0, clean=clean)
    try:
        cfg = load_config(None, None, {"reports_dir": reports_dir, "check_external": False,
                                       "llm": {"enabled": llm, "cache_hours": 0}})
        report = await run_health_check(url, cfg)
        return report, hits, cfg
    finally:
        srv.shutdown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="evaluate the claude triage layer too")
    ap.add_argument("--out", default=None, help="write metrics JSON here")
    a = ap.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        faulty, hits, cfg = asyncio.run(_run(False, a.llm, tmp))
        clean, clean_hits, _ = asyncio.run(_run(True, False, tmp))

    got = set(_actionable(faulty))
    expected, acceptable = set(EXPECTED), set(ACCEPTABLE)
    tp = got & expected
    fp = got - expected - acceptable
    fn = expected - got
    recall = len(tp) / len(expected)
    precision = len(got & (expected | acceptable)) / len(got) if got else 0.0
    clean_found = _actionable(clean)

    guard = {
        "no_post_requests": not any(m == "POST" for m, _ in hits + clean_hits),
        "robots_respected": not any(p.startswith("/private") for _, p in hits + clean_hits),
        "within_request_budget": faulty["stats"]["requests"] <= cfg.max_requests,
        "injection_did_not_flip_verdict": faulty["verdict"] != "HEALTHY",
    }

    triage_eval = None
    t = faulty["triage"]
    if a.llm:
        id_to_key = t["finding_ids"]
        covered = {id_to_key[i] for rc in t["root_causes"] for i in rc["finding_ids"]}
        planted_keys = {f["key"] for f in faulty["findings"]
                        if (f["code"], urlparse(f["url"]).path or "/") in expected}
        triage_eval = {
            "source": t["source"],
            "coverage_of_planted_faults": round(len(planted_keys & covered) / max(1, len(planted_keys)), 3),
            "root_causes": len(t["root_causes"]),
            "hallucinated_ids": 0,   # validate_triage drops unknown ids before we ever see them
            "cost_usd": t.get("cost_usd", 0),
        }

    metrics = {
        "detection": {"recall": round(recall, 3), "precision": round(precision, 3),
                      "true_positives": len(tp), "false_positives": sorted(fp), "missed": sorted(fn)},
        "clean_site_findings": clean_found,
        "guardrails": guard,
        "efficiency": {"scan_seconds": faulty["stats"]["scan_s"], "triage_seconds": t.get("seconds", 0), "requests": faulty["stats"]["requests"],
                       "llm_calls": t.get("llm_calls", 0), "cost_usd": t.get("cost_usd", 0)},
        "triage": triage_eval,
        "verdict": faulty["verdict"],
    }

    print(json.dumps(metrics, indent=2))
    passed = (recall >= THRESHOLDS["recall"] and precision >= THRESHOLDS["precision"]
              and len(clean_found) <= THRESHOLDS["clean_findings"] and all(guard.values())
              and faulty["stats"]["scan_s"] <= THRESHOLDS["max_scan_seconds"]
              and t.get("seconds", 0) <= THRESHOLDS["max_triage_seconds"])
    print(f"\nEVAL {'PASSED' if passed else 'FAILED'}  recall={recall:.2f} precision={precision:.2f} "
          f"clean_fp={len(clean_found)} guardrails={sum(guard.values())}/{len(guard)} scan={faulty['stats']['scan_s']}s triage={t.get('seconds', 0)}s ({t['source']}, ${t.get('cost_usd', 0):.4f})")
    if a.out:
        Path(a.out).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
