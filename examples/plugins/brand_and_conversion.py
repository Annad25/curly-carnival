"""Example org-specific plugin: business rules a generic checker can't know.

Drop any file like this into a plugins dir (`--plugins dir` or `plugins_dir:` in sites.yaml).
"""
from healthchecker.checks import check
from healthchecker.models import Finding


@check("brand_and_conversion")
async def brand_and_conversion(site, cfg):
    out = []
    home = next((p for p in site.pages if p.url == site.start_url), None)
    if home is None:
        return out
    # The homepage must keep a path to contact sales / demo, or the site is "up" but not doing its job.
    contact = [l for l in home.links if any(k in l.lower() for k in ("contact", "demo", "join", "email-protection"))]
    if not contact and "contact" not in home.text.lower():
        out.append(Finding("brand_and_conversion", "no_contact_path", "high", home.url,
                           "Homepage has no contact / demo call-to-action"))
    # Social preview must exist - it's what people see when the link is shared.
    og = site.resources.get(site.origin + "/og-image.png?v=2") or site.resources.get(site.origin + "/og-image.png")
    if og is not None and not og.ok:
        out.append(Finding("brand_and_conversion", "broken_og_image", "medium", og.url, "Social preview image is broken"))
    return out
