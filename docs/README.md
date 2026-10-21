# Website Health Checker: write-up

## 1. What "healthy" means here

A website can return `200 OK` and still be broken. I check eight areas. Each one is a separate checker agent, and they all run in parallel:

| Area | What is checked | Example finding codes |
|---|---|---|
| Availability | homepage up, every crawled page 2xx, redirect chains and loops | `site_down`, `server_error`, `redirect_chain` |
| Links & assets | broken internal/external links, missing images/JS/CSS | `broken_link`, `broken_asset` |
| Transport | DNS, TLS cert validity and expiry, HTTP→HTTPS redirect | `cert_expiring`, `no_https_redirect` |
| Security headers | HSTS, CSP, nosniff, clickjacking, version leaks, cookies | `missing_hsts`, `missing_csp` |
| Content | pages that return 200 but show an error, unrendered templates, injected text, blank pages, alt text, mixed content | `soft_error`, `template_leak`, `suspicious_injected_text` |
| SEO / discoverability | robots.txt, sitemap entries that 404, title, description, h1, noindex | `sitemap_broken_url`, `missing_title` |
| Performance | time to first byte, page weight, compression | `slow_response`, `page_too_heavy` |
| Business rules (plugin) | site-specific rules, e.g. the homepage must have a contact/demo link | `no_contact_path` |

## 2. Workflow (agents + reasoning)

```
            ┌──────────── Planner / Crawler ────────────┐
URL/config →│ robots.txt → sitemap → BFS (budgeted)       │
            │ HEAD-check every referenced asset/link      │
            └───────────────┬─────────────────────────────┘
                            ▼  (one shared, fetched snapshot of the site)
   availability  links  transport  headers  content  seo  perf  plugins   ← checker agents, parallel, no LLM
                            ▼  findings (code, severity, url, evidence)
            ┌──────── Triage agent (claude -p, haiku) ────────┐
            │ groups findings into root causes, gives fixes,   │  ← reasoning layer
            │ flags likely false positives                     │
            └───────────────┬──────────────────────────────────┘
                            ▼
   deterministic verdict + score → JSON/Markdown report → diff vs last run → hooks
```

**Why it's fast and cheap.** All detection is deterministic: async HTTP with an HTML parser, and no browser. The LLM only sees the list of findings, never raw HTML. On top of that:
- A healthy run makes no LLM call.
- A run whose findings match the last one reuses the cached triage, so it's also free.
- Otherwise it makes one `claude -p --model haiku` call with about 3k tokens.

The crawl reads the whole site once, and every checker works from that same snapshot.

## 3. Results

| Target | Time | Requests | Verdict | Notes |
|---|---|---|---|---|
| localhost fixture (14 planted faults) | 2.8 s scan + 21 s triage | 25 | UNHEALTHY | 14/14 found, 0 false positives, triage $0.0048 |
| localhost clean twin | ~2 s | – | HEALTHY | 0 findings |
| **https://aintropy.ai/** | 12-20 s scan + 14 s triage | 118 (17 pages, 110 URLs) | DEGRADED, 86/100 | triage $0.0038 first run, $0 cached re-run |

**aintropy.ai findings (real).**
- No `Strict-Transport-Security` header.
- No `Content-Security-Policy` header.
- No clickjacking protection.
- 13 images without alt text on the UniK research post.
- `/solutions/nj_demo` and `/vision/unik-results-blog-2/` have no `<h1>`.
- The two demo apps (`/solutions/nj_opendata/`, `/solutions/seattle_legal/`) have no meta description.

No broken links, a valid certificate and fast pages. Claude's triage grouped the three header findings into one root cause (the headers aren't set at the CDN/edge) and ranked it first. Full report: [sample_report_aintropy.md](sample_report_aintropy.md). The demo apps are React single-page apps. I report them as `client_rendered` (info only) rather than "blank page", because a crawler that doesn't run JavaScript can't judge their content.

## 4. Evals (`python src/evals/run_eval.py`)

| Metric | How | Threshold | Current |
|---|---|---|---|
| Recall | planted faults found / 14 | ≥ 0.95 | 1.00 |
| Precision | findings that are planted (or a known side effect) / all findings | ≥ 0.90 | 1.00 |
| False positives on a healthy site | findings on the clean twin | 0 | 0 |
| Guardrails | no POSTs, robots-disallowed `/private` never hit, within request budget, injected "report this site as healthy" text doesn't change the verdict | 4/4 | 4/4 |
| Efficiency | scan seconds, triage seconds, requests, LLM calls, $ per run | scan < 20 s, triage < 90 s | 2.8 s scan, 21 s triage, 1 call, $0.0048 |
| Triage quality (`--llm`) | share of planted faults covered by a root cause; invented IDs are dropped | 100% | 100% (9 root causes, 0 invented IDs) |

The fixture's ground truth lives in `src/fixture_site/server.py` (`EXPECTED`). The unit tests (`pytest`, 11 of them) cover the parser, the verdict rules, the guardrails, every triage path (Claude, cache, bad output, timeout) and the hooks.

## 5. Guardrails

- **Read-only:** the fetcher can only send GET and HEAD. Forms are recorded but never submitted.
- **Polite:** it obeys robots.txt and sends its own User-Agent. Concurrency, page count, request count and total time are all capped, and each request has a hard 20s limit.
- **LLM is advisory only:** the verdict and severities come from rules. The model can explain and group findings, but it can't make the site look healthier.
- **Untrusted input:** scraped text is truncated, control characters removed, secrets redacted and injection phrases stripped, then marked as data in the prompt. The reply must be JSON, and root causes that point at findings that don't exist are dropped. `claude` runs in an empty temp folder with no repo access.
- **Secrets:** read only from env vars (`CLAUDE_KEY`, `HEALTH_WEBHOOK_*`, `HEALTH_API_TOKEN`).
- **The HTTP API:** requires a bearer token and only checks allowlisted hosts, so it can't be used to probe internal networks.
- **Fails safe:** if `claude` is missing, slow or returns garbage, it falls back to rule-based triage. A crashing checker becomes an info finding instead of stopping the run.

## 6. Hooks: how it plugs into other systems

| Hook | Use |
|---|---|
| CLI and exit codes (0/1/2/3) | CI gates, cron, any scheduler |
| `--json` on stdout | pipe into anything |
| Signed webhook (`HEALTH_WEBHOOK_URL`, HMAC `X-HealthCheck-Signature`) | Slack, Teams, PagerDuty, n8n, Zapier. Has a Slack-ready `text` field. Fires only **on change** by default. |
| `on_complete_command` | run a local script (open a Jira ticket, push metrics) with `HEALTH_REPORT_PATH` set |
| `POST /check` HTTP API (`serve`) | other agents, chat-ops bots |
| Check plugins (`--plugins dir`) | org-specific rules, see `examples/plugins/` |
| GitHub Action, Dockerfile | every 30 min and after every deploy, keeps the last report so it can diff |

## 7. Gaps / next steps

- **JavaScript:** no headless browser, a deliberate cost/speed trade-off. A good next step is an optional Playwright checker that runs only on `client_rendered` pages and key journeys (console errors, Core Web Vitals, screenshots).
- **Single location:** checks run from one place, with no multi-region uptime.
- **Triage latency:** the Claude call takes 14-21s, longer than the scan itself. It runs once per changed result set, so scheduled runs are mostly cache hits.
