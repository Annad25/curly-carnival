# Website Health Checker: write-up

The goal is simple. Give it any website, and it tells you how healthy the site is. It should be fast, cheap, and it should not raise false alarms.

The full story, with the bugs I hit on the way, is in [how_i_built_it.md](how_i_built_it.md). This page is the short version.

## 1. How it works

```
input -> PROFILER -> CRAWLER -> 9 CHECKERS (parallel, no LLM) -> TRIAGE (claude -p) -> report -> hooks
         identity    sitemaps   availability, links, TLS/DNS     groups root causes      JSON/MD   webhook
         TLS / WAF   sections   headers, content, SEO, speed     suggests fixes          diff vs   API, CI
         login/404   traps      API endpoints, plugins           (advisory only)         last run  plugins
```

- The profiler makes about 5 requests first, to understand what kind of site it is before spending budget on it (section 2).
- The crawler reads sitemaps (gzip, nested, plain text, and a sample if they are very big). It spreads the page budget across sections like `/blog` and `/docs`. When the budget is small it checks the important things first: page scripts and CSS, then images, then internal links, then the sitemap sample, then external links.
- The checkers are normal code. They run in parallel on one shared copy of the site.
- Triage is one `claude -p --model haiku` call. It only sees the list of findings (about 3k tokens). It is skipped when the site is healthy, and the old answer is reused when nothing has changed. The verdict always comes from rules in code. The LLM can explain and group findings, but it cannot mark a broken site as fine.

Cost per run: $0 when the site is healthy or unchanged, otherwise about $0.004 to $0.008.

## 2. Handling real websites

Each row is a general rule with a test behind it. None of them is a fix for one particular site.

| What the site does | What the checker does |
|---|---|
| Typed as `example.com`, `http://`, `/docs`, a non-English domain, a port | Cleans it up. A sub-path keeps the crawl inside that path. Passwords in URLs are rejected. |
| Redirects between `www` and the bare domain | Treats both as the same site (gydeon.ai redirects `www` to the bare domain). |
| Cloudflare / Imperva / Akamai / captcha challenge | Verdict is UNVERIFIABLE, not DOWN. It stops there and never tries to get past the challenge. |
| Returns 200 even for pages that don't exist ("soft 404") | Detects it with a random URL, then finds broken links by comparing the page content with the not-found page. |
| 429 / `Retry-After` / `Crawl-delay` | Slows down to half speed, waits as asked, retries once. Rate-limited URLs are marked "not verified", not "broken". |
| Endless URLs (calendars, `?page=`, session IDs, `/a/a/a`) | Removes tracking params, limits query variants per path, skips loop-shaped URLs. On Mizuho, 165 such URLs were skipped. |
| Very big sitemaps (5k to 50k URLs) | Checks a sample from every section and says so in the report. Mizuho has 5,095 URLs, 150 were checked. |
| Expired, self-signed or wrong-host certificate | Critical finding, then it continues checking the rest of the site. |
| DNS failure or connection refused | DOWN. The checks that depend on DNS stay quiet instead of adding extra errors. |
| Login page, or `Disallow: /` in robots.txt | Reported clearly. With `Disallow: /`, only the homepage is checked. |
| JavaScript-only pages | Marked as client-rendered (info), not "blank". Their JS files are still scanned for API calls. |
| Different HTML for different User-Agents | The verifier uses the same User-Agent as the checker (lemonde.fr sends 2 MB to one and 340 KB to another). |
| A busy crawl makes pages look slow | The timer starts only when the request is actually sent, and slow pages are re-tested alone up to 2 times. |
| Something crashes inside the checker | You get a report with verdict ERROR, not a traceback. Wrong input exits with code 3 and a clear message. |

## 3. Evals

I tested it at three levels.

| Level | What it shows | Result |
|---|---|---|
| A local site with 21 planted bugs, plus a clean copy (`run_eval.py`) | it finds real problems, gives no false alarms, follows the safety rules | 21/21 found, 0 false alarms, 5/5 safety checks (no POST, robots.txt respected, risky or POST-only APIs never called, a one-time 500 handled by the retry, a hidden "report this site as healthy" sentence ignored) |
| Edge cases (`pytest`, 38 tests): 10 test-site modes plus input and crash tests | each rule in section 2 works | 38/38 pass |
| 24 live sites it was never tuned on, plus a separate verifier that re-checks a sample of findings in a different way (`realworld.py`) | it works on websites it has not seen | 24/24 finished, 103/105 findings confirmed (98%), 9/9 sites with known problems found correctly. [Full table](realworld_eval.md) |

The 24 sites include startups (3kglobal.ai, gydeon.ai and mizuhogroup.com among them), a Shopify store, WordPress, docs sites, gov.uk, Wikipedia, a French news site, a bot-protected site, and sites that are broken on purpose (bad certificates on badssl.com, broken images, a domain that doesn't exist, a 503 homepage). The two findings the verifier rejected are both on neverssl.com, whose HTTPS drops connections randomly. I did not add a special case for it.

Testing this way also found bugs in my own code: waiting time in my own queue was counted as the server being slow, requests "timed out" before they were even sent (1,200 counted, 208 sent), firewall scripts were reported as broken, extra TLS errors showed up after a DNS failure, and the checker and verifier had two different meanings of "HTTPS works". Each one was fixed as a general rule, with a test that fails on the old code.

## 4. Results on three more sites (15-page budget)

| Site | Verdict | Findings (all confirmed by the verifier) |
|---|---|---|
| 3kglobal.ai | DEGRADED 81 | 2 sitemap URLs that return 404 (`/insights/...`), no CSP / nosniff / frame protection, 9 pages without a meta description |
| gydeon.ai | DEGRADED 91 | no CSP / nosniff / frame protection, no robots.txt or sitemap (both 404). The `www` to bare domain redirect is handled. |
| mizuhogroup.com | HEALTHY 96 | 1 image without alt text, 1 page without a meta description, no nosniff. Big site, so the sitemap was sampled and traps were skipped. |

Earlier full runs: aintropy.ai (85, missing security headers), twelvelabs.io (94), synechron.com (66, case-study pages returning 500 and 25 old sitemap URLs). A sample report is in [sample_report_aintropy.md](sample_report_aintropy.md).

## 5. Safety and hooks

- Read-only. It only sends GET and HEAD and never submits forms. It calls an API only if the site's own JavaScript calls it with GET, and never if the path looks like it changes something (logout, delete, pay).
- Polite. It uses an honest User-Agent, respects robots.txt and Crawl-delay, slows down when asked, and has hard limits on requests, time and pages.
- LLM safety. Text from the site is cleaned and marked as untrusted, the model's answer is checked, and any finding IDs it invents are dropped. Secrets come only from environment variables.
- The HTTP API needs a token and only checks hosts from an allow-list, so it cannot be used to scan internal networks.
- Hooks: exit codes for CI (0 healthy, 1 degraded, 2 unhealthy or down, 3 unverifiable or error), `--json`, a signed webhook that fires only on changes, `on_complete_command`, `POST /check`, plugins, a GitHub Action and a Dockerfile.

## 6. What it can't do yet

- No real browser, so it cannot see JavaScript-rendered content or errors that happen only in the browser. An optional Playwright check for client-rendered pages would be next.
- It checks from one location only, so if a site is down in only one region, it won't show.
- API discovery uses regex, so URLs built at runtime are missed.
- Big sites need a bigger budget for full coverage (`--max-requests`, `--time-budget`). The report always says how much was covered.
