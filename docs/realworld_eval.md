# Real-world generalisation eval

**24/24 sites finished without crashing** | verified precision **103/105 = 98%** (independent re-check of sampled findings) | known-answer expectations **9/9**

| site | category | verdict | score | time | pages / URLs | coverage | findings | verified | expectation |
|---|---|---|---|---|---|---|---|---|---|
| aintropy | AI startup / Cloudflare | DEGRADED | 86 | 18.8s | 15 / 89 | 100% | 8 | 8/8 | - |
| twelvelabs | video-AI company / Framer | DEGRADED | 95 | 7.6s | 14 / 238 | 97% | 2 | 2/2 | - |
| synechron | large enterprise / Next.js + Imperva | DEGRADED | 95 | 32.6s | 13 / 234 | 47% | 2 | 2/2 | - |
| 3kglobal | AI company / Cloudflare (user pick) | DEGRADED | 81 | 10.4s | 14 / 74 | 100% | 15 | 8/8 | - |
| gydeon | startup / Vercel Next.js, www->apex (user pick) | DEGRADED | 91 | 4.2s | 3 / 27 | 100% | 6 | 4/4 | - |
| mizuho | global bank / Cloudflare (user pick) | HEALTHY | 96 | 14.0s | 15 / 240 | 51% | 4 | 4/4 | - |
| example | minimal single page | DEGRADED | 81 | 1.5s | 1 / 2 | 100% | 10 | 7/7 | pass |
| books | catalogue built for crawlers | DEGRADED | 66 | 48.4s | 9 / 243 | 81% | 25 | 5/5 | pass |
| badssl-expired | known-bad: expired cert | DOWN | 41 | 9.6s | 1 / 4 | 100% | 11 | 1/1 | pass |
| badssl-selfsigned | known-bad: self-signed | DOWN | 41 | 13.2s | 1 / 4 | 100% | 11 | 1/1 | pass |
| badssl-wronghost | known-bad: hostname mismatch | DOWN | 41 | 9.8s | 1 / 4 | 100% | 11 | 1/1 | pass |
| badssl-http | known-bad: served over http | DEGRADED | 81 | 6.7s | 1 / 4 | 100% | 10 | 6/6 | pass |
| neverssl | http-only by design | UNHEALTHY | 51 | 77.2s | 1 / 3 | 100% | 13 | 5/7 | pass |
| the-internet | known-bad: broken images | DEGRADED | 42 | 21.2s | 13 / 84 | 100% | 57 | 6/6 | pass |
| dns-fail | known-bad: DNS failure | DOWN | 20 | 1.7s | 0 / 1 | 100% | 2 | 1/1 | pass |
| python-docs | docs site, sub-path scope | UNHEALTHY | 81 | 5.7s | 8 / 240 | 50% | 7 | 6/6 | - |
| react | JS framework docs (Next.js) | DEGRADED | 86 | 5.3s | 15 / 243 | 71% | 20 | 7/7 | - |
| wikipedia | huge multilingual portal | DEGRADED | 92 | 8.3s | 1 / 26 | 100% | 5 | 3/3 | - |
| gov-uk | government, very large | HEALTHY | 96 | 14.9s | 15 / 238 | 24% | 7 | 7/7 | - |
| lemonde | non-English news | DEGRADED | 89 | 20.3s | 14 / 232 | 15% | 5 | 3/3 | - |
| allbirds | e-commerce (Shopify) | DEGRADED | 75 | 16.9s | 13 / 234 | 36% | 27 | 7/7 | - |
| wordpress-news | WordPress blog, sub-path | DEGRADED | 91 | 66.4s | 8 / 125 | 57% | 10 | 8/8 | - |
| g2 | bot-protected marketplace | UNVERIFIABLE | 100 | 1.7s | 0 / 1 | 100% | 0 | - | - |
| httpstat-503 | known-bad: homepage 503 | DOWN | 60 | 21.1s | 0 / 1 | 100% | 1 | 1/1 | - |

## Findings the verifier refuted

- neverssl: `no_https` http://neverssl.com/
- neverssl: `slow_response` http://neverssl.com/

## How sites were handled (profiler notes)

- **badssl-expired**: certificate invalid - assessed over an unverified connection
- **badssl-selfsigned**: certificate invalid - assessed over an unverified connection
- **badssl-wronghost**: certificate invalid - assessed over an unverified connection
- **g2**: automated requests are challenged by Cloudflare
