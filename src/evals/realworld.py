"""Real-world generalisation eval: run the checker on a diverse corpus of live websites it was
never tuned for, then INDEPENDENTLY re-verify a sample of each site's findings.

Why: the localhost fixtures prove the rules work; this proves they hold on the open web, where
sites are slow, blocked, huge, multilingual, mis-configured or broken on purpose.

  python src/evals/realworld.py                 # full corpus (~5 min, polite budgets)
  python src/evals/realworld.py --only badssl   # filter by name/category

Metrics per site: finished (no crash), verdict, time, coverage, findings, verified precision,
and expectation checks for sites whose ground truth is public (badssl.com, the-internet, DNS failure).
The verifier uses a different method from the checker on purpose - sequential GETs, generous
timeout, separate client - so it can catch measurement artefacts (the class of bug that produced
false "slow" pages earlier). It never impersonates a browser either.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from healthchecker.config import load_config  # noqa: E402
from healthchecker.htmlparse import parse_page  # noqa: E402
from healthchecker.runner import run_health_check  # noqa: E402

# name, url, category, expectations (all optional): verdict_in, must, must_any, must_not
CORPUS = [
    ("aintropy", "https://aintropy.ai/", "AI startup / Cloudflare", {}),
    ("twelvelabs", "https://www.twelvelabs.io/", "video-AI company / Framer", {}),
    ("synechron", "https://www.synechron.com/", "large enterprise / Next.js + Imperva", {}),
    ("3kglobal", "https://www.3kglobal.ai/", "AI company / Cloudflare (user pick)", {}),
    ("gydeon", "https://www.gydeon.ai/", "startup / Vercel Next.js, www->apex (user pick)", {}),
    ("mizuho", "https://www.mizuhogroup.com/", "global bank / Cloudflare (user pick)", {}),
    ("example", "https://example.com/", "minimal single page", {"must_not": ["site_down"]}),
    ("books", "https://books.toscrape.com/", "catalogue built for crawlers", {"must_not": ["site_down"]}),
    ("badssl-expired", "https://expired.badssl.com/", "known-bad: expired cert", {"must": ["cert_expired"]}),
    ("badssl-selfsigned", "https://self-signed.badssl.com/", "known-bad: self-signed", {"must": ["cert_self_signed"]}),
    ("badssl-wronghost", "https://wrong.host.badssl.com/", "known-bad: hostname mismatch",
     {"must": ["cert_hostname_mismatch"]}),
    ("badssl-http", "http://http.badssl.com/", "known-bad: served over http",
     {"must_any": ["no_https", "no_https_redirect"]}),
    ("neverssl", "http://neverssl.com/", "http-only by design",
     {"must_any": ["no_https", "no_https_redirect"], "must_not": ["site_down"]}),
    ("the-internet", "https://the-internet.herokuapp.com/broken_images", "known-bad: broken images",
     {"must": ["broken_asset"]}),
    ("dns-fail", "https://hc-no-such-domain-7f3a9c.com/", "known-bad: DNS failure",
     {"verdict_in": ["DOWN"], "must": ["site_down"]}),
    ("python-docs", "https://docs.python.org/3/", "docs site, sub-path scope", {}),
    ("react", "https://react.dev/", "JS framework docs (Next.js)", {}),
    ("wikipedia", "https://www.wikipedia.org/", "huge multilingual portal", {}),
    ("gov-uk", "https://www.gov.uk/", "government, very large", {}),
    ("lemonde", "https://www.lemonde.fr/", "non-English news", {}),
    ("allbirds", "https://www.allbirds.com/", "e-commerce (Shopify)", {}),
    ("wordpress-news", "https://wordpress.org/news/", "WordPress blog, sub-path", {}),
    ("g2", "https://www.g2.com/", "bot-protected marketplace", {}),
    ("httpstat-503", "https://httpstat.us/503", "known-bad: homepage 503", {}),
]

STATUS_CODES = {"broken_link", "broken_asset", "sitemap_broken_url", "api_endpoint_missing", "api_endpoint_down",
                "form_target_broken", "server_error", "unreachable", "homepage_error"}
HEADER_CODES = {"missing_hsts": "strict-transport-security", "missing_csp": "content-security-policy",
                "missing_xcto": "x-content-type-options"}
PAGE_CODES = {"missing_title", "missing_meta_description", "missing_h1", "missing_lang", "img_missing_alt"}


async def verify(f: dict, start_url: str, client: httpx.AsyncClient, cfg) -> str:
    """-> 'confirmed' | 'refuted' | 'n/a'. Sequential, independent re-measurement."""
    code, url = f["code"], f["url"]
    try:
        if code in STATUS_CODES:
            if "soft 404" in f["message"]:
                return "n/a"
            r = await client.get(url)
            return "confirmed" if r.status_code >= 400 else "refuted"
        if code == "site_down":
            try:
                r = await client.get(url)
                return "confirmed" if r.status_code >= 500 else "refuted"
            except httpx.HTTPError:
                return "confirmed"
        if code in HEADER_CODES:
            r = await client.get(url)
            return "confirmed" if HEADER_CODES[code] not in r.headers else "refuted"
        if code == "missing_frame_protection":
            r = await client.get(url)
            ok = "x-frame-options" in r.headers or "frame-ancestors" in r.headers.get("content-security-policy", "")
            return "refuted" if ok else "confirmed"
        if code in PAGE_CODES:
            r = await client.get(url)
            p = parse_page(url, str(r.url), r.status_code, r.content, r.headers.get("content-type", ""))
            holds = {"missing_title": not p.title, "missing_meta_description": not p.meta_description,
                     "missing_h1": p.h1_count == 0, "missing_lang": not p.lang,
                     "img_missing_alt": bool(p.imgs_missing_alt)}[code]
            return "confirmed" if holds else "refuted"
        if code == "slow_response":
            best = 1e9
            for _ in range(2):
                t0 = time.perf_counter()
                async with client.stream("GET", url) as r:
                    best = min(best, (time.perf_counter() - t0) * 1000)
            return "confirmed" if best > cfg.slow_ms else "refuted"
        if code == "page_too_heavy":
            r = await client.get(url)
            return "confirmed" if len(r.content) > cfg.heavy_bytes else "refuted"
        if code == "missing_security_txt":
            r = await client.get(url)
            return "confirmed" if r.status_code != 200 or "contact:" not in r.text.lower() else "refuted"
        if code == "soft_404":
            r = await client.get(urljoin(start_url, f"/hc-verify-{secrets.token_hex(5)}"))
            return "confirmed" if r.status_code == 200 else "refuted"
        if code.startswith("cert_"):
            host = urlparse(url).hostname
            try:
                await asyncio.to_thread(_tls_connect, host)
                return "refuted"
            except ssl.SSLError:
                return "confirmed"
            except OSError:
                return "n/a"                       # timeout / reset: can't tell either way
        if code in ("no_https", "no_https_redirect"):
            works = False                          # same definition as the checker: a real HTTPS response,
            for _ in range(2):                     # with one retry (some servers drop the first connection)
                try:
                    await client.get(url.replace("http://", "https://", 1))
                    works = True
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(1)
            return ("refuted" if works else "confirmed") if code == "no_https" else ("confirmed" if works else "refuted")
    except httpx.HTTPError:
        return "n/a"
    return "n/a"


def _tls_connect(host: str):
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=10) as s, ctx.wrap_socket(s, server_hostname=host):
        pass


async def eval_site(name, url, category, expect, out_dir, sem) -> dict:
    async with sem:
        cfg = load_config(None, None, {"reports_dir": str(out_dir), "max_pages": 15, "max_requests": 250,
                                       "time_budget_s": 60, "concurrency": 6, "max_external": 20,
                                       "llm": {"enabled": False}, "notify": "never"})
        t0 = time.perf_counter()
        try:
            report = await run_health_check(url, cfg)
        except Exception as e:      # noqa: BLE001 - the eval must record crashes, not die
            return {"name": name, "url": url, "category": category, "finished": False, "error": repr(e)[:200]}
        took = time.perf_counter() - t0
        actionable = [f for f in report["findings"] if f["severity"] != "info"]
        # sample up to 8 findings, one per code first so the sample is diverse
        by_code, sample = {}, []
        for f in actionable:
            by_code.setdefault(f["code"], []).append(f)
        while len(sample) < 8 and any(by_code.values()):
            for c in list(by_code):
                if by_code[c] and len(sample) < 8:
                    sample.append(by_code[c].pop(0))
        verdicts = []
        # same UA as the checker: some sites serve different HTML per User-Agent (lemonde.fr: 2 MB vs 340 KB),
        # and we want to re-check the *measurement*, not compare two different variants of the page
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, verify=True,
                                     headers={"User-Agent": cfg.user_agent}) as client:
            for f in sample:                       # sequential on purpose
                try:                               # the harness gets the same safety net as the tool
                    v = await asyncio.wait_for(verify(f, report["target"], client, cfg), 60)
                except Exception:                  # noqa: BLE001
                    v = "n/a"
                verdicts.append((f["code"], f["url"], v))
        codes = {f["code"] for f in report["findings"]}
        exp_ok = all(c in codes for c in expect.get("must", [])) and \
            (not expect.get("must_any") or any(c in codes for c in expect["must_any"])) and \
            not any(c in codes for c in expect.get("must_not", [])) and \
            (not expect.get("verdict_in") or report["verdict"] in expect["verdict_in"])
        s = report["stats"]
        checked, skipped = s["urls_checked"], s.get("urls_skipped_budget", 0)
        return {
            "name": name, "url": url, "category": category, "finished": True, "verdict": report["verdict"],
            "score": report["score"], "seconds": round(took, 1), "pages": s["pages_crawled"], "urls": checked,
            "coverage": round(checked / max(1, checked + skipped), 2), "requests": s["requests"],
            "findings": len(actionable), "codes": sorted(codes),
            "confirmed": sum(v == "confirmed" for *_, v in verdicts),
            "refuted": [(c, u) for c, u, v in verdicts if v == "refuted"],
            "unverifiable": sum(v == "n/a" for *_, v in verdicts),
            "expectation": "pass" if exp_ok else "FAIL", "expect": expect,
            "notes": report.get("profile", {}).get("notes", []),
        }


async def main_async(only: str | None, out_dir: Path, parallel: int) -> list[dict]:
    sem = asyncio.Semaphore(parallel)
    corpus = [c for c in CORPUS if not only or only in c[0] or only in c[2]]
    return await asyncio.gather(*(eval_site(n, u, c, e, out_dir, sem) for n, u, c, e in corpus))


def to_markdown(rows: list[dict]) -> str:
    fin = [r for r in rows if r["finished"]]
    conf = sum(r["confirmed"] for r in fin)
    ref = sum(len(r["refuted"]) for r in fin)
    exp = [r for r in fin if r["expect"]]
    lines = [
        "# Real-world generalisation eval", "",
        f"**{len(fin)}/{len(rows)} sites finished without crashing** | "
        f"verified precision **{conf}/{conf + ref} = {conf / max(1, conf + ref):.0%}** "
        f"(independent re-check of sampled findings) | "
        f"known-answer expectations **{sum(r['expectation'] == 'pass' for r in exp)}/{len(exp)}**", "",
        "| site | category | verdict | score | time | pages / URLs | coverage | findings | verified | expectation |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if not r["finished"]:
            lines.append(f"| {r['name']} | {r['category']} | CRASH | | | | | | | {r['error']} |")
            continue
        ver = f"{r['confirmed']}/{r['confirmed'] + len(r['refuted'])}" if r["confirmed"] + len(r["refuted"]) else "-"
        lines.append(f"| {r['name']} | {r['category']} | {r['verdict']} | {r['score']} | {r['seconds']}s | "
                     f"{r['pages']} / {r['urls']} | {r['coverage']:.0%} | {r['findings']} | {ver} | "
                     f"{r['expectation'] if r['expect'] else '-'} |")
    refuted = [(r["name"], c, u) for r in fin for c, u in r["refuted"]]
    if refuted:
        lines += ["", "## Findings the verifier refuted", ""] + [f"- {n}: `{c}` {u}" for n, c, u in refuted]
    notes = [(r["name"], n) for r in fin for n in r["notes"]]
    if notes:
        lines += ["", "## How sites were handled (profiler notes)", ""] + [f"- **{n}**: {t}" for n, t in notes]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--out", default="reports/realworld")
    ap.add_argument("--md", default=None, help="also write the markdown table here")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = asyncio.run(main_async(a.only, out, a.parallel))
    (out / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    md = to_markdown(rows)
    print(md)
    if a.md:
        Path(a.md).write_text(md, encoding="utf-8")
    fin = [r for r in rows if r["finished"]]
    conf, ref = sum(r["confirmed"] for r in fin), sum(len(r["refuted"]) for r in fin)
    ok = len(fin) == len(rows) and conf / max(1, conf + ref) >= 0.9 and all(r["expectation"] == "pass" for r in fin if r["expect"])
    print("REALWORLD EVAL", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
