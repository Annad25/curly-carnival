"""Outbound hooks: generic signed webhook (Slack/Teams/PagerDuty/any HTTP endpoint)
and an optional local command (create a ticket, push metrics, ...).

Destinations come only from config/env set by the operator - never from page content.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import subprocess

import httpx

from .models import SEV_RANK


def should_notify(report: dict, mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    d = report.get("diff", {})
    sev = {f["key"]: f["severity"] for f in report["findings"]}
    new_serious = [k for k in d.get("new", []) if SEV_RANK[sev.get(k, "info")] >= SEV_RANK["medium"]]
    return d.get("previous_verdict") != report["verdict"] or bool(new_serious)


def payload(report: dict, report_path: str) -> dict:
    d = report.get("diff", {})
    text = (f"[{report['verdict']}] {report['target']} score {report['score']}/100 - "
            f"{report['triage']['summary']} ({len(d.get('new', []))} new, {len(d.get('resolved', []))} resolved)")
    return {
        "event": "website_health.completed",
        "text": text,                         # Slack / Teams / Discord render this field
        "target": report["target"],
        "verdict": report["verdict"],
        "previous_verdict": d.get("previous_verdict"),
        "score": report["score"],
        "counts": report["counts"],
        "new_findings": d.get("new", [])[:25],
        "resolved_findings": d.get("resolved", [])[:25],
        "root_causes": [{"title": c["title"], "fix": c["fix"]} for c in report["triage"]["root_causes"][:5]],
        "report_path": report_path,
    }


def send_webhook(url: str, body: dict, secret: str | None) -> str:
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-HealthCheck-Signature"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    try:
        r = httpx.post(url, content=raw, headers=headers, timeout=10)
        return f"webhook {r.status_code}"
    except httpx.HTTPError as e:
        return f"webhook failed: {type(e).__name__}"


def run_command(cmd: str, report_path: str, report: dict) -> str:
    env = {**os.environ, "HEALTH_REPORT_PATH": report_path, "HEALTH_VERDICT": report["verdict"],
           "HEALTH_TARGET": report["target"]}
    try:
        p = subprocess.run(shlex.split(cmd), env=env, timeout=60, capture_output=True, text=True)  # no shell
        return f"command exit {p.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"command failed: {e}"


def dispatch(report: dict, report_path: str, cfg) -> list[str]:
    if not should_notify(report, cfg.notify):
        return ["notify: skipped (no change)"]
    log = []
    if cfg.webhook_url:
        log.append(send_webhook(cfg.webhook_url, payload(report, report_path), cfg.webhook_secret))
    if cfg.on_complete_command:
        log.append(run_command(cfg.on_complete_command, report_path, report))
    return log or ["notify: no hooks configured"]
