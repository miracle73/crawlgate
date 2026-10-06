"""Checks: SiteData -> findings. Severity comes only from the resolved severity table."""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from .config import Config
from .extract import JSONLD_REQUIRED, normalize_url
from .models import Finding, Page, Severity, SiteData

Raw = tuple[str, str, str, str]  # rule, url, message, detail
OG_REQUIRED = ("og:title", "og:description", "og:url", "og:image")


def _host(u: str) -> str:
    return urlsplit(u).netloc.lower()


def noindex(p: Page, cfg: Config) -> bool:
    meta = p.surface.meta_robots if p.surface else None
    return "noindex" in " ".join(filter(None, [None if cfg.ignore_x_robots_tag else p.x_robots_tag, meta])).lower()


def page_checks(p: Page, site: SiteData, cfg: Config, prod_hosts: set[str], sitemap: set[str]) -> Iterator[Raw]:
    u = p.url
    if _host(u) not in prod_hosts | {_host(site.base_url)}:
        return  # third-party target: judged by the rule that referenced it (canonical/hreflang), not here
    if p.status >= 500 or p.status == 0:
        yield "page_5xx", u, f"page returned {p.status or 'no response'}", str(p.status)
    hops = len(p.redirect_chain)
    if hops > 1:
        chain = " -> ".join(f"{h.url} [{h.status}]" for h in p.redirect_chain) + f" -> {p.final_url} [{p.status}]"
        yield "redirect_chain_long", u, f"redirect chain of {hops} hops", chain
    s = p.surface
    if s is None or p.depth < 0:
        return  # verification-only fetch: status checks only

    robots = ", ".join(dict.fromkeys(filter(None, [None if cfg.ignore_x_robots_tag else p.x_robots_tag, s.meta_robots]))).lower()
    if "noindex" in robots and not any(re.search(rx, u) for rx in cfg.noindex_allowed):
        yield "noindex_production_route", u, "page is noindex", robots

    if not s.title:
        yield "title_missing", u, "missing <title>", ""
    elif not 10 <= len(s.title) <= 60:
        yield "title_length", u, f"title is {len(s.title)} chars (10-60)", ""
    if not s.meta_description:
        yield "description_missing", u, "missing meta description", ""
    elif not 50 <= len(s.meta_description) <= 160:
        yield "description_length", u, f"meta description is {len(s.meta_description)} chars (50-160)", ""

    if s.canonical_count == 0:
        yield "canonical_missing", u, "no rel=canonical", ""
    elif s.canonical_count > 1:
        yield "canonical_multiple", u, f"{s.canonical_count} rel=canonical tags", ""
    if s.canonical and _host(s.canonical) not in prod_hosts:
        yield "canonical_foreign_host", u, "canonical points at non-production host", s.canonical

    for f in p.hydration_only:
        yield "seo_only_after_hydration", u, f"`{f}` only exists after JavaScript runs", f

    # social
    for tag in OG_REQUIRED:
        if tag not in s.og:
            rule = "og_image_missing" if tag == "og:image" else "og_tag_missing"
            yield rule, u, f"missing {tag}", tag
    if "twitter:card" not in s.twitter:
        yield "twitter_card_missing", u, "missing twitter:card", ""
    if "twitter:image" not in s.twitter and "og:image" not in s.og:
        yield "twitter_image_missing", u, "missing twitter:image (and no og:image fallback)", ""
    img = p.og_image
    if img:
        if not img.absolute:
            yield "og_image_relative", u, "og:image is relative; Facebook/WhatsApp will not resolve it", img.url
        if img.status != 200:
            yield "og_image_unreachable", u, f"og:image returned {img.status}", img.url
        elif not (img.content_type or "").startswith("image/"):
            yield "og_image_bad_type", u, f"og:image content-type {img.content_type}", img.url
        elif img.width is not None and (img.width < 1200 or img.height < 630):
            yield "og_image_too_small", u, f"og:image is {img.width}x{img.height}, need >=1200x630", img.url
        gone = [k for k, v in (("og:image:width", img.meta_width), ("og:image:height", img.meta_height),
                               ("og:image:alt", img.meta_alt)) if not v]
        if gone:
            yield "og_image_meta_incomplete", u, "missing " + ", ".join(gone), ",".join(gone)

    # structured data
    for i, b in enumerate(s.jsonld):
        if not b.valid_json:
            yield "jsonld_invalid_json", u, f"JSON-LD block {i} does not parse: {b.error}", str(i)
            continue
        if not b.types:
            yield "jsonld_missing_type", u, f"JSON-LD block {i} has no @type", str(i)
        for t in b.types:
            if t not in JSONLD_REQUIRED:
                yield "jsonld_unknown_type", u, f"JSON-LD type {t} not validated", t
        for t, props in sorted(b.missing_properties.items()):
            yield "jsonld_missing_required", u, f"{t} missing required: {', '.join(props)}", f"{t}:{','.join(props)}"


