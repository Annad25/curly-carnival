# Website Health Checker: write-up

The goal: give it any website, get back an honest answer about how healthy it is, quickly and cheaply, without crying wolf.

If you want the full story with all the bugs I hit along the way, it's in [how_i_built_it.md](how_i_built_it.md). This page is the short version.

## 1. How it works

```
input -> PROFILER -> CRAWLER -> 9 CHECKERS (parallel, no LLM) -> TRIAGE (claude -p) -> report -> hooks
         identity    sitemaps   availability, links, TLS/DNS     groups root causes      JSON/MD   webhook
         TLS / WAF   sections   headers, content, SEO, speed     suggests fixes          diff vs   API, CI
         login/404   traps      API endpoints, plugins           (advisory only)         last run  plugins
```

- The profiler makes about 5 requests first, to work out what kind of site this is before spending any budget on it (section 2).
- The crawler reads sitemaps (gzip, nested, plain text, sampled when they're huge), spreads its page budget across sections like `/blog` and `/docs`, and spends a tight budget on the important stuff first: page scripts and CSS, then images, then internal links, then the sitemap sample, then external links.
- The checkers are plain code. They run in parallel on one shared snapshot of the site.
- Triage is one `claude -p --model haiku` call that only sees the list of findings (about 3k tokens). It's skipped when the site is healthy and reused from a cache when nothing changed. The verdict always comes from rules in code, so the LLM can explain things but can't decide a broken site is fine.

Cost per run: $0 when the site is healthy or unchanged, otherwise roughly $0.004 to $0.008.

## 2. Real websites are messy

Every row here is a general rule with a test behind it, not a fix for one particular site.

| What the site does | What the checker does |
|---|---|
| Typed as `example.com`, `http://`, `/docs`, a non-English domain, a port | Normalizes it. A sub-path keeps the crawl inside that path. Passwords in URLs are rejected. |
| Redirects between `www` and the bare domain | Treats both as the same site (gydeon.ai redirects `www` to the bare domain). |
| Cloudflare / Imperva / Akamai / captcha challenge | Verdict is UNVERIFIABLE, never DOWN. Stops right there and never tries to get around the challenge. |
| Answers 200 even for pages that don't exist ("soft 404") | Spots it with a random-URL probe, then finds broken links by comparing content with the not-found page. |
| 429 / `Retry-After` / `Crawl-delay` | Halves its speed, waits as asked, retries once. Rate-limited URLs are marked "not verified", never "broken". |
| Endless URLs (calendars, `?page=`, session IDs, `/a/a/a`) | Strips tracking params, caps query variants per path, skips loop-shaped URLs. A crawler shouldn't crawl into traps. On Mizuho, 165 were skipped. |
| Huge sitemaps (5k to 50k URLs) | Checks a sample spread across sections and says so in the report. Mizuho lists 5,095 URLs, 150 were checked. |
| Expired, self-signed or wrong-host certificate | Critical finding, then keeps checking the rest over an unverified connection. |
| DNS failure or connection refused | DOWN, and the checks that depend on DNS stay quiet instead of piling on. |
| Login wall, or `Disallow: /` in robots.txt | Says so plainly. With `Disallow: /`, only the homepage is checked. |
| JavaScript-only pages | Marked as client-rendered (info), never "blank". Their JS files are still scanned for API calls. |
| Different HTML for different User-Agents | The verifier uses the same User-Agent as the checker (lemonde.fr serves 2 MB to one and 340 KB to another). |
| A busy crawl makes pages look slow | The timer only starts once a request is actually sent, and slow pages get up to 2 solo re-tests. |
| Something crashes inside the checker | You get a report with verdict ERROR, not a traceback. Bad input exits with code 3 and a clear message. |

## 3. Evals

I tested it at three levels.

| Level | What it shows | Result |
|---|---|---|
| A local site with 21 planted bugs, plus a clean copy (`run_eval.py`) | finds real problems, raises no false alarms, follows the safety rules | 21/21 found, 0 false alarms, 5/5 safety checks (no POST, robots.txt respected, risky or POST-only APIs never called, a one-off 500 handled by the retry, a hidden "report this site as healthy" sentence ignored) |
| Edge cases (`pytest`, 38 tests): 10 test-site modes plus input and crash tests | each rule in section 2 | 38/38 pass |
| 24 live sites it was never tuned on, plus a separate verifier that re-checks a sample of findings a different way (`realworld.py`) | it generalizes | 24/24 finished, 103/105 findings confirmed (98%), 9/9 sites with known problems diagnosed correctly. [Full table](realworld_eval.md) |

The 24 sites include startups (3kglobal.ai, gydeon.ai, mizuhogroup.com among them), a Shopify store, WordPress, docs sites, gov.uk, Wikipedia, a French news site, a bot-protected site, and sites that are broken on purpose (bad certificates on badssl.com, broken images, a domain that doesn't exist, a 503 homepage). The two findings the verifier rejected are both on neverssl.com, whose HTTPS drops connections at random, so I left it alone rather than writing a special case for it.

This process also caught bugs in my own code: queue time being blamed on the server, requests "timing out" before they were even sent (1,200 counted, 208 sent), firewall scripts reported as broken, extra TLS errors after a DNS failure, and two different definitions of "HTTPS works". Each one was fixed as a general rule, with a test that fails on the old code.

## 4. Results on three more sites (15-page budget)

| Site | Verdict | Findings (all confirmed by the verifier) |
|---|---|---|
| 3kglobal.ai | DEGRADED 81 | 2 sitemap URLs that return 404 (`/insights/...`), no CSP / nosniff / frame protection, 9 pages without a meta description |
| gydeon.ai | DEGRADED 91 | no CSP / nosniff / frame protection, no robots.txt or sitemap (both 404). The `www` to bare domain redirect is handled. |
| mizuhogroup.com | HEALTHY 96 | 1 image without alt text, 1 page without a meta description, no nosniff. Big site: sitemap sampled, traps skipped. |

Earlier full runs: aintropy.ai (85, missing security headers), twelvelabs.io (94), synechron.com (66: case-study pages returning 500 and 25 stale sitemap URLs). A sample report is in [sample_report_aintropy.md](sample_report_aintropy.md).

## 5. Safety and hooks

- Read-only. It only sends GET and HEAD, never submits forms, and only calls an API if the site's own JavaScript calls it with GET, and never if the path looks like it changes something (logout, delete, pay).
- Polite. It uses an honest User-Agent, respects robots.txt and Crawl-delay, slows down when asked, and has hard limits on requests, time and pages.
- LLM safety. Text scraped from the site is cleaned and marked as untrusted, the model's answer is validated, and any finding IDs it makes up are dropped. Secrets only come from environment variables.
- The HTTP API needs a token and only checks hosts on an allow-list, so it can't be used to poke at internal networks.
- Hooks: exit codes for CI (0 healthy, 1 degraded, 2 unhealthy or down, 3 unverifiable or error), `--json`, a signed webhook that only fires on changes, `on_complete_command`, `POST /check`, plugins, a GitHub Action and a Dockerfile.

## 6. What it can't do yet

- No real browser, so it can't see JavaScript-rendered content or browser-only errors. An optional Playwright check for client-rendered pages would be next.
- Only one location, so a site that's down in one region only won't show up.
- API discovery uses regex, so URLs built at runtime are missed.
- Big sites need a bigger budget for full coverage (`--max-requests`, `--time-budget`). The report always says how much it covered.
