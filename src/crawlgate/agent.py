"""Optional second phase: draft a fix for a failing report as a patch / pull request.

Kept deliberately outside the gate. Nothing in checks/gate/severity imports this module,
the verdict is never read back from it, and it cannot touch the baseline, config or CI.
The agent proposes; code validates every edit; people review the PR; the gate re-runs on it.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .models import Report, Severity

log = logging.getLogger("crawlgate.agent")
MODEL = "claude-opus-5-5"

# Rules a text edit to page source can plausibly fix. og:image needs an asset, so it's excluded.
FIXABLE = frozenset({
    "noindex_production_route", "noindex_in_sitemap", "canonical_foreign_host", "canonical_to_noindex",
    "canonical_missing", "canonical_multiple", "title_missing", "title_length", "description_missing",
    "description_length", "og_tag_missing", "og_image_relative", "og_image_meta_incomplete",
    "twitter_card_missing", "jsonld_invalid_json", "jsonld_missing_type", "jsonld_missing_required",
})
# The agent may never edit what decides the verdict.
PROTECTED = re.compile(r"(^|/)(\.github/|\.crawlgate/|crawlgate\.toml$|[^/]*baseline[^/]*\.json$|action\.ya?ml$)")
SOURCE_EXT = {".html", ".htm", ".jsx", ".tsx", ".js", ".ts", ".vue", ".svelte", ".astro", ".njk", ".liquid",
              ".hbs", ".ejs", ".erb", ".php", ".py", ".md", ".mdx", ".json", ".yaml", ".yml", ".toml"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", ".next", "__pycache__", ".crawlgate"}
MAX_FILE_BYTES = 60_000
MAX_FILES = 12

SYSTEM = """You fix SEO regressions reported by crawlgate, a CI gate, by proposing minimal source edits.

Each edit replaces `old_string` with `new_string` in one of the provided files. `old_string` must be copied
exactly from that file and occur in it exactly once. Change only what the finding requires; keep formatting.
Never weaken a check: don't remove pages from sitemaps or hide content to make a finding disappear, and don't
invent facts (prices, dates, authors) for JSON-LD. If a field needs information you don't have, skip it.
If a finding can't be fixed safely from these files (for example a noindex that looks intentional), add it to
`skipped` with a one-line reason instead of guessing. A reviewer will read every edit."""


class Edit(BaseModel):
    path: str = Field(description="Repo-relative path, exactly as given")
    old_string: str
    new_string: str
    rule: str
    url: str
    rationale: str = Field(description="One sentence for the PR description")


class Skip(BaseModel):
    rule: str
    url: str
    reason: str


class Proposal(BaseModel):
    edits: list[Edit]
    skipped: list[Skip]


class Rejected(BaseModel):
    edit: Edit
    why: str


def fixable(r: Report) -> list:
    return [f for f in r.findings if f.rule in FIXABLE and (not f.known or f.severity == Severity.BLOCK)]


def _files(repo: Path) -> list[Path]:
    out = []
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo).as_posix()
        if (p.is_file() and p.suffix.lower() in SOURCE_EXT and p.stat().st_size <= MAX_FILE_BYTES
                and not SKIP_DIRS & set(p.relative_to(repo).parts) and not PROTECTED.search(rel)):
            out.append(p)
    return out


def locate(r: Report, findings: list, repo: Path) -> list[Path]:
    """Rank repo files by how many of the failing pages' distinctive strings they contain."""
    pages = {p.url: p for p in r.site.pages}
    needles: set[str] = set()
    for f in findings:
        p = pages.get(f.url)
        path = urlsplit(f.url).path.strip("/")
        if path:
            needles.add(path.rsplit("/", 1)[-1])
        if p and p.surface:
            s = p.surface
            needles.update(v for v in (s.title, s.canonical, s.meta_description, s.og.get("og:url")) if v and len(v) > 8)
    scored = []
    for fp in _files(repo):
        text = fp.read_text(encoding="utf-8", errors="ignore")
        score = sum(1 for n in needles if n in text) + (2 if any(n and n in fp.as_posix() for n in needles) else 0)
        if score:
            scored.append((-score, fp.relative_to(repo).as_posix(), fp))
    return [fp for _, _, fp in sorted(scored)[:MAX_FILES]]


