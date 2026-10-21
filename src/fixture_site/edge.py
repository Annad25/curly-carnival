"""Edge-case websites: one local server, one MODE per real-world failure category.

Each mode reproduces a behaviour seen on real sites so the checker's handling of it is tested,
not hoped for:

  soft404       every unknown URL answers 200 with a "not found" page
  waf           homepage is a Cloudflare-style JS challenge (403 + cf-mitigated)
  ratelimit     every 3rd request gets 429 + Retry-After
  redirect_host 127.0.0.1 redirects to localhost (apex -> www style host change)
  charset       windows-1252 page declared only in <meta charset>
  trap          infinite calendar (?month=N) + relative-link loop
  gzsitemap     sitemap index -> two gzipped sitemaps with 6,000 URLs
  spa           client-rendered shell (no server text)
  login         homepage redirects to /login
  disallow      robots.txt: Disallow: /
"""
from __future__ import annotations

import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ["soft404", "waf", "ratelimit", "redirect_host", "charset", "trap", "gzsitemap", "spa", "login", "disallow"]

FILLER = "This paragraph is real content so the page is not mistaken for an empty or error page. " * 3


def page(title: str, body: str, lang: str = "en", head: str = "") -> str:
    return (f'<!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>'
            f'<meta name="description" content="{title} page">{head}</head><body>'
            f'<nav><a href="/">Home</a></nav><h1>{title}</h1><p>{FILLER}</p>{body}</body></html>')


NOT_FOUND = page("Acme", "<p>Sorry, we could not find that page. Try the homepage.</p>")
SPA_SHELL = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>App</title>'
             '<meta name="description" content="app"><script type="module" src="/app.js"></script></head>'
             '<body><div id="root"></div></body></html>')


def make_handler(mode: str, hits: list):
    class H(BaseHTTPRequestHandler):
        server_version, sys_version = "EdgeSite", ""

        def log_message(self, *a):
            pass

        def send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            hits.append(("POST", self.path))
            self.send(405)

        def do_GET(self):  # noqa: C901 - a switchboard of behaviours on purpose
            path, _, query = self.path.partition("?")
            hits.append((self.command, self.path))
            host = self.headers.get("Host", "")
            port = self.server.server_address[1]
            if path == "/.well-known/security.txt":
                return self.send(200, "Contact: mailto:sec@example.com\n", "text/plain")
            if path == "/app.js":
                return self.send(200, "document.getElementById('root').textContent='hi'", "application/javascript")

            if mode == "soft404":
                if path == "/robots.txt":
                    return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
                if path == "/":
                    return self.send(200, page("Home", '<a href="/exists">Exists</a> <a href="/gone">Gone</a>'))
                if path == "/exists":
                    return self.send(200, page("Exists", "<p>A real page with its own words: pricing plans.</p>"))
                return self.send(200, NOT_FOUND)                     # soft 404 for everything else

            if mode == "waf":
                return self.send(403, "<html><head><title>Just a moment...</title></head><body>"
                                      "<div id='cf-chl-widget'>Enable JavaScript and cookies to continue</div>"
                                      "</body></html>", headers={"cf-mitigated": "challenge", "Server": "cloudflare"})

            if mode == "ratelimit":
                n = sum(1 for m, _ in hits if m in ("GET", "HEAD"))
                if n % 3 == 0 and path != "/":
                    return self.send(429, "slow down", "text/plain", {"Retry-After": "1"})
                if path == "/robots.txt":
                    return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
                links = " ".join(f'<a href="/p{i}">p{i}</a>' for i in range(12))
                if path == "/" or path.startswith("/p"):
                    return self.send(200, page(f"Page {path}", links if path == "/" else ""))
                return self.send(404, page("Not found", ""))

            if mode == "redirect_host":
                if host.startswith("127.0.0.1"):
                    return self.send(301, headers={"Location": f"http://localhost:{port}{self.path}"})
                if path == "/robots.txt":
                    return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
                if path in ("/", "/about", "/team"):
                    return self.send(200, page(path.strip("/") or "Home", '<a href="/about">About</a> <a href="/team">Team</a>'))
                return self.send(404, page("Not found", ""))

            if mode == "charset":
                if path == "/":
                    html = ('<!DOCTYPE html><html lang="fr"><head><meta charset="windows-1252"><title>Café crème</title>'
                            '<meta name="description" content="café"></head><body><h1>Café</h1>'
                            f'<p>Déjà vu à la française. {FILLER}</p></body></html>')
                    return self.send(200, html.encode("cp1252"), "text/html")   # no charset in the header
                return self.send(404, page("Not found", ""))

            if mode == "trap":
                if path == "/robots.txt":
                    return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
                if path == "/":
                    return self.send(200, page("Home", '<a href="/calendar?month=1">Calendar</a> <a href="a/">loop</a>'))
                if path == "/calendar":
                    links = " ".join(f'<a href="/calendar?month={m}">m{m}</a>' for m in range(1, 40))
                    return self.send(200, page("Calendar", links))
                if path.endswith("/a/") or path == "/a/":
                    return self.send(200, page("Loop", '<a href="a/">deeper</a>'))   # /a/a/a/a/...
                return self.send(404, page("Not found", ""))

            if mode == "gzsitemap":
                if path == "/robots.txt":
                    return self.send(200, f"User-agent: *\nSitemap: http://{host}/sitemap_index.xml\n", "text/plain")
                if path == "/sitemap_index.xml":
                    xml = ('<?xml version="1.0"?><sitemapindex>'
                           f'<sitemap><loc>http://{host}/sm1.xml.gz</loc></sitemap>'
                           f'<sitemap><loc>http://{host}/sm2.xml.gz</loc></sitemap></sitemapindex>')
                    return self.send(200, xml, "application/xml")
                if path in ("/sm1.xml.gz", "/sm2.xml.gz"):
                    start = 0 if path == "/sm1.xml.gz" else 3000
                    sec = ["blog", "docs", "shop"]
                    locs = "".join(f"<url><loc>http://{host}/{sec[i % 3]}/{i}</loc></url>" for i in range(start, start + 3000))
                    return self.send(200, gzip.compress(f'<?xml version="1.0"?><urlset>{locs}</urlset>'.encode()),
                                     "application/x-gzip")
                if path == "/" or path.split("/")[1] in ("blog", "docs", "shop"):
                    return self.send(200, page(f"Page {path}", ""))
                return self.send(404, page("Not found", ""))

            if mode == "spa":
                if path == "/robots.txt":
                    return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
                return self.send(200, SPA_SHELL) if path == "/" else self.send(404, page("Not found", ""))

            if mode == "login":
                if path == "/robots.txt":
                    return self.send(200, "User-agent: *\nAllow: /\n", "text/plain")
                if path == "/":
                    return self.send(302, headers={"Location": "/login?next=/"})
                if path == "/login":
                    return self.send(200, page("Sign in", '<form method="post" action="/login">'
                                                          '<input type="password" name="p"></form>'))
                return self.send(404, page("Not found", ""))

            if mode == "disallow":
                if path == "/robots.txt":
                    return self.send(200, "User-agent: *\nDisallow: /\n", "text/plain")
                if path == "/":
                    return self.send(200, page("Home", '<a href="/secret">Secret</a> <a href="/other">Other</a>'))
                return self.send(200, page("Other", ""))

            return self.send(500, "unknown mode")

    return H


def start(mode: str, port: int = 0):
    assert mode in MODES, mode
    hits: list = []
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(mode, hits))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/", hits
