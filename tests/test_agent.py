"""The agent proposes; code validates; the gate decides. No network: a fake client stands in for the API."""

import ast
from pathlib import Path
from types import SimpleNamespace

from crawlgate import agent
from crawlgate.agent import Edit, Proposal
from crawlgate.models import Finding, Page, Report, SeoSurface, Severity, SiteData

SRC = Path(agent.__file__).parent


def _report() -> Report:
    s = SeoSurface(title="About the demo shop", meta_robots="noindex", canonical="https://x.test/about.html")
    page = Page(url="https://x.test/about.html", depth=1, status=200, final_url="https://x.test/about.html", surface=s)
    f = Finding(rule="noindex_production_route", severity=Severity.BLOCK, url=page.url, message="page is noindex")
    return Report(base_url="https://x.test/", site=SiteData(base_url="https://x.test/", pages=[page]),
                  findings=[f], verdict="fail")


def _repo(tmp: Path) -> Path:
    (tmp / "site").mkdir()
    (tmp / "site/about.html").write_text(
        '<head><meta name="robots" content="noindex"><title>About the demo shop</title></head>', encoding="utf-8")
    (tmp / "crawlgate.baseline.json").write_text("{}", encoding="utf-8")
    return tmp


class FakeClient:
    def __init__(self, proposal: Proposal) -> None:
        self.calls: list[dict] = []
        parse = lambda **kw: self.calls.append(kw) or SimpleNamespace(stop_reason="end_turn", parsed_output=proposal)
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=parse))


def _edit(**kw) -> Edit:
    d = dict(path="site/about.html", old_string='<meta name="robots" content="noindex">', new_string="",
             rule="noindex_production_route", url="https://x.test/about.html", rationale="remove leaked noindex")
    d.update(kw)
    return Edit(**d)


def test_locates_source_and_applies_valid_edit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    client = FakeClient(Proposal(edits=[_edit()], skipped=[]))
    prop, files = agent.propose(_report(), repo, client=client)
    assert [f.relative_to(repo).as_posix() for f in files] == ["site/about.html"]
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert "crawlgate.baseline.json" not in client.calls[0]["messages"][0]["content"]  # never shown to the model
    ok, bad = agent.validate(prop, repo, files)
    assert len(ok) == 1 and not bad
    agent.apply(ok, repo)
    assert "noindex" not in (repo / "site/about.html").read_text(encoding="utf-8")


def test_validation_rejects_protected_unknown_ambiguous_and_unfixable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    files = [repo / "site/about.html"]
    prop = Proposal(edits=[
        _edit(path="crawlgate.baseline.json", old_string="{}", new_string='{"findings": []}'),
        _edit(path=".github/workflows/seo.yml"),
        _edit(path="site/other.html"),
        _edit(old_string="<"),                      # ambiguous: many matches
        _edit(rule="og_image_missing"),             # needs an asset, not a text edit
    ], skipped=[])
    ok, bad = agent.validate(prop, repo, files)
    assert not ok
    assert [b.why.split(" (")[0].split(",")[0] for b in bad] == [
        "protected path", "protected path", "file was not provided to the model",
        "old_string matches 5 times", "rule og_image_missing is not agent-fixable"]


def test_refusal_yields_no_edits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    client = FakeClient(Proposal(edits=[], skipped=[]))
    client.beta.messages.parse = lambda **kw: SimpleNamespace(stop_reason="refusal", parsed_output=None)
    prop, _ = agent.propose(_report(), repo, client=client)
    assert prop.edits == []


def test_gate_never_imports_agent() -> None:
    for mod in ("checks", "gate", "severity", "report", "crawler", "models", "extract", "config"):
        tree = ast.parse((SRC / f"{mod}.py").read_text(encoding="utf-8"))
        names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        assert not any("agent" in n for n in names), mod
