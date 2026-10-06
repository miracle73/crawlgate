"""HTML -> SeoSurface. Pure functions, no I/O."""

from __future__ import annotations

import json
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import JsonLdBlock, SeoSurface

# Minimal required-property table for common schema.org types.
JSONLD_REQUIRED: dict[str, list[str]] = {
    "Organization": ["name", "url"],
    "WebSite": ["name", "url"],
    "WebPage": ["name"],
    "Article": ["headline", "author", "datePublished"],
    "NewsArticle": ["headline", "author", "datePublished"],
    "BlogPosting": ["headline", "author", "datePublished"],
    "Product": ["name", "offers"],
    "Offer": ["price", "priceCurrency"],
    "BreadcrumbList": ["itemListElement"],
    "ListItem": ["position"],
    "FAQPage": ["mainEntity"],
    "Question": ["name", "acceptedAnswer"],
    "Event": ["name", "startDate", "location"],
    "SportsEvent": ["name", "startDate", "location"],
    "SportsTeam": ["name"],
    "Person": ["name"],
    "LocalBusiness": ["name", "address"],
    "ImageObject": ["url"],
    "SoftwareApplication": ["name"],
    "WebApplication": ["name"],
    "MobileApplication": ["name"],
    "VideoObject": ["name", "thumbnailUrl", "uploadDate"],
    "Review": ["itemReviewed", "author"],
    "AggregateRating": ["ratingValue"],
    "SearchAction": ["target"],
    "SiteNavigationElement": [],
    "ItemList": ["itemListElement"],
}


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}" + (f"?{parts.query}" if parts.query else "")


def _text(v: str | None) -> str | None:
    if v is None:
        return None
    v = " ".join(v.split())
    return v or None


def _jsonld(raw: str) -> JsonLdBlock:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return JsonLdBlock(valid_json=False, error=f"{e.msg} at line {e.lineno}")
    nodes: list[dict] = []

    def walk(o: object) -> None:
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            if "@type" in o:
                nodes.append(o)
            for k, v in o.items():
                if k != "@context":
                    walk(v)

    walk(data)
    if isinstance(data, dict) and "@graph" not in data and "@type" not in data:
        return JsonLdBlock(valid_json=True, error="no @type")
    types: list[str] = []
    missing: dict[str, list[str]] = {}
    for n in nodes:
        t = n["@type"]
        for name in t if isinstance(t, list) else [t]:
            name = str(name).removeprefix("https://schema.org/").removeprefix("http://schema.org/")
            types.append(name)
            req = JSONLD_REQUIRED.get(name)
            if req:
                gone = [p for p in req if p not in n or n[p] in (None, "", [])]
                if gone:
                    missing.setdefault(name, [])
                    missing[name] = sorted(set(missing[name]) | set(gone))
    return JsonLdBlock(valid_json=True, types=sorted(set(types)), missing_properties=missing)


def extract(html: str, page_url: str) -> tuple[SeoSurface, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    s = SeoSurface()
    if soup.title and soup.title.string is not None:
        s.title = _text(soup.title.string)
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").strip().lower()
        prop = (m.get("property") or "").strip().lower()
        content = m.get("content")
        if content is None:
            continue
        content = " ".join(content.split())
        if name == "description":
            s.meta_description = content or None
        elif name == "robots":
            s.meta_robots = content.lower()
        key = prop or name
        if key.startswith("og:") and key not in s.og:
            s.og[key] = content
        elif key.startswith("twitter:") and key not in s.twitter:
            s.twitter[key] = content
    canon = [l for l in soup.find_all("link", href=True) if "canonical" in [r.lower() for r in (l.get("rel") or [])]]
    s.canonical_count = len(canon)
    if canon:
        s.canonical = urljoin(page_url, canon[0]["href"].strip())
    for l in soup.find_all("link", href=True, hreflang=True):
        if "alternate" in [r.lower() for r in (l.get("rel") or [])]:
            s.hreflang.setdefault(l["hreflang"].strip().lower(), normalize_url(urljoin(page_url, l["href"])))
    for sc in soup.find_all("script", type=lambda t: t and t.lower().strip() == "application/ld+json"):
        s.jsonld.append(_jsonld(sc.string or sc.get_text() or ""))
    links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        u = urljoin(page_url, href)
        if urlsplit(u).scheme in ("http", "https"):
            links.add(normalize_url(u))
    s.hreflang = dict(sorted(s.hreflang.items()))
    s.og = dict(sorted(s.og.items()))
    s.twitter = dict(sorted(s.twitter.items()))
    return s, sorted(links)


def hydration_diff(raw: SeoSurface, rendered: SeoSurface) -> list[str]:
    """SEO fields that exist only after JS runs. Non-JS crawlers (WhatsApp, Facebook, many bots) never see them."""
    out: list[str] = []
    for f in ("title", "meta_description", "canonical", "meta_robots"):
        if getattr(rendered, f) and not getattr(raw, f):
            out.append(f)
    for group in ("og", "twitter", "hreflang"):
        r, h = getattr(raw, group), getattr(rendered, group)
        out += [f"{group}:{k}" if group == "hreflang" else k for k in sorted(set(h) - set(r))]
    if len(rendered.jsonld) > len(raw.jsonld):
        out.append("jsonld")
    return out
