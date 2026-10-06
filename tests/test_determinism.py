"""Same input -> same page set -> same bytes. The headline claim, tested on the config path too."""

from pathlib import Path

from conftest import build

from crawlgate import gate, report as rep
from crawlgate.config import Config, load

EXAMPLE = Path(__file__).parent.parent / "crawlgate.example.toml"


def frontier(r) -> list[tuple[str, int, int]]:
    return [(p.url, p.depth, p.status) for p in r.site.pages]


def test_frontier_independent_of_concurrency(site_url: str) -> None:
    runs = [build(site_url, Config(render=False, rate_per_sec=200, retries=0, concurrency=c)) for c in (1, 8, 8)]
    assert frontier(runs[0]) == frontier(runs[1]) == frontier(runs[2])


def test_verify_budget_prioritises_hreflang_over_sitemap(site_url: str) -> None:
    # Tiny verify budget: hreflang/canonical targets must still be checked before sitemap filler,
    # and the truncation must be reported, not silent.
    # Unverified after depth 1: /en/old (sitemap), /fr/dead (hreflang), /gone (sitemap).
    # Alphabetical order would spend the single slot on /en/old.
    r = build(site_url, Config(render=False, rate_per_sec=200, retries=0, max_depth=1, max_verify=1))
    rules = {f.rule for f in r.findings}
    assert "hreflang_dead_target" in rules
    assert "sitemap_url_error" not in rules
    assert "verify_truncated" in rules


def test_example_config_full_pipeline_is_byte_stable(site_url: str) -> None:
    cfg = load(EXAMPLE)
    out = [rep.to_json(gate.apply(build(site_url, cfg), None, "block")) for _ in range(2)]
    assert out[0] == out[1]
