# Problem 2.4: Website Health Checker

A fast, cheap agent that tests whether a website is healthy: availability, links and assets, TLS/DNS, security headers, content errors, SEO, performance, and the API endpoints the site's JavaScript calls. It uses Claude (`claude -p`) only to reason over the findings: grouping them into root causes and suggesting fixes.

**Write-up (design, evals, guardrails, results):** [docs/README.md](docs/README.md)

## Quick start

```bash
pip install -r requirements.txt
python src/main.py demo                                  # localhost site with 21 planted faults
python src/main.py check --url https://aintropy.ai/      # real site
python src/main.py check --config sites.yaml             # every site in the config
python src/evals/run_eval.py                             # precision / recall / guardrails / cost
python -m pytest -q
```

Optional: `export CLAUDE_KEY=...` and `npm i -g @anthropic-ai/claude-code` turn on LLM triage. Without them, triage is rule-based.

## Layout

```
src/main.py                 CLI (check | demo | serve)
src/healthchecker/
  crawler.py                planner/crawler agent (robots, sitemap, budgeted BFS)
  endpoints.py              API endpoint discovery from JS bundles + safety rules
  fetcher.py                read-only async HTTP with budgets + retry
  checks/                   checker agents (one file each) + plugin loader
  triage.py                 reasoning layer: claude -p -> cache -> rules fallback
  guardrails.py             prompt-injection / secret sanitising, output validation
  report.py, hooks.py       verdict, diff, JSON/MD reports, webhook + command hooks
  api.py                    authenticated POST /check API with a host allowlist
src/fixture_site/           localhost test website with ground-truth faults
src/evals/run_eval.py       eval harness
examples/plugins/           example org-specific check
sites.yaml                  targets + settings
Dockerfile, .github/workflows/health-check.yml   deployment
```
