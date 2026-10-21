"""One test per real-world failure category (see fixture_site/edge.py).

These guard the *generalisation* of the checker: each encodes a rule ("a WAF challenge is not
downtime", "429 is not a broken link", ...) rather than a fix for one particular website.
"""
import asyncio
import socket

import pytest

from fixture_site import edge
from healthchecker import runner
from healthchecker.config import load_config
from healthchecker.profile import normalize_input
from healthchecker.runner import run_health_check


def run(mode, tmp_path, **over):
    srv, url, hits = edge.start(mode)
    try:
        cfg = load_config(None, None, {"reports_dir": str(tmp_path), "check_external": False,
                                       "llm": {"enabled": False}, "time_budget_s": 60, **over})
        report = asyncio.run(run_health_check(url, cfg))
    finally:
        srv.shutdown()
    codes = {}
    for f in report["findings"]:
        codes.setdefault(f["code"], []).append(f)
    return report, codes, hits


def paths(findings):
    return {f["url"].split("/", 3)[-1].split("?")[0] for f in findings}


def test_soft404_site_broken_link_detected_by_content(tmp_path):
    report, codes, _ = run("soft404", tmp_path)
    assert "soft_404" in codes
    broken = paths(codes.get("broken_link", []))
    assert "gone" in broken and "exists" not in broken


def test_waf_challenge_is_unverifiable_not_down(tmp_path):
    report, codes, hits = run("waf", tmp_path)
    assert report["verdict"] == "UNVERIFIABLE"
    assert "site_down" not in codes and "homepage_error" not in codes
    assert set(codes) <= {"blocked_by_bot_protection", "no_ipv6"}       # no noise about the challenge page
    assert len(hits) <= 3                                                # we stop instead of hammering


def test_rate_limited_urls_are_not_reported_broken(tmp_path):
    report, codes, _ = run("ratelimit", tmp_path)
    assert "rate_limited" in codes
    assert not codes.get("broken_link") and not codes.get("server_error")


def test_apex_to_www_style_host_redirect_still_crawls_the_site(tmp_path):
    report, codes, _ = run("redirect_host", tmp_path)
    assert report["stats"]["pages_crawled"] >= 3                         # /, /about, /team on the new host
    assert any("localhost" in h for h in report["profile"]["hosts"])
    assert not codes.get("broken_link")


def test_meta_charset_windows_1252_decoded(tmp_path):
    report, codes, _ = run("charset", tmp_path)
    assert "template_leak" not in codes and "missing_title" not in codes
    # the parsed title must be the real word, not mojibake
    from healthchecker.htmlparse import decode_html
    assert "Café" in decode_html('<meta charset="windows-1252"><title>Café</title>'.encode("cp1252"))


def test_crawler_traps_do_not_eat_the_budget(tmp_path):
    report, codes, hits = run("trap", tmp_path, max_pages=40)
    calendar_hits = [p for _, p in hits if p.startswith("/calendar")]
    loop_hits = [p for _, p in hits if "/a/a/a/a" in p]
    assert len(calendar_hits) <= 6 and not loop_hits
    assert "crawl_trap_avoided" in codes


def test_gzipped_sitemap_index_is_read_and_sampled(tmp_path):
    report, codes, _ = run("gzsitemap", tmp_path, max_requests=600)
    assert "sitemap_sampled" in codes
    assert "6000" in codes["sitemap_sampled"][0]["message"]
    assert not codes.get("sitemap_broken_url") and "missing_sitemap" not in codes


def test_spa_shell_is_not_reported_as_blank(tmp_path):
    report, codes, _ = run("spa", tmp_path)
    assert "client_rendered" in codes
    assert "empty_page" not in codes and "missing_h1" not in codes


def test_login_wall_reported(tmp_path):
    report, codes, hits = run("login", tmp_path)
    assert "auth_required" in codes
    assert not any(m == "POST" for m, _ in hits)                         # never submits the login form


def test_robots_disallow_all_only_touches_homepage(tmp_path):
    report, codes, hits = run("disallow", tmp_path)
    assert "robots_disallow_all" in codes
    touched = {p.split("?")[0] for _, p in hits}
    assert touched <= {"/", "/robots.txt"}, touched


def test_unreachable_site_is_down_not_a_crash(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                            # nothing listens here now
    cfg = load_config(None, None, {"reports_dir": str(tmp_path), "llm": {"enabled": False}, "timeout_s": 2})
    report = asyncio.run(run_health_check(f"http://127.0.0.1:{port}/", cfg))
    assert report["verdict"] == "DOWN"
    assert [f["code"] for f in report["findings"] if f["severity"] != "info"] == ["site_down"]


@pytest.mark.parametrize("raw,expected", [
    ("Example.com", "https://example.com/"),
    ("http://example.com:80/docs", "http://example.com/docs"),
    ("  https://WWW.Example.com/?utm_source=x  ", "https://www.example.com/"),
    ("bücher.de", "https://xn--bcher-kva.de/"),
])
def test_input_normalisation(raw, expected):
    assert normalize_input(raw) == expected


@pytest.mark.parametrize("raw", ["", "not a url", "ftp://example.com", "https://user:pass@example.com/"])
def test_bad_input_rejected_clearly(raw):
    with pytest.raises(ValueError):
        normalize_input(raw)


def test_internal_crash_becomes_error_report(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("simulated crawler bug")
    monkeypatch.setattr(runner, "crawl", boom)
    cfg = load_config(None, None, {"reports_dir": str(tmp_path), "llm": {"enabled": False}})
    report = asyncio.run(run_health_check("https://example.com/", cfg))
    assert report["verdict"] == "ERROR"
    assert report["findings"][0]["code"] == "tool_error"
