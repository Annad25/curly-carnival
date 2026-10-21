"""Checker agents. Each check is `async def fn(site, cfg) -> list[Finding]`.

Add a check by decorating a function with @check("name") in any module in this
package, or in a file inside `plugins_dir` (see examples/plugins/). That is the
plug-in hook for org-specific rules (e.g. "pricing page must show a price").
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pkgutil
import time
from pathlib import Path

from ..models import Finding

REGISTRY: dict[str, callable] = {}


def assessable(site) -> bool:
    """False when the site is unreachable or challenged by a WAF: page-level checks would only
    produce noise about the error/challenge page, not facts about the site."""
    prof = getattr(site, "profile", None)
    return not (prof and (prof.unreachable or prof.blocked))


def check(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


def load_builtin():
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")


def load_plugins(directory: str | None):
    if not directory:
        return
    for path in sorted(Path(directory).glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"hc_plugin_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)


async def run_checks(site, cfg) -> tuple[list[Finding], dict]:
    load_builtin()
    load_plugins(cfg.plugins_dir)
    names = [n for n in REGISTRY if n not in cfg.disabled_checks]
    timings: dict[str, float] = {}

    async def run_one(name):
        t0 = time.perf_counter()
        try:
            out = await REGISTRY[name](site, cfg)
        except Exception as e:  # one broken checker must not kill the run
            out = [Finding(name, "checker_error", "info", site.start_url, f"check '{name}' crashed", repr(e)[:200])]
        timings[name] = round((time.perf_counter() - t0) * 1000, 1)
        return out

    results = await asyncio.gather(*(run_one(n) for n in names))
    findings, seen = [], set()
    for batch in results:
        for f in batch:
            if f.key not in seen:          # dedupe identical findings across checkers
                seen.add(f.key)
                findings.append(f)
    return findings, timings
