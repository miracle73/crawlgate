"""Baseline diff + gate verdict.

Rules:
- Findings already present in the baseline are `known`. Known WARN/INFO do not gate.
- BLOCK findings always gate, baseline or not. You cannot baseline away a guarantee.
- URLs are compared by path relative to the base URL, so a baseline taken from
  production can gate a preview deployment on another host.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .models import Finding, Page, Report, Severity, SiteData

log = logging.getLogger("crawlgate.gate")
FAIL_ON = {"block": Severity.BLOCK, "warn": Severity.WARN, "info": Severity.INFO}


def rel(url: str, base: str) -> str:
    u, b = urlsplit(url), urlsplit(base)
    if u.netloc.lower() == b.netloc.lower():
        return (u.path or "/") + (f"?{u.query}" if u.query else "")
    return url


def _fkey(f: Finding, base: str) -> tuple[str, str, str]:
    return (f.rule, rel(f.url, base), f.detail)


def _crawled(site: SiteData) -> dict[str, Page]:
    return {rel(p.url, site.base_url): p for p in site.pages if p.depth >= 0}


def diff(current: SiteData, baseline: SiteData, table: dict[str, Severity]) -> list[Finding]:
    cur, old = _crawled(current), _crawled(baseline)
    out: list[Finding] = []

    def add(rule: str, path: str, msg: str, detail: str = "") -> None:
        url = cur[path].url if path in cur else (path if "://" in path else current.base_url.rstrip("/") + path)
        out.append(Finding(rule=rule, severity=table[rule], url=url, message=msg, detail=detail))

    for path in sorted(set(old) - set(cur)):
        add("page_removed", path, "page in baseline is no longer reachable", path)
    for path in sorted(set(cur) - set(old)):
        add("page_added", path, "new page", path)
    for path in sorted(set(cur) & set(old)):
        a, b = old[path], cur[path]
        if a.status != b.status:
            regressed = 200 <= a.status < 300 and (b.status >= 400 or b.status == 0)
            add("status_regressed" if regressed else "status_changed", path,
                f"status {a.status} -> {b.status}", f"{a.status}->{b.status}")
        if rel(a.final_url, baseline.base_url) != rel(b.final_url, current.base_url):
            add("changed_final_url", path, "final URL changed", f"{a.final_url} -> {b.final_url}")
        sa, sb = a.surface, b.surface
        if not sa or not sb:
            continue
        for field in ("canonical", "meta_robots", "title", "meta_description"):
            va, vb = getattr(sa, field), getattr(sb, field)
            if va != vb:
                rule = {"meta_robots": "changed_robots"}.get(field, f"changed_{field}")
                add(rule, path, f"{field} changed", f"{va!r} -> {vb!r}")
        for group in ("og", "twitter"):
            ga, gb = getattr(sa, group), getattr(sb, group)
            for k in sorted(set(ga) | set(gb)):
                if ga.get(k) != gb.get(k):
                    add(f"changed_{group}", path, f"{k} changed", f"{k}: {ga.get(k)!r} -> {gb.get(k)!r}")
        ha = {k: rel(v, baseline.base_url) for k, v in sa.hreflang.items()}
        hb = {k: rel(v, current.base_url) for k, v in sb.hreflang.items()}
        if ha != hb:
            add("changed_hreflang", path, "hreflang set changed",
                f"-{sorted(set(ha.items()) - set(hb.items()))} +{sorted(set(hb.items()) - set(ha.items()))}")
        ja = sorted(t for blk in sa.jsonld for t in blk.types)
        jb = sorted(t for blk in sb.jsonld for t in blk.types)
        if ja != jb:
            add("changed_jsonld", path, "JSON-LD types changed", f"{ja} -> {jb}")
    return out


def apply(report: Report, baseline: Report | None, fail_on: str) -> Report:
    if baseline is not None:
        known = {_fkey(f, baseline.base_url) for f in baseline.findings}
        for f in report.findings:
            f.known = _fkey(f, report.base_url) in known
    report.findings.sort(key=Finding.sort_key)
    counts = {s.name: 0 for s in sorted(Severity, reverse=True)}
    gating = [f for f in report.findings if not f.known or f.severity == Severity.BLOCK]
    for f in gating:
        counts[f.severity.name] += 1
    counts["known"] = sum(1 for f in report.findings if f.known and f.severity != Severity.BLOCK)
    report.counts = counts
    threshold = FAIL_ON.get(fail_on)
    worst = max((f.severity for f in gating), default=None)
    if threshold is not None and worst is not None and worst >= threshold:
        report.verdict = "fail"
    elif worst is not None and worst >= Severity.WARN:
        report.verdict = "warn"
    else:
        report.verdict = "pass"
    log.info("gate", extra={"verdict": report.verdict, "fail_on": fail_on, **counts})
    return report
