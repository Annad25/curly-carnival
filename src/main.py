"""Website Health Checker - CLI entry point.

  python src/main.py check --url https://aintropy.ai/        # check any site
  python src/main.py check --config sites.yaml               # check every site in a config
  python src/main.py demo                                    # local fixture site with planted faults
  python src/main.py serve --port 8080                       # HTTP API for other systems

Exit codes: 0 healthy, 1 degraded, 2 unhealthy/down, 3 tool error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from healthchecker.config import load_config, site_urls  # noqa: E402
from healthchecker.report import EXIT_CODES, console_summary  # noqa: E402
from healthchecker.runner import run_health_check  # noqa: E402


def _overrides(a) -> dict:
    o = {"max_pages": a.max_pages, "max_requests": a.max_requests, "reports_dir": a.reports_dir, "webhook_url": a.webhook,
         "notify": a.notify, "plugins_dir": a.plugins, "check_external": False if a.no_external else None}
    if a.no_llm:
        o["llm"] = {"enabled": False}
    return o


def _print(report: dict, as_json: bool):
    if as_json:
        print(json.dumps({k: v for k, v in report.items() if k != "request_log"}, indent=2))
    else:
        print(console_summary(report))
        print(f"\n  report: {report.get('report_path')}   hooks: {', '.join(report.get('hooks', []))}")


def cmd_check(a) -> int:
    targets = [a.url] if a.url else [s["url"] for s in site_urls(a.config) if s.get("enabled", True)]
    if not targets:
        print("give --url or a --config with sites", file=sys.stderr)
        return 3
    worst = 0
    for url in targets:
        cfg = load_config(a.config, url, _overrides(a))
        report = asyncio.run(run_health_check(url, cfg))
        _print(report, a.json)
        worst = max(worst, EXIT_CODES[report["verdict"]])
    return 0 if a.exit_zero else worst


def cmd_demo(a) -> int:
    from fixture_site.server import start
    srv, url, hits = start(0, clean=a.clean)
    print(f"started local fixture website ({'clean' if a.clean else 'with planted faults'}) at {url}")
    try:
        cfg = load_config(a.config, None, _overrides(a))  # fixture "external" links point at localhost
        report = asyncio.run(run_health_check(url, cfg))
        _print(report, a.json)
        posts = [h for h in hits if h[0] == "POST"]
        private = [h for h in hits if h[1].startswith("/private")]
        print(f"\n  guardrails: POST requests={len(posts)}  robots-disallowed hits={len(private)}  "
              f"total requests={report['stats']['requests']}/{cfg.max_requests}")
    finally:
        srv.shutdown()
    return 0 if a.exit_zero else EXIT_CODES[report["verdict"]]


def cmd_serve(a) -> int:
    from healthchecker.api import serve
    serve(a.host, a.port, a.config)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="healthcheck", description="Fast, cheap website health checker agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--config", default=None, help="YAML config (see sites.yaml)")
        p.add_argument("--max-pages", type=int, default=None)
        p.add_argument("--max-requests", type=int, default=None, help="hard cap on HTTP requests (default 400)")
        p.add_argument("--reports-dir", default=None)
        p.add_argument("--webhook", default=None, help="POST results here (or env HEALTH_WEBHOOK_URL)")
        p.add_argument("--notify", choices=["always", "on_change", "never"], default=None)
        p.add_argument("--plugins", default=None, help="directory with extra check plugins")
        p.add_argument("--no-llm", action="store_true", help="skip claude triage (rules only)")
        p.add_argument("--no-external", action="store_true", help="don't check off-site links")
        p.add_argument("--json", action="store_true", help="print full JSON report to stdout")
        p.add_argument("--exit-zero", action="store_true", help="always exit 0 (for demos)")

    c = sub.add_parser("check", help="check a website")
    c.add_argument("--url", default=None)
    common(c)
    c.set_defaults(fn=cmd_check)

    d = sub.add_parser("demo", help="run against the bundled localhost fixture site")
    d.add_argument("--clean", action="store_true", help="use the healthy fixture (false-positive test)")
    common(d)
    d.set_defaults(fn=cmd_demo)

    s = sub.add_parser("serve", help="HTTP API: POST /check")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    s.add_argument("--config", default="sites.yaml")
    s.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return 3


if __name__ == "__main__":
    sys.exit(main())
