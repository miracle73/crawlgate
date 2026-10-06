import logging

from conftest import build

from crawlgate import gate, report as rep, severity
from crawlgate.models import Severity


def test_config_cannot_downgrade_block(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="crawlgate"):
        t = severity.resolve({"noindex_in_sitemap": "info", "og_image_missing": "block"})
    assert t["noindex_in_sitemap"] == Severity.BLOCK
    assert t["og_image_missing"] == Severity.BLOCK
    assert any("downgrade ignored" in r.getMessage() for r in caplog.records)
    assert severity.RULES["og_image_missing"] == Severity.WARN  # floor table is immutable


def test_report_is_byte_stable(site_url, cfg) -> None:
    a = rep.to_json(gate.apply(build(site_url, cfg), None, "block"))
    b = rep.to_json(gate.apply(build(site_url, cfg), None, "block"))
    assert a == b


def test_gate_fails_on_block(report) -> None:
    assert gate.apply(report, None, "block").verdict == "fail"


def test_baseline_suppresses_known_warn_but_never_block(site_url, cfg) -> None:
    base = gate.apply(build(site_url, cfg), None, "none")
    cur = build(site_url, cfg)
    cur.findings += gate.diff(cur.site, base.site, severity.resolve({}))
    r = gate.apply(cur, base, "warn")
    assert r.counts["WARN"] == 0 and r.counts["known"] > 0
    assert r.counts["BLOCK"] > 0 and r.verdict == "fail"  # BLOCKs survive baselining


def test_diff_detects_status_regression(site_url, cfg) -> None:
    base = build(site_url, cfg)
    cur = build(site_url, cfg)
    page = next(p for p in cur.site.pages if p.url.endswith("/no-og"))
    page.status = 404
    page.surface.canonical = "https://staging.example.com/no-og"
    found = {f.rule: f for f in gate.diff(cur.site, base.site, severity.resolve({}))}
    assert found["status_regressed"].severity == Severity.BLOCK
    assert found["changed_canonical"].detail.endswith("'https://staging.example.com/no-og'")