def site_checks(site: SiteData, cfg: Config, prod_hosts: set[str]) -> Iterator[Raw]:
    by_url = {p.url: p for p in site.pages}
    sitemap = set(site.sitemap_urls)
    base = site.base_url

    if not site.robots_txt_found:
        yield "robots_txt_missing", base, "robots.txt not found", ""
    if not sitemap:
        yield "sitemap_missing", base, "no sitemap.xml found (robots.txt Sitemap: or /sitemap.xml)", ""

    if site.verify_skipped:
        n = len(site.verify_skipped)
        yield "verify_truncated", base, f"{n} referenced URLs not checked (raise max_verify)", str(n)
    if site.robots_blocks_all:
        yield "robots_blocks_all", base, "robots.txt disallows the base URL for all crawlers", ""
    for u in site.robots_disallowed_sitemap_urls:
        yield "sitemap_url_disallowed", u, "URL in sitemap.xml is disallowed by robots.txt", ""

    for p in site.pages:
        yield from page_checks(p, site, cfg, prod_hosts, sitemap)

    # sitemap vs crawl
    linked: set[str] = set()
    for p in site.pages:
        if p.depth >= 0:
            linked.add(p.url)
            linked.update(p.links)
    for u in sorted(sitemap):
        p = by_url.get(u)
        if p and (p.status == 404 or p.status == 410 or p.status >= 500 or p.status == 0):
            yield "sitemap_url_error", u, f"URL in sitemap.xml returns {p.status}", str(p.status)
        if p and noindex(p, cfg):
            yield "noindex_in_sitemap", u, "noindex page listed in sitemap.xml", ""
        if site.crawl_complete and u not in linked and _host(u) == _host(base):
            yield "orphan_page", u, "in sitemap.xml but not linked from any crawled page", ""

    # internal links
    for p in site.pages:
        for link in p.links:
            t = by_url.get(link)
            if t is None or _host(link) != _host(base):
                continue
            if t.status in (404, 410):
                yield "internal_link_broken", p.url, f"links to {t.status}", link
            elif t.redirect_chain:
                yield "internal_link_redirect", p.url, f"links to a redirect ({len(t.redirect_chain)} hops)", link

    # canonical targets
    for p in site.pages:
        if p.depth < 0 or not p.surface or not p.surface.canonical:
            continue
        c = normalize_url(p.surface.canonical)
        t = by_url.get(c)
        if t and c != p.url and noindex(t, cfg):
            yield "canonical_to_noindex", p.url, "canonical target is noindex", c

    # hreflang: dead targets, reciprocity, completeness, self-reference
    clusters: dict[str, dict[str, str]] = {p.url: p.surface.hreflang for p in site.pages
                                           if p.depth >= 0 and p.surface and p.surface.hreflang}
    for u, hl in sorted(clusters.items()):
        if u not in hl.values():
            yield "hreflang_no_self", u, "hreflang set does not include the page itself", ""
        for lang, target in hl.items():
            t = by_url.get(target)
            if t is not None and (t.status >= 400 or t.status == 0):
                yield "hreflang_dead_target", u, f"hreflang `{lang}` -> {t.status}", f"{lang} {target}"
                continue
            if t is not None and t.surface is not None and target != u:
                back = t.surface.hreflang
                if u not in back.values():
                    yield "hreflang_not_reciprocal", u, f"`{lang}` target does not link back", f"{lang} {target}"
                    continue
                missing = sorted(set(hl) - set(back))  # same cluster, different locale sets
                if missing:
                    yield "hreflang_incomplete", target, f"hreflang set lacks {', '.join(missing)} listed by {u}", ",".join(missing)


def run(site: SiteData, cfg: Config, table: dict[str, Severity]) -> list[Finding]:
    prod = {h.lower() for h in cfg.production_hosts} or {_host(site.base_url)}
    seen: set[tuple[str, str, str]] = set()
    out: list[Finding] = []
    for rule, url, msg, detail in site_checks(site, cfg, prod):
        if (rule, url, detail) in seen:
            continue
        seen.add((rule, url, detail))
        out.append(Finding(rule=rule, severity=table[rule], url=url, message=msg, detail=detail))
    return out
