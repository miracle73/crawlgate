"""Hard-coded severity rules. This is the floor. Config may raise a severity, never lower one.

A prompt is a request, a check is a guarantee: nothing outside this module decides
how bad a rule is, and nothing can make a BLOCK quieter.
"""

from __future__ import annotations

import logging
from types import MappingProxyType

from .models import Severity

B, W, I = Severity.BLOCK, Severity.WARN, Severity.INFO

RULES: MappingProxyType[str, Severity] = MappingProxyType({
    # indexability
    "noindex_production_route": B,
    "noindex_in_sitemap": B,
    "canonical_foreign_host": B,
    "canonical_to_noindex": B,
    "canonical_missing": W,
    "canonical_multiple": W,
    "robots_blocks_all": B,
    # status / sitemap
    "sitemap_url_error": B,          # sitemap URL returns 404 or 5xx
    "page_5xx": B,
    "status_regressed": B,           # baseline 2xx -> now 4xx/5xx
    "status_changed": W,
    "page_removed": W,
    "page_added": I,
    "internal_link_broken": W,
    "redirect_chain_long": W,
    "internal_link_redirect": I,
    "orphan_page": W,                # in sitemap, not reachable via links
    "sitemap_missing": W,
    "sitemap_url_disallowed": W,
    "robots_txt_missing": I,
    "verify_truncated": W,           # checks silently incomplete is worse than slow
    # hreflang
    "hreflang_dead_target": B,
    "hreflang_not_reciprocal": W,
    "hreflang_incomplete": W,
    "hreflang_no_self": W,
    # metadata
    "title_missing": W,
    "title_length": I,
    "description_missing": W,
    "description_length": W,
    "seo_only_after_hydration": W,
    # social
    "og_image_missing": W,
    "og_image_relative": W,
    "og_image_unreachable": W,
    "og_image_bad_type": W,
    "og_image_too_small": W,
    "og_image_meta_incomplete": I,
    "og_tag_missing": I,
    "twitter_card_missing": I,
    "twitter_image_missing": I,
    # structured data
    "jsonld_invalid_json": W,
    "jsonld_missing_type": W,
    "jsonld_missing_required": W,
    "jsonld_unknown_type": I,
    # surface diffs vs baseline
    "changed_canonical": W,
    "changed_robots": W,
    "changed_title": I,
    "changed_meta_description": I,
    "changed_og": I,
    "changed_twitter": I,
    "changed_hreflang": W,
    "changed_jsonld": I,
    "changed_final_url": W,
    # performance
    "cwv_over_budget": W,
    "lighthouse_failed": I,
})

log = logging.getLogger("crawlgate.severity")


def resolve(overrides: dict[str, str] | None = None) -> dict[str, Severity]:
    """Apply config overrides on top of RULES. Upgrades only; downgrades are ignored and logged."""
    table = dict(RULES)
    for rule, raw in sorted((overrides or {}).items()):
        if rule not in RULES:
            log.warning("unknown rule in severity override", extra={"rule": rule})
            continue
        try:
            wanted = Severity[raw.upper()]
        except KeyError:
            log.warning("invalid severity in override", extra={"rule": rule, "value": raw})
            continue
        floor = RULES[rule]
        if wanted < floor:
            log.warning(
                "severity downgrade ignored",
                extra={"rule": rule, "requested": wanted.name, "kept": floor.name},
            )
            continue
        table[rule] = wanted
    return table
