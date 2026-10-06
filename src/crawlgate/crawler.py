"""Deterministic BFS crawler: bounded worker pool, token bucket, robots.txt, retries with backoff."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

import httpx

from .config import Config
from .extract import extract, hydration_diff, normalize_url
from .models import Hop, OgImage, Page, SiteData

log = logging.getLogger("crawlgate.crawler")
RETRY_STATUS = {429, 500, 502, 503, 504}


class TokenBucket:
    def __init__(self, rate: float, burst: int | None = None) -> None:
        self.rate = max(rate, 0.01)
        self.cap = float(burst or max(1, int(rate)))
        self.tokens = self.cap
        self.t = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class Fetched:
    def __init__(self, chain: list[Hop], resp: httpx.Response | None, error: str | None = None) -> None:
        self.chain, self.resp, self.error = chain, resp, error

    @property
    def status(self) -> int:
        return self.resp.status_code if self.resp is not None else 0

    @property
    def final_url(self) -> str:
        return str(self.resp.url) if self.resp is not None else (self.chain[-1].url if self.chain else "")


class Crawler:
    def __init__(self, base_url: str, cfg: Config) -> None:
        self.base = normalize_url(base_url)
        self.host = urlsplit(self.base).netloc
        self.cfg = cfg
        self.bucket = TokenBucket(cfg.rate_per_sec)
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.robots: RobotFileParser | None = None
        self.inc = [re.compile(p) for p in cfg.include]
        self.exc = [re.compile(p) for p in cfg.exclude]
        self.client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=cfg.timeout_s,
            headers={"User-Agent": cfg.user_agent, "Accept-Language": "*"},
        )

    # -- http ---------------------------------------------------------------
    async def _get(self, url: str) -> httpx.Response:
        delay = 0.5
        for attempt in range(self.cfg.retries + 1):
            await self.bucket.take()
            try:
                r = await self.client.get(url)
                if r.status_code not in RETRY_STATUS or attempt == self.cfg.retries:
                    return r
                log.info("retrying", extra={"url": url, "status": r.status_code, "attempt": attempt + 1})
            except httpx.TransportError as e:
                if attempt == self.cfg.retries:
                    raise
                log.info("retrying", extra={"url": url, "error": type(e).__name__, "attempt": attempt + 1})
            await asyncio.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    async def fetch(self, url: str, max_hops: int = 10) -> Fetched:
        chain: list[Hop] = []
        cur = url
        async with self.sem:
            for _ in range(max_hops + 1):
                try:
                    r = await self._get(cur)
                except httpx.HTTPError as e:
                    return Fetched(chain, None, f"{type(e).__name__}")
                if r.is_redirect and "location" in r.headers:
                    chain.append(Hop(url=cur, status=r.status_code))
                    cur = urljoin(cur, r.headers["location"])
                    continue
                return Fetched(chain, r)
        return Fetched(chain, None, "too many redirects")

    # -- policy -------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        if urlsplit(url).netloc != self.host:
            return False
        if self.inc and not any(p.search(url) for p in self.inc):
            return False
        if any(p.search(url) for p in self.exc):
            return False
        if self.cfg.respect_robots and self.robots and not self.robots.can_fetch(self.cfg.user_agent, url):
            log.info("robots disallow", extra={"url": url})
            return False
        return True

    # -- site files ---------------------------------------------------------
    async def load_robots(self, site: SiteData) -> None:
        u = urljoin(self.base, "/robots.txt")
        f = await self.fetch(u)
        if f.status == 200:
            site.robots_txt_found = True
            rp = RobotFileParser()
            text = f.resp.text
            rp.parse(text.splitlines())
            self.robots = rp
            site.robots_blocks_all = not rp.can_fetch("*", self.base) or not rp.can_fetch(self.cfg.user_agent, self.base)
            site.robots_sitemaps = sorted({
                normalize_url(line.split(":", 1)[1].strip())
                for line in text.splitlines()
                if line.lower().startswith("sitemap:")
            })

    async def load_sitemaps(self, site: SiteData) -> None:
        queue = list(site.robots_sitemaps) or [urljoin(self.base, "/sitemap.xml")]
        seen: set[str] = set()
        urls: set[str] = set()
        while queue and len(seen) < 50:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            f = await self.fetch(sm)
            if f.status != 200:
                log.info("sitemap unavailable", extra={"url": sm, "status": f.status})
                continue
            try:
                root = ET.parse(io.BytesIO(f.resp.content)).getroot()
            except ET.ParseError as e:
                log.warning("sitemap parse error", extra={"url": sm, "error": str(e)})
                continue
            locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
            if root.tag.endswith("sitemapindex"):
                queue += sorted(locs)
            else:
                urls.update(normalize_url(l) for l in locs)
        site.sitemap_urls = sorted(urls)
        if self.robots:
            site.robots_disallowed_sitemap_urls = [u for u in site.sitemap_urls if not self.robots.can_fetch("*", u)]

    # -- pages --------------------------------------------------------------
    def _page(self, url: str, depth: int, f: Fetched) -> Page:
        p = Page(url=url, depth=depth, status=f.status, final_url=normalize_url(f.final_url) if f.final_url else url,
                 redirect_chain=f.chain)
        if f.resp is None:
            return p
        p.content_type = f.resp.headers.get("content-type", "").split(";")[0].strip().lower() or None
        p.x_robots_tag = f.resp.headers.get("x-robots-tag")
        if p.content_type == "text/html" and f.status == 200:
            p.surface, p.links = extract(f.resp.text, str(f.resp.url))
        return p

    async def crawl(self) -> SiteData:
        site = SiteData(base_url=self.base)
        await self.load_robots(site)
        await self.load_sitemaps(site)
        pages: dict[str, Page] = {}
        level = [self.base]
        for depth in range(self.cfg.max_depth + 2):
            pending = sorted(set(u for u in level if u not in pages))
            if not pending:
                site.crawl_complete = True
                break
            if depth > self.cfg.max_depth or len(pages) >= self.cfg.max_pages:
                break
            level = pending[: self.cfg.max_pages - len(pages)]
            log.info("crawl level", extra={"depth": depth, "urls": len(level)})
            results = await asyncio.gather(*(self.fetch(u) for u in level))
            nxt: set[str] = set()
            for u, f in zip(level, results):
                pages[u] = self._page(u, depth, f)
                for link in pages[u].links:
                    if link not in pages and self.allowed(link):
                        nxt.add(link)
            if self.cfg.render:
                await self._render([pages[u] for u in level if pages[u].surface], nxt, pages)
            level = sorted(nxt)

        # Verification pass: fetch referenced URLs we didn't expand, in priority tiers so a budget
        # never starves the checks that need them. Tier 0: hreflang/canonical targets. Tier 1: links
        # from crawled pages. Tier 2: sitemap URLs. Sorted within each tier; truncation is reported.
        tiers: list[set[str]] = [set(), set(), set(site.sitemap_urls)]
        for p in pages.values():
            if p.surface:
                tiers[0].update(p.surface.hreflang.values())
                if p.surface.canonical:
                    tiers[0].add(normalize_url(p.surface.canonical))
            tiers[1].update(u for u in p.links if urlsplit(u).netloc == self.host)
        todo: list[str] = []
        for tier in tiers:
            todo += sorted(u for u in tier if u not in pages and u not in todo and urlsplit(u).scheme in ("http", "https"))
        site.verify_skipped = todo[self.cfg.max_verify:]
        todo = todo[: self.cfg.max_verify]
        log.info("verify pass", extra={"urls": len(todo), "skipped": len(site.verify_skipped)})
        for u, f in zip(todo, await asyncio.gather(*(self.fetch(u) for u in todo))):
            p = self._page(u, -1, f)
            p.links = []
            pages[u] = p

        await self._og_images(pages)
        site.pages = [pages[k] for k in sorted(pages)]
        await self.client.aclose()
        return site

    async def _render(self, todo: list[Page], nxt: set[str], pages: dict[str, Page]) -> None:
        if not todo:
            return
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(user_agent=self.cfg.user_agent)

            async def one(p: Page) -> None:
                async with self.sem:
                    await self.bucket.take()
                    tab = await ctx.new_page()
                    try:
                        await tab.goto(p.url, wait_until="load", timeout=self.cfg.timeout_s * 1000)
                        try:  # best effort: analytics beacons keep many sites from ever going idle
                            await tab.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:  # noqa: BLE001
                            pass
                        html = await tab.content()
                    except Exception as e:  # noqa: BLE001 - render failure is non-fatal
                        log.warning("render failed", extra={"url": p.url, "error": type(e).__name__})
                        return
                    finally:
                        await tab.close()
                rendered, links = extract(html, p.final_url)
                p.hydration_only = hydration_diff(p.surface, rendered)
                extra = sorted(set(links) - set(p.links))
                p.links = sorted(set(p.links) | set(extra))
                for link in extra:
                    if link not in pages and self.allowed(link):
                        nxt.add(link)

            await asyncio.gather(*(one(p) for p in todo))
            await browser.close()

    async def _og_images(self, pages: dict[str, Page]) -> None:
        targets: dict[str, list[Page]] = {}
        for p in pages.values():
            img = p.surface.og.get("og:image") if p.surface else None
            if img:
                absolute = urlsplit(img).scheme in ("http", "https")
                p.og_image = OgImage(
                    url=img, absolute=absolute,
                    meta_width=p.surface.og.get("og:image:width"),
                    meta_height=p.surface.og.get("og:image:height"),
                    meta_alt=p.surface.og.get("og:image:alt"),
                )
                targets.setdefault(img if absolute else urljoin(p.final_url, img), []).append(p)
        from PIL import Image

        async def probe(url: str) -> None:
            f = await self.fetch(url)
            ctype = f.resp.headers.get("content-type", "").split(";")[0].strip().lower() if f.resp else None
            w = h = None
            if f.status == 200 and ctype and ctype.startswith("image/"):
                try:
                    w, h = Image.open(io.BytesIO(f.resp.content)).size
                except Exception:  # noqa: BLE001 - svg/corrupt image
                    pass
            for p in targets[url]:
                p.og_image.status, p.og_image.content_type, p.og_image.width, p.og_image.height = f.status, ctype, w, h

        await asyncio.gather(*(probe(u) for u in sorted(targets)))


async def crawl(base_url: str, cfg: Config) -> SiteData:
    return await Crawler(base_url, cfg).crawl()
