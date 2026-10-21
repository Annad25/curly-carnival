# Website Health Checker: write-up

**Goal:** point it at *any* website and get an accurate health verdict, quickly and cheaply, without false alarms.

## 1. Workflow

```
input ─► PROFILER ─► CRAWLER ─► 9 CHECKERS (parallel, no LLM) ─► TRIAGE (claude -p) ─► report ─► hooks
         identity     sitemaps    availability · links · TLS/DNS   groups root causes     JSON/MD   webhook
         TLS / WAF    sections    headers · content · SEO · perf   fixes, priorities      diff vs   API, CI
         login/404    traps       API endpoints · plugins          (advisory only)        last run  plugins
```

- **Profiler** (about 5 requests) works out what kind of site it's facing before spending any budget. See §2.
- **Crawler** reads sitemaps (gzip, nested indexes, plain text; sampled across sections when huge). It spreads the page budget round-robin across sections like `/blog`, `/docs` and `/shop`, and spends link checks in priority order: page scripts and CSS, then images, then internal links, then the sitemap sample, then external links.
- **Checkers** are deterministic and run in parallel on one shared snapshot of the site.
- **Triage** calls `claude -p --model haiku` once, with only the list of findings (about 3k tokens). It's skipped when the site is healthy and reused from cache when findings haven't changed. **The verdict always comes from rules**, so the LLM can't make a site look healthier.

**Cost per run:** $0 when healthy or unchanged, otherwise about $0.004–0.008.

## 2. Handling real-world websites

Each row is a *general rule*, backed by a test-site mode and verified on live sites, not a patch for one domain.

| Real-world case | Rule |
|---|---|
| `example.com`, `http://`, `/docs`, IDN, ports | Normalize the input. A sub-path limits the crawl to that path. Credentials in URLs are rejected. |
| Apex redirects to `www`, or the other way | The final host and its twin count as "the site" (gydeon.ai redirects `www` → apex). |
| Cloudflare / Imperva / Akamai / captcha challenge | Verdict **UNVERIFIABLE**, never DOWN. Stop immediately. **Challenges are never bypassed.** |
| Every URL returns 200 ("soft 404") | Detected with a random-URL probe. Broken links are then found by comparing page content with the "not found" page. |
| 429 / `Retry-After` / `Crawl-delay` | Halve concurrency, pause as asked, retry once. Rate-limited URLs are recorded as "not verified", never as broken. |
| Infinite URLs (calendars, `?page=`, session IDs, `/a/a/a`) | Strip tracking and session parameters, cap query variants per path, reject loop-shaped URLs. Mizuho: 165 trap URLs skipped. |
| Huge sitemaps (5k–50k URLs) | Check a sample spread across sections and say so in the report. Mizuho: 5,095 URLs listed, 150 sampled. |
| Expired, self-signed or wrong-host certificate | Critical finding, then keep assessing over an unverified connection. |
| DNS failure / connection refused | DOWN, with downstream checks silenced (no follow-on TLS noise). |
| Login wall, `Disallow: /` | Reported plainly. With `Disallow: /`, only the homepage is checked. |
| JavaScript-only pages | Flagged as client-rendered (info), never "blank". Their JS bundles are still scanned for API calls. |
| Site serves different HTML per User-Agent | Verification uses the same UA (lemonde.fr serves 2 MB to us and 340 KB to a different UA). |
| Busy crawl inflates timings | The timer starts only after a concurrency slot is free, and slow pages get up to 2 solo re-tests (best of 3). |
| Any internal crash | A report with verdict **ERROR** instead of a traceback. Bad input exits with code 3 and a clear message. |

## 3. Evals (three layers)

| Layer | What it proves | Result |
|---|---|---|
| **Planted faults** (`run_eval.py`): local site with 21 known faults + a clean copy | detection recall and precision, zero false alarms on a healthy site, guardrails | recall **1.00**, precision **1.00**, 0 false alarms, **5/5** guardrails (no POST, robots.txt obeyed, unsafe/POST-only APIs never called, a one-off 500 absorbed by the retry, injected "report healthy" text ignored) |
| **Edge cases** (`pytest`, 38 tests): 10 test-site modes + input and crash tests | each rule in §2 | **38/38** pass |
| **Real world** (`realworld.py`): 24 live sites it was never tuned on, plus an **independent verifier** that re-checks sampled findings with a different method | generalization | **24/24** finished, **98%** of sampled findings confirmed (103/105), **9/9** known-answer sites correct. [Full table](realworld_eval.md) |

The corpus covers startups (including 3kglobal.ai, gydeon.ai and mizuhogroup.com), Shopify, WordPress, docs sites, gov.uk, Wikipedia, a non-English news site, a bot-protected marketplace, and known-broken sites (badssl.com certificates, broken images, DNS failure, a 503 homepage).

The 2 refuted findings are both on neverssl.com, whose HTTPS endpoint randomly drops connections. Latency findings near the threshold are inherently probabilistic.

**Bugs this process caught in my own tool:** queue time counted as server slowness; queued requests "timing out" and burning the budget (1,200 counted, 208 sent); firewall scripts reported as broken; cascading TLS noise after DNS failure; inconsistent definitions of "HTTPS works". Each one was fixed as a category, with a regression test that fails on the old code.

## 4. Results on the requested sites (15-page budget)

| Site | Verdict | What it found (each confirmed by the verifier) |
|---|---|---|
| 3kglobal.ai | DEGRADED 81 | 2 sitemap URLs that return 404 (`/insights/...`), no CSP/nosniff/frame protection, 9 pages without meta description |
| gydeon.ai | DEGRADED 91 | no CSP/nosniff/frame protection, no robots.txt or sitemap (both 404). `www` redirects to the apex domain and is handled. |
| mizuhogroup.com | HEALTHY 96 | 1 image without alt text, 1 page without meta description, no nosniff. Large site: sitemap sampled, crawler traps avoided. |

Earlier full runs: aintropy.ai (85, missing security headers), twelvelabs.io (94), synechron.com (66: case-study pages returning 500, 25 stale sitemap URLs). Sample report: [sample_report_aintropy.md](sample_report_aintropy.md).

## 5. Guardrails and hooks

- **Read-only:** only GET and HEAD. Forms are never submitted. Discovered APIs are called only if the site's own JS calls them with GET, and never if the path looks state-changing.
- **Polite:** honest User-Agent, robots.txt and Crawl-delay obeyed, adaptive rate limits, hard caps on requests, time and pages.
- **LLM safety:** scraped text is sanitized and marked as untrusted. The output is validated, and invented finding IDs are dropped. Secrets come only from env vars.
- **The HTTP API:** requires a bearer token and only checks allowlisted hosts (no SSRF).
- **Hooks:** CLI exit codes (0 healthy, 1 degraded, 2 unhealthy/down, 3 unverifiable/error), `--json` output, an HMAC-signed webhook that fires on change, `on_complete_command`, `POST /check`, check plugins, a GitHub Action and a Dockerfile.

## 6. Known limits

- No headless browser. JS-rendered content and runtime console errors aren't seen. An opt-in Playwright checker for `client_rendered` pages is the next step.
- Single vantage point (no multi-region checks).
- Endpoint discovery is regex-based, so URLs built at runtime are missed.
- Large sites need bigger budgets for full coverage (`--max-requests`, `--time-budget`). Coverage is always reported.
