# Problem 2.4: Website Health Checker

A fast, cheap agent that tests whether a website is healthy: availability, links and assets, TLS/DNS, security headers, content errors, SEO, performance, and the API endpoints the site's JavaScript calls. It uses Claude (`claude -p`) only to reason over the findings: grouping them into root causes and suggesting fixes.

**Write-up (design, evals, guardrails, results):** [docs/README.md](docs/README.md)  
**How it was built, step by step:** [docs/how_i_built_it.md](docs/how_i_built_it.md)

## Quick start

```bash
pip install -r requirements.txt
python src/main.py demo                                  # localhost site with 21 planted faults
python src/main.py check --url https://aintropy.ai/      # real site
python src/main.py check --config sites.yaml             # every site in the config
python src/main.py check --url example.com               # any input form works (scheme optional)
python src/evals/run_eval.py                             # precision / recall / guardrails / cost
python src/evals/realworld.py                            # 24 live sites + independent verifier (~5 min)
python -m pytest -q
```

Optional: `export CLAUDE_KEY=...` and `npm i -g @anthropic-ai/claude-code` turn on LLM triage. Without them, triage is rule-based.

## Layout

```
src/main.py                 CLI (check | demo | serve)
src/healthchecker/
  profile.py                site profiler: identity, TLS, WAF/login/soft-404 detection, robots rules
  crawler.py                planner/crawler agent (sitemaps, section-aware crawl, trap protection, tiers)
  endpoints.py              API endpoint discovery from JS bundles + safety rules
  fetcher.py                read-only async HTTP: budgets, adaptive concurrency, Retry-After, Crawl-delay
  checks/                   checker agents (one file each) + plugin loader
  triage.py                 reasoning layer: claude -p -> cache -> rules fallback
  guardrails.py             prompt-injection / secret sanitising, output validation
  report.py, hooks.py       verdict, diff, JSON/MD reports, webhook + command hooks
  api.py                    authenticated POST /check API with a host allowlist
src/fixture_site/           localhost test websites: 21 planted faults + 10 edge-case modes
src/evals/run_eval.py       eval harness (planted faults, guardrails, cost)
src/evals/realworld.py      generalisation eval on live sites with an independent verifier
examples/plugins/           example org-specific check
sites.yaml                  targets + settings
Dockerfile, .github/workflows/health-check.yml   deployment
```
