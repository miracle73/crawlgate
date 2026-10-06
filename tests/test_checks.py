from crawlgate.extract import normalize_url
from crawlgate.models import Report, Severity


def rules(r: Report, path: str) -> set[str]:
    return {f.rule for f in r.findings if normalize_url(f.url) == normalize_url(r.base_url.rstrip("/") + path)}


def test_known_good_page_is_clean(report: Report) -> None:
    assert rules(report, "/good") == set()
    assert rules(report, "/de/good") == set()
    good = next(p for p in report.site.pages if p.url.endswith("/good") and "/de/" not in p.url)
    assert good.og_image and (good.og_image.width, good.og_image.height) == (1200, 630)
    assert good.surface.jsonld[0].types == ["WebSite"]


def test_missing_og_image(report: Report) -> None:
    f = next(f for f in report.findings if f.rule == "og_image_missing")
    assert f.url.endswith("/no-og") and f.severity == Severity.WARN
    assert "twitter_image_missing" in rules(report, "/no-og")


def test_noindex_in_sitemap_blocks(report: Report) -> None:
    got = rules(report, "/noindex")
    assert {"noindex_in_sitemap", "noindex_production_route"} <= got
    assert all(f.severity == Severity.BLOCK for f in report.findings if f.rule == "noindex_in_sitemap")


def test_sitemap_404_blocks(report: Report) -> None:
    errs = {f.url.rsplit("/", 1)[1]: f for f in report.findings if f.rule == "sitemap_url_error"}
    assert set(errs) == {"gone", "old"} and all(f.severity == Severity.BLOCK for f in errs.values())


def test_broken_hreflang(report: Report) -> None:
    got = {(f.rule, f.detail.split()[0]) for f in report.findings if f.url.endswith("/hreflang-broken")}
    assert ("hreflang_dead_target", "fr") in got
    assert ("hreflang_not_reciprocal", "de") in got
    dead = next(f for f in report.findings if f.rule == "hreflang_dead_target")
    assert dead.severity == Severity.BLOCK


def test_redirect_chain(report: Report) -> None:
    f = next(f for f in report.findings if f.rule == "redirect_chain_long")
    assert f.url.endswith("/redirect") and f.severity == Severity.WARN
    assert f.message == "redirect chain of 3 hops"
    page = next(p for p in report.site.pages if p.url.endswith("/redirect"))
    assert page.final_url.endswith("/good") and page.status == 200
    assert any(f.rule == "internal_link_redirect" for f in report.findings)


def test_unreachable_foreign_canonical_is_not_a_5xx() -> None:
    # Regression (found by the Action demo): a staging canonical that doesn't resolve was also
    # reported as page_5xx BLOCK on the third-party host.
    from crawlgate import checks, severity
    from crawlgate.config import Config
    from crawlgate.models import Page, SeoSurface, SiteData

    surface = SeoSurface(title="Pricing page title", canonical="https://staging.example.com/p", canonical_count=1)
    site = SiteData(base_url="http://site.test/", pages=[
        Page(url="http://site.test/p", depth=0, status=200, final_url="http://site.test/p", surface=surface),
        Page(url="https://staging.example.com/p", depth=-1, status=0, final_url="https://staging.example.com/p"),
    ])
    rules = {f.rule for f in checks.run(site, Config(), severity.resolve({}))}
    assert "canonical_foreign_host" in rules and "page_5xx" not in rules
