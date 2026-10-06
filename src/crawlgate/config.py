"""Run configuration. Loaded from crawlgate.toml. Cannot lower severities (see severity.resolve)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class CwvBudget(BaseModel):
    enabled: bool = False
    urls: int = 3  # audit the first N pages (deterministic order)
    lcp_ms: float = 2500
    cls: float = 0.1
    tbt_ms: float = 200


class Config(BaseModel):
    max_depth: int = 3
    max_pages: int = 500
    max_verify: int = 5000  # referenced-but-not-expanded URLs to status-check
    concurrency: int = 4
    rate_per_sec: float = 5.0
    timeout_s: float = 20.0
    retries: int = 3
    user_agent: str = "crawlgate/0.1 (+https://github.com/crawlgate)"
    include: list[str] = Field(default_factory=list)  # regexes on URL
    exclude: list[str] = Field(default_factory=list)
    render: bool = True
    respect_robots: bool = True
    production_hosts: list[str] = Field(default_factory=list)  # default: host of base URL
    noindex_allowed: list[str] = Field(default_factory=list)  # regexes of routes allowed to be noindex
    ignore_x_robots_tag: bool = False  # preview deployments often send X-Robots-Tag: noindex
    severity: dict[str, str] = Field(default_factory=dict)  # upgrades only
    lighthouse: CwvBudget = Field(default_factory=CwvBudget)


def load(path: str | Path | None) -> Config:
    if path is None:
        p = Path("crawlgate.toml")
        if not p.exists():
            return Config()
    else:
        p = Path(path)
    with p.open("rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data.get("crawlgate", data))
