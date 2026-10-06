"""Optional Lighthouse Core Web Vitals budget. Shells out to the `lighthouse` CLI (npm i -g lighthouse).

Metric values are timing-dependent, so they are logged, never written to the report.
Only the over-budget verdict (metric + budget) lands in findings.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Iterator

from .config import CwvBudget

log = logging.getLogger("crawlgate.lighthouse")
METRICS = {"largest-contentful-paint": ("lcp_ms", "LCP", "ms"), "cumulative-layout-shift": ("cls", "CLS", ""),
           "total-blocking-time": ("tbt_ms", "TBT", "ms")}


def audit(urls: list[str], budget: CwvBudget) -> Iterator[tuple[str, str, str, str]]:
    exe = shutil.which("lighthouse") or shutil.which("lighthouse.cmd")
    if not exe:
        yield "lighthouse_failed", urls[0] if urls else "", "lighthouse CLI not found; CWV budget skipped", "not-installed"
        return
    for url in urls[: budget.urls]:
        try:
            out = subprocess.run(
                [exe, url, "--quiet", "--output=json", "--only-categories=performance",
                 "--chrome-flags=--headless=new"], capture_output=True, text=True, timeout=180, check=True).stdout
            audits = json.loads(out)["audits"]
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError) as e:
            yield "lighthouse_failed", url, "lighthouse run failed", type(e).__name__
            continue
        for key, (field, name, unit) in METRICS.items():
            val = audits.get(key, {}).get("numericValue")
            limit = getattr(budget, field)
            log.info("cwv", extra={"url": url, "metric": name, "value": val, "budget": limit})
            if val is not None and val > limit:
                yield "cwv_over_budget", url, f"{name} over budget ({limit}{unit})", name
