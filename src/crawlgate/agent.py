"""Optional second phase: draft a fix for a failing report as a patch / pull request.

Kept deliberately outside the gate. Nothing in checks/gate/severity imports this module,
the verdict is never read back from it, and it cannot touch the baseline, config or CI.
The agent proposes; code validates every edit; people review the PR; the gate re-runs on it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .models import Report, Severity

log = logging.getLogger("crawlgate.agent")
MODEL = "anthropic/claude-sonnet-4.5"
BASE_URL = "https://openrouter.ai/api/v1"

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
Every finding goes in exactly one place: as an edit's `rule`, in another edit's `also_fixes`, or in `skipped`.
`skipped` means genuinely not fixed. If a finding can't be fixed safely from these files (for example a noindex that looks intentional), add it to
`skipped` with a one-line reason instead of guessing. A reviewer will read every edit."""


class Edit(BaseModel):
    path: str = Field(description="Repo-relative path, exactly as given")
    old_string: str
    new_string: str
    rule: str
    url: str
    rationale: str = Field(description="One sentence for the PR description")
    also_fixes: list[str] = Field(description="Other finding rules on the same URL this edit resolves; [] if none")


class Skip(BaseModel):
    rule: str
    url: str
    reason: str


class Proposal(BaseModel):
    edits: list[Edit]
    skipped: list[Skip]


class ProposeError(RuntimeError):
    """The model call failed, was refused, or returned output that breaks the contract. Nothing is applied."""


class Result(BaseModel):
    proposal: Proposal
    model_requested: str
    model_used: str  # as reported by the provider; recorded in the PR body


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


def _strict(schema: dict) -> dict:
    """Pydantic schema -> strict JSON schema (no extra keys, everything required)."""
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties", {}))
    for v in list(schema.get("properties", {}).values()) + list(schema.get("$defs", {}).values()):
        _strict(v)
    if "items" in schema:
        _strict(schema["items"])
    return schema


def _client():
    from openai import OpenAI

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ProposeError("OPENROUTER_API_KEY is not set")
    return OpenAI(base_url=BASE_URL, api_key=key, max_retries=0, timeout=300)


def propose(r: Report, repo: Path, client=None, model: str = MODEL) -> tuple[Result, list[Path]]:
    """One model call. No fallback model, no retry on refusal: if it doesn't work, say so and stop."""
    findings = fixable(r)
    files = locate(r, findings, repo)
    empty = Result(proposal=Proposal(edits=[], skipped=[]), model_requested=model, model_used="(not called)")
    if not findings or not files:
        return empty, files
    client = client or _client()
    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=16000,
            temperature=0,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": _prompt(r, findings, files, repo)}],
            response_format={"type": "json_schema", "json_schema": {
                "name": "proposal", "strict": True, "schema": _strict(Proposal.model_json_schema())}},
        )
    except Exception as e:  # noqa: BLE001 - surface any transport/API error verbatim, then stop
        raise ProposeError(f"model call failed: {type(e).__name__}: {e}") from e
    if not resp.choices:
        raise ProposeError("model returned no choices")
    choice = resp.choices[0]
    used = getattr(resp, "model", None) or "(unknown)"
    if getattr(choice.message, "refusal", None):
        raise ProposeError(f"{used} refused: {choice.message.refusal}")
    if choice.finish_reason not in ("stop", None):
        raise ProposeError(f"{used} stopped with finish_reason={choice.finish_reason!r}; output not trusted")
    raw = choice.message.content or ""
    try:
        proposal = Proposal.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as e:
        raise ProposeError(
            f"{used} returned output that does not match the Proposal schema: {e}\n---\n{raw[:2000]}"
        ) from e
    log.info("proposal", extra={"model_requested": model, "model_used": used, "edits": len(proposal.edits)})
    return Result(proposal=proposal, model_requested=model, model_used=used), files


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


def pr_body(res: Result, edits: list[Edit], rejected: list[Rejected]) -> str:
    skipped = res.proposal.skipped
    out = ["Drafted by `crawlgate propose` from a failing crawlgate report. **Review every line.**",
           f"Model: requested `{res.model_requested}`, answered by `{res.model_used}` (via OpenRouter, no fallback).",
           "This PR does not change the gate's verdict; the crawlgate check re-runs on it and decides.", "",
           "| rule | url | change |", "|---|---|---|"]
    out += [f"| `{e.rule}`{''.join(f', `{r}`' for r in e.also_fixes)} | {e.url} | {e.rationale} |" for e in edits]
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
