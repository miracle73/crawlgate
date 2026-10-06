"""Typed SEO surface. Everything serialized here must be deterministic: no timings, no timestamps."""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, Field


class Severity(IntEnum):
    INFO = 1
    WARN = 2
    BLOCK = 3

    def __str__(self) -> str:
        return self.name


class Hop(BaseModel):
    url: str
    status: int


class OgImage(BaseModel):
    url: str
    absolute: bool
    status: int | None = None
    content_type: str | None = None
    width: int | None = None
    height: int | None = None
    meta_width: str | None = None
    meta_height: str | None = None
    meta_alt: str | None = None


class JsonLdBlock(BaseModel):
    valid_json: bool
    types: list[str] = Field(default_factory=list)
    missing_properties: dict[str, list[str]] = Field(default_factory=dict)
    error: str | None = None


class SeoSurface(BaseModel):
    title: str | None = None
    meta_description: str | None = None
    canonical: str | None = None
    canonical_count: int = 0
    meta_robots: str | None = None
    hreflang: dict[str, str] = Field(default_factory=dict)
    og: dict[str, str] = Field(default_factory=dict)
    twitter: dict[str, str] = Field(default_factory=dict)
    jsonld: list[JsonLdBlock] = Field(default_factory=list)


class Page(BaseModel):
    url: str
    depth: int
    status: int
    final_url: str
    redirect_chain: list[Hop] = Field(default_factory=list)
    content_type: str | None = None
    x_robots_tag: str | None = None
    surface: SeoSurface | None = None
    hydration_only: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    og_image: OgImage | None = None


class Finding(BaseModel):
    rule: str
    severity: Severity
    url: str
    message: str
    detail: str = ""
    known: bool = False  # present in baseline

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.url, self.detail)

    def sort_key(self) -> tuple:
        return (-int(self.severity), self.rule, self.url, self.detail)


class SiteData(BaseModel):
    base_url: str
    robots_txt_found: bool = False
    robots_sitemaps: list[str] = Field(default_factory=list)
    robots_blocks_all: bool = False
    robots_disallowed_sitemap_urls: list[str] = Field(default_factory=list)
    sitemap_urls: list[str] = Field(default_factory=list)
    verify_skipped: list[str] = Field(default_factory=list)  # referenced but not fetched (budget)
    crawl_complete: bool = False  # frontier exhausted (not cut by depth/max_pages)
    pages: list[Page] = Field(default_factory=list)


class Report(BaseModel):
    tool: str = "crawlgate"
    schema_version: int = 1
    base_url: str
    verdict: str = "pass"
    counts: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    site: SiteData
