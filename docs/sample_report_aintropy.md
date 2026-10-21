# Health report: https://aintropy.ai/

**Verdict: DEGRADED** | score 85/100 | 27.11s | 17 pages, 110 URLs, 126 requests | triage: claude ($0.0084)

Counts: medium=2, low=7, info=3

## Summary

The site is missing several standard security headers (HSTS, CSP, frame protection) on the homepage, which is the highest-priority fix and likely a server or CDN config gap. Separately, a few low-severity accessibility and SEO gaps appear on specific pages: missing h1 headings, missing meta descriptions, and images without alt text. The missing security.txt is a low-effort add.

## Root causes (fix in this order)

1. **Security headers not set on homepage response** - Add the three headers at the edge or server layer (for example, in the CDN response-header policy or the web server config): Strict-Transport-Security with max-age of at least one year, a Content-Security-Policy that starts with default-src 'self', and frame-ancestors 'none' (plus X-Frame-Options: DENY for older browsers). Verify with curl -I after deploy.  
   _The web server, CDN, or hosting layer does not emit Strict-Transport-Security, Content-Security-Policy, or X-Frame-Options/frame-ancestors, so none of the responses carry these protections._ (https://aintropy.ai/, https://aintropy.ai/, https://aintropy.ai/)
2. **Missing h1 on solution and vision pages** - Make sure each of these pages has exactly one h1 that holds the page title, either by changing the template or by adding the heading to the page content.  
   _These page templates or their content blocks render the title as a non-h1 element (or omit it), which often happens when a theme or generated content replaces the main heading._ (https://aintropy.ai/solutions/nj_demo, https://aintropy.ai/vision/unik-results-blog-2/)
3. **Missing meta descriptions on solution pages** - Add a unique 120 to 160 character meta description to each page, or set a template default that is populated from the page summary.  
   _The SEO meta description field is empty for these pages, probably because the CMS or template does not fill it in for solutions content._ (https://aintropy.ai/solutions/seattle_legal/, https://aintropy.ai/solutions/nj_opendata/)
4. **Images without alt text in the UNIK research post** - Add descriptive alt text to each image in the post. Use alt="" only for purely decorative images. Those images are under /content-pulled/research_papers/, so fix them at the source so the import stops producing them.  
   _The 13 images were pulled into the content from a source that did not carry alt text, so the converted HTML has no alt attributes._ (https://aintropy.ai/research/unik-universal-knowledge-perception/)
5. **No security.txt published** - Create /.well-known/security.txt with a Contact line (email or URL), an Expires date within a year, and optionally Policy and Canonical lines. Serve it as plain text.  
   _The site has no /.well-known/security.txt file._ (https://aintropy.ai/.well-known/security.txt)

## Since last run

previous verdict: None | new: 12 | resolved: 0 | persisting: 0

## Endpoints discovered

| method | url | found in | result |
|---|---|---|---|
| POST | https://aintropy.ai/solutions/seattle_legal/api/search | js | not called (POST - inventoried only, never called) |
| POST | https://aintropy.ai/solutions/seattle_legal/api/feedback | js | not called (POST - inventoried only, never called) |
| POST | https://aintropy.ai/solutions/seattle_legal/api/compare | js | not called (POST - inventoried only, never called) |
| GET | https://aintropy.ai/solutions/seattle_legal/api/seed-questions | js | 200 |
| GET | https://aintropy.ai/solutions/seattle_legal/api/gate/status | js | 200 |
| POST | https://aintropy.ai/solutions/seattle_legal/api/signup | js | not called (POST - inventoried only, never called) |
| GET | https://aintropy.ai/solutions/nj_opendata/api/seed-questions | js | 200 |
| POST | https://aintropy.ai/solutions/nj_opendata/api/search | js | not called (POST - inventoried only, never called) |
| GET | https://aintropy.ai/solutions/nj_opendata/api/gate/status | js | 200 |
| POST | https://aintropy.ai/solutions/nj_opendata/api/signup | js | not called (POST - inventoried only, never called) |
| POST | https://aintropy.ai/solutions/nj_opendata/api/compare | js | not called (POST - inventoried only, never called) |
| POST | https://aintropy.ai/solutions/nj_opendata/api/feedback | js | not called (POST - inventoried only, never called) |

## All findings

| severity | code | url | detail |
|---|---|---|---|
| medium | missing_csp | https://aintropy.ai/ | Content-Security-Policy header missing (no XSS mitigation) |
| medium | missing_hsts | https://aintropy.ai/ | Strict-Transport-Security header missing (allows SSL-strip downgrade) |
| low | img_missing_alt | https://aintropy.ai/research/unik-universal-knowledge-perception/ | 13 image(s) without alt text - https://aintropy.ai/content-pulled/research_papers/2026-09-22-unik-universal-knowledge-perception/media/image9.gif, https://aintr |
| low | missing_frame_protection | https://aintropy.ai/ | No X-Frame-Options or CSP frame-ancestors (clickjacking) |
| low | missing_h1 | https://aintropy.ai/solutions/nj_demo | Page has no <h1> |
| low | missing_h1 | https://aintropy.ai/vision/unik-results-blog-2/ | Page has no <h1> |
| low | missing_meta_description | https://aintropy.ai/solutions/nj_opendata/ | No meta description |
| low | missing_meta_description | https://aintropy.ai/solutions/seattle_legal/ | No meta description |
| low | missing_security_txt | https://aintropy.ai/.well-known/security.txt | No security.txt - researchers have no way to report vulnerabilities (RFC 9116) |
| info | client_rendered | https://aintropy.ai/solutions/nj_opendata/ | Content is rendered by JavaScript - static crawl sees an empty shell (its JS bundles are still checked by the links checker) |
| info | client_rendered | https://aintropy.ai/solutions/seattle_legal/ | Content is rendered by JavaScript - static crawl sees an empty shell (its JS bundles are still checked by the links checker) |
| info | long_title | https://aintropy.ai/research/unik-universal-knowledge-perception/ | Title is 75 chars |
