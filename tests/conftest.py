"""Local fixture site served over real HTTP so the crawler runs end to end."""

from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from crawlgate import checks, severity
from crawlgate.config import Config
from crawlgate.crawler import crawl
from crawlgate.models import Report

FIX = Path(__file__).parent / "fixtures"
PAGES = {"/": "index.html", "/good": "good.html", "/de/good": "de_good.html", "/no-og": "no_og.html",
         "/noindex": "noindex.html", "/hreflang-broken": "hreflang_broken.html"}
REDIRECTS = {"/redirect": "/r1", "/r1": "/r2", "/r2": "/good"}


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(scope="session")
def site_url() -> Iterator[str]:
    og = _png(1200, 630)
    state: dict[str, str] = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8", **hdr: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in hdr.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            p, base = self.path, state["base"]
            if p in REDIRECTS:
                return self._send(301, Location=REDIRECTS[p])
            if p == "/img/og.png":
                return self._send(200, og, "image/png")
            name = PAGES.get(p) or {"/sitemap.xml": "sitemap.xml", "/robots.txt": "robots.txt"}.get(p)
            if not name:
                return self._send(404, b"not found")
            ctype = {"xml": "application/xml", "txt": "text/plain"}.get(name.rsplit(".", 1)[1], "text/html; charset=utf-8")
            self._send(200, (FIX / name).read_text(encoding="utf-8").replace("{BASE}", base).encode(), ctype)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    state["base"] = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield state["base"]
    srv.shutdown()


@pytest.fixture
def cfg() -> Config:
    return Config(render=False, rate_per_sec=200, retries=0, noindex_allowed=[])


def build(url: str, cfg: Config) -> Report:
    table = severity.resolve(cfg.severity)
    site = asyncio.run(crawl(url, cfg))
    return Report(base_url=site.base_url, site=site, findings=checks.run(site, cfg, table))


@pytest.fixture
def report(site_url: str, cfg: Config) -> Report:
    return build(site_url, cfg)