def _prompt(r: Report, findings: list, files: list[Path], repo: Path) -> str:
    pages = {p.url: p for p in r.site.pages}
    lines = ["<findings>"]
    for f in findings:
        lines.append(f"- [{f.severity.name}] {f.rule} on {f.url}: {f.message} {f.detail}".rstrip())
        p = pages.get(f.url)
        if p and p.surface:
            lines.append(f"  current surface: {p.surface.model_dump_json(exclude={'jsonld'})}")
    lines.append("</findings>")
    for fp in files:
        rel = fp.relative_to(repo).as_posix()
        lines.append(f'<file path="{rel}">\n{fp.read_text(encoding="utf-8", errors="ignore")}\n</file>')
    return "\n".join(lines)


def propose(r: Report, repo: Path, client=None, model: str = MODEL) -> tuple[Proposal, list[Path]]:
    findings = fixable(r)
    files = locate(r, findings, repo)
    if not findings or not files:
        return Proposal(edits=[], skipped=[]), files
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    resp = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": _prompt(r, findings, files, repo)}],
        output_format=Proposal,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        log.warning("no proposal", extra={"stop_reason": resp.stop_reason})
        return Proposal(edits=[], skipped=[]), files
    return resp.parsed_output, files


def validate(p: Proposal, repo: Path, allowed: list[Path]) -> tuple[list[Edit], list[Rejected]]:
    """Code decides which proposed edits are even eligible. The gate decides if they worked."""
    allowed_rel = {fp.relative_to(repo).as_posix() for fp in allowed}
    ok, bad = [], []
    pending: dict[str, str] = {}
    for e in p.edits:
        why = None
        if PROTECTED.search(e.path):
            why = "protected path (baseline/config/CI)"
        elif e.path not in allowed_rel:
            why = "file was not provided to the model"
        elif e.rule not in FIXABLE:
            why = f"rule {e.rule} is not agent-fixable"
        else:
            text = pending.get(e.path) or (repo / e.path).read_text(encoding="utf-8")
            n = text.count(e.old_string) if e.old_string else 0
            if n != 1:
                why = f"old_string matches {n} times, need exactly 1"
            elif e.old_string == e.new_string:
                why = "no-op edit"
            else:
                pending[e.path] = text.replace(e.old_string, e.new_string, 1)
        (bad.append(Rejected(edit=e, why=why)) if why else ok.append(e))
    return ok, bad


def apply(edits: list[Edit], repo: Path) -> list[str]:
    touched = []
    for e in edits:
        fp = repo / e.path
        text = fp.read_text(encoding="utf-8")
        fp.write_text(text.replace(e.old_string, e.new_string, 1), encoding="utf-8", newline="")
        touched.append(e.path)
    return sorted(set(touched))


def pr_body(r: Report, edits: list[Edit], rejected: list[Rejected], skipped: list[Skip]) -> str:
    out = ["Drafted by `crawlgate propose` from a failing crawlgate report. **Review every line.**",
           "This PR does not change the gate's verdict; the crawlgate check re-runs on it and decides.", "",
           "| rule | url | change |", "|---|---|---|"]
    out += [f"| `{e.rule}` | {e.url} | {e.rationale} |" for e in edits]
    if skipped or rejected:
        out += ["", "**Not fixed:**", ""]
        out += [f"- `{s.rule}` {s.url}: {s.reason}" for s in skipped]
        out += [f"- `{x.edit.rule}` {x.edit.url}: edit rejected by validation ({x.why})" for x in rejected]
    return "\n".join(out) + "\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def open_pr(repo: Path, touched: list[str], body: str, base: str | None) -> str:
    """Commit to a fresh crawlgate/* branch and open a draft PR. Never pushes to the base branch."""
    base = base or _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = f"crawlgate/fix-{_git(repo, 'rev-parse', '--short', 'HEAD')}"
    if branch == base or not branch.startswith("crawlgate/"):
        raise RuntimeError("refusing to commit to the base branch")
    _git(repo, "checkout", "-b", branch)
    _git(repo, "add", "--", *touched)
    _git(repo, "commit", "-m", "Fix SEO regressions flagged by crawlgate")
    _git(repo, "push", "-u", "origin", branch)
    return subprocess.run(
        ["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
         "--title", "Fix SEO regressions flagged by crawlgate", "--body", body],
        cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
