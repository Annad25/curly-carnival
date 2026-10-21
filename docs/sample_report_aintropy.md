# Health report: https://aintropy.ai/

**Verdict: DEGRADED** | score 86/100 | 34.33s | 17 pages, 110 URLs, 119 requests | triage: claude ($0.0038)

Counts: medium=2, low=6, info=3

## Summary

Four security headers are missing site-wide (HSTS, CSP, frame protection), which points to the web server or CDN config and is the top fix. The remaining findings are low severity: missing alt text on 13 images in one UNIK research post, and missing h1 and meta descriptions on a few solutions and blog pages, which look like template or content-pipeline gaps. No finding was excluded as a false positive.

## Root causes (fix in this order)

1. **Security headers not set on the site** - Add the three headers at the edge (CDN or reverse proxy) for all responses: Strict-Transport-Security with max-age of at least 15552000, a CSP starting with default-src 'self' (test in Report-Only first), and frame-ancestors 'none' with X-Frame-Options DENY.  
   _The origin server, reverse proxy, or CDN does not add Strict-Transport-Security, Content-Security-Policy, or X-Frame-Options/frame-ancestors to responses, so none of these headers are sent on the homepage._ (https://aintropy.ai/, https://aintropy.ai/, https://aintropy.ai/)
2. **Images without alt text in the UNIK research post** - Add alt text to the 13 images in the post, or have the import step fill alt from captions or figure titles, and add a check to the pipeline so new imports fail without alt.  
   _The content import pipeline (content-pulled/research_papers) copies images from the source papers without carrying over or generating alt attributes._ (https://aintropy.ai/research/unik-universal-knowledge-perception/)
3. **Missing h1 on solutions and blog pages** - Confirm the rendered DOM on each page, then make the page title an h1 in the template, keeping one h1 per page.  
   _These pages probably use a layout or template where the title is rendered as a non-h1 element, such as a div, a logo image, or text injected by JavaScript._ (https://aintropy.ai/solutions/nj_demo, https://aintropy.ai/vision/unik-results-blog-2/)
4. **Missing meta descriptions on solutions pages** - Add a description field to the solutions template and fill it for seattle_legal and nj_opendata, then check the other solutions pages for the same gap.  
   _The solutions page template does not output a meta description, or the per-page field is empty in the CMS._ (https://aintropy.ai/solutions/seattle_legal/, https://aintropy.ai/solutions/nj_opendata/)

## Since last run

previous verdict: None | new: 11 | resolved: 0 | persisting: 0

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
| info | client_rendered | https://aintropy.ai/solutions/nj_opendata/ | Content is rendered by JavaScript - static crawl sees an empty shell (its JS bundles are still checked by the links checker) |
| info | client_rendered | https://aintropy.ai/solutions/seattle_legal/ | Content is rendered by JavaScript - static crawl sees an empty shell (its JS bundles are still checked by the links checker) |
| info | long_title | https://aintropy.ai/research/unik-universal-knowledge-perception/ | Title is 75 chars |
