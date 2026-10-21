"""Configuration: sane defaults, optionally overridden by a YAML file and CLI flags."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

import yaml


@dataclass
class LLMConfig:
    enabled: bool = True
    model: str = "haiku"          # cheapest model is plenty for triage
    timeout_s: int = 120
    max_findings: int = 60        # cap what we send -> caps cost
    cache_hours: int = 24         # identical findings -> reuse last triage, $0


@dataclass
class Config:
    # crawl budget (guardrails against hammering a site)
    max_pages: int = 40
    max_depth: int = 3
    max_requests: int = 400
    time_budget_s: int = 90
    concurrency: int = 8
    timeout_s: float = 10.0
    user_agent: str = "AIntropyHealthChecker/1.0 (read-only; +https://github.com)"
    respect_robots: bool = True
    check_external: bool = True
    max_external: int = 60
    ignore_patterns: list = field(default_factory=lambda: ["/cdn-cgi/"])

    # thresholds
    slow_ms: int = 1500
    very_slow_ms: int = 4000
    heavy_bytes: int = 1_500_000
    compress_min_bytes: int = 20_000
    cert_warn_days: int = 30
    redirect_hops_warn: int = 3

    # checks / plugins
    disabled_checks: list = field(default_factory=list)
    plugins_dir: str | None = None

    # outputs & hooks
    reports_dir: str = "reports"
    notify: str = "on_change"        # always | on_change | never
    webhook_url: str | None = None
    webhook_secret: str | None = None
    on_complete_command: str | None = None

    llm: LLMConfig = field(default_factory=LLMConfig)


def load_config(path: str | None = None, site: str | None = None, overrides: dict | None = None) -> Config:
    """Merge: defaults <- yaml `defaults` <- yaml `sites[site]` <- env <- CLI overrides."""
    cfg = Config()
    data = {}
    if path:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    _apply(cfg, data.get("defaults", {}))
    if site:
        for s in data.get("sites", []):
            if s.get("name") == site or s.get("url") == site:
                _apply(cfg, {k: v for k, v in s.items() if k not in ("name", "url")})
    # secrets only ever come from the environment, never from yaml in the repo
    cfg.webhook_url = os.environ.get("HEALTH_WEBHOOK_URL", cfg.webhook_url)
    cfg.webhook_secret = os.environ.get("HEALTH_WEBHOOK_SECRET", cfg.webhook_secret)
    _apply(cfg, overrides or {})
    return cfg


def site_urls(path: str | None) -> list[dict]:
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("sites", [])


def _apply(cfg: Config, values: dict) -> None:
    names = {f.name for f in fields(cfg)}
    for k, v in values.items():
        if v is None or k not in names:
            continue
        if k == "llm" and isinstance(v, dict):
            for lk, lv in v.items():
                if hasattr(cfg.llm, lk):
                    setattr(cfg.llm, lk, lv)
        else:
            setattr(cfg, k, v)
