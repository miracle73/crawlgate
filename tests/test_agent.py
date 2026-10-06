"""The agent proposes; code validates; the gate decides. No network: a fake client stands in for the API."""

import ast
import re
import json
from pathlib import Path

import pytest
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
    """Mimics openai.OpenAI().chat.completions.create as OpenRouter returns it."""

    def __init__(self, content: str | Proposal = "", *, model="anthropic/claude-sonnet-4.5", refusal=None,
                 finish="stop", exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        if isinstance(content, Proposal):
            content = content.model_dump_json()

        def create(**kw):
            self.calls.append(kw)
            if exc:
                raise exc
            msg = SimpleNamespace(content=content, refusal=refusal)
            return SimpleNamespace(model=model, choices=[SimpleNamespace(message=msg, finish_reason=finish)])

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _edit(**kw) -> Edit:
    d = dict(path="site/about.html", old_string='<meta name="robots" content="noindex">', new_string="",
             rule="noindex_production_route", url="https://x.test/about.html", rationale="remove leaked noindex",
             also_fixes=["noindex_in_sitemap"])
    d.update(kw)
    return Edit(**d)


def test_locates_source_and_applies_valid_edit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    client = FakeClient(Proposal(edits=[_edit()], skipped=[]))
    res, files = agent.propose(_report(), repo, client=client)
    assert [f.relative_to(repo).as_posix() for f in files] == ["site/about.html"]
    call = client.calls[0]
    assert call["model"] == "anthropic/claude-sonnet-4.5"
    assert "fallbacks" not in call and "models" not in call  # no model swap of any kind
    assert "crawlgate.baseline.json" not in json.dumps(call["messages"])  # never shown to the model
    ok, bad = agent.validate(res.proposal, repo, files)
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


@pytest.mark.parametrize("client, msg", [
    (FakeClient(refusal="I can't help with that"), "refused"),
    (FakeClient("{}", finish="length"), "finish_reason='length'"),
    (FakeClient('{"edits": [{"path": "x"}], "skipped": []}'), "does not match the Proposal schema"),
    (FakeClient("not json at all"), "does not match the Proposal schema"),
    (FakeClient(exc=TimeoutError("read timeout")), "model call failed: TimeoutError"),
])
def test_failures_are_loud_and_apply_nothing(tmp_path: Path, client, msg: str) -> None:
    repo = _repo(tmp_path)
    before = (repo / "site/about.html").read_text(encoding="utf-8")
    with pytest.raises(agent.ProposeError, match=re.escape(msg)):
        agent.propose(_report(), repo, client=client)
    assert len(client.calls) == 1  # one attempt, no retry on another model
    assert (repo / "site/about.html").read_text(encoding="utf-8") == before


def test_pr_body_records_requested_and_answering_model(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    client = FakeClient(Proposal(edits=[_edit()], skipped=[]), model="anthropic/claude-sonnet-4.5-20250929")
    res, files = agent.propose(_report(), repo, client=client, model="anthropic/claude-sonnet-4.5")
    ok, bad = agent.validate(res.proposal, repo, files)
    body = agent.pr_body(res, ok, bad)
    assert "requested `anthropic/claude-sonnet-4.5`, answered by `anthropic/claude-sonnet-4.5-20250929`" in body
    assert "| `noindex_production_route`, `noindex_in_sitemap` |" in body and "Not fixed" not in body


def test_gate_never_imports_agent() -> None:
    for mod in ("checks", "gate", "severity", "report", "crawler", "models", "extract", "config"):
        tree = ast.parse((SRC / f"{mod}.py").read_text(encoding="utf-8"))
        names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        assert not any("agent" in n for n in names), mod
