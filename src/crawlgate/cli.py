"""crawlgate CLI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import typer

from . import checks, gate, lighthouse, report, severity
from . import log as logsetup
from .config import Config, load
from .crawler import crawl as do_crawl
from .models import Finding, Report

app = typer.Typer(add_completion=False, no_args_is_help=True, help="SEO regression gate for CI.")
baseline_app = typer.Typer(no_args_is_help=True, help="Manage the committed baseline.")
app.add_typer(baseline_app, name="baseline")
log = logging.getLogger("crawlgate.cli")

ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="crawlgate.toml")]
DepthOpt = Annotated[int | None, typer.Option("--depth")]
MaxOpt = Annotated[int | None, typer.Option("--max-pages")]
RenderOpt = Annotated[bool | None, typer.Option("--render/--no-render", help="Playwright hydration diff")]


def _cfg(config: Path | None, depth: int | None, max_pages: int | None, render: bool | None) -> Config:
    cfg = load(config)
    if depth is not None:
        cfg.max_depth = depth
    if max_pages is not None:
        cfg.max_pages = max_pages
    if render is not None:
        cfg.render = render
    return cfg


def _build(url: str, cfg: Config) -> Report:
    table = severity.resolve(cfg.severity)
    site = asyncio.run(do_crawl(url, cfg))
    findings = checks.run(site, cfg, table)
    if cfg.lighthouse.enabled:
        urls = [p.url for p in site.pages if p.depth >= 0 and p.surface]
        findings += [Finding(rule=r, severity=table[r], url=u, message=m, detail=d)
                     for r, u, m, d in lighthouse.audit(urls, cfg.lighthouse)]
    return Report(base_url=site.base_url, site=site, findings=findings)


def _write(r: Report, out: Path) -> None:
    out.write_text(report.to_json(r), encoding="utf-8", newline="\n")
    out.with_suffix(".md").write_text(report.to_markdown(r), encoding="utf-8", newline="\n")
    log.info("wrote report", extra={"path": str(out)})


@app.callback()
def main(log_level: Annotated[str, typer.Option(help="DEBUG/INFO/WARNING")] = "INFO") -> None:
    logsetup.setup(log_level)


@app.command()
def crawl(url: str, out: Annotated[Path, typer.Option("--out", "-o")] = Path("report.json"),
          config: ConfigOpt = None, depth: DepthOpt = None, max_pages: MaxOpt = None, render: RenderOpt = None) -> None:
    """Crawl URL and write the extracted SEO surface + findings (no gate)."""
    r = gate.apply(_build(url, _cfg(config, depth, max_pages, render)), None, "none")
    _write(r, out)
    typer.echo(report.to_markdown(r))


@baseline_app.command("update")
def baseline_update(url: str, out: Annotated[Path, typer.Option("--out", "-o")] = Path("crawlgate.baseline.json"),
                    config: ConfigOpt = None, depth: DepthOpt = None, max_pages: MaxOpt = None,
                    render: RenderOpt = None) -> None:
    """Write a new baseline. Commit it and review it like any other diff."""
    r = gate.apply(_build(url, _cfg(config, depth, max_pages, render)), None, "none")
    out.write_text(report.to_json(r), encoding="utf-8", newline="\n")
    typer.echo(f"baseline written to {out} ({len(r.site.pages)} urls, {len(r.findings)} findings)")


@app.command()
def check(url: str, baseline: Annotated[Path | None, typer.Option("--baseline", "-b")] = None,
          fail_on: Annotated[str, typer.Option("--fail-on", help="block|warn|info|none")] = "block",
          out: Annotated[Path, typer.Option("--out", "-o")] = Path("report.json"),
          config: ConfigOpt = None, depth: DepthOpt = None, max_pages: MaxOpt = None, render: RenderOpt = None) -> None:
    """Crawl, diff against baseline, gate. Exits 1 when the gate fails."""
    cfg = _cfg(config, depth, max_pages, render)
    r = _build(url, cfg)
    base = None
    if baseline is not None and baseline.exists():
        base = report.load(baseline)
        r.findings += gate.diff(r.site, base.site, severity.resolve(cfg.severity))
    elif baseline is not None:
        log.warning("baseline not found; gating on findings only", extra={"path": str(baseline)})
    r = gate.apply(r, base, fail_on.lower())
    _write(r, out)
    typer.echo(report.to_markdown(r))
    raise typer.Exit(1 if r.verdict == "fail" else 0)


@app.command("report")
def report_cmd(path: Path, fmt: Annotated[str, typer.Option("--format", "-f", help="html|md|json")] = "html",
               out: Annotated[Path | None, typer.Option("--out", "-o")] = None) -> None:
    """Render a report.json as html, markdown or normalized json."""
    r = report.load(path)
    text = {"html": report.to_html, "md": report.to_markdown, "json": report.to_json}[fmt](r)
    dest = out or path.with_suffix({"html": ".html", "md": ".md", "json": ".json"}[fmt])
    dest.write_text(text, encoding="utf-8", newline="\n")
    typer.echo(str(dest))


@app.command()
def propose(path: Path, repo: Annotated[Path, typer.Option("--repo", help="site source checkout")] = Path("."),
            open_pr: Annotated[bool, typer.Option("--open-pr", help="commit to crawlgate/* branch, open draft PR")] = False,
            base: Annotated[str | None, typer.Option("--base")] = None,
            model: Annotated[str | None, typer.Option("--model", help="OpenRouter model id")] = None,
            config: ConfigOpt = None,
            out: Annotated[Path, typer.Option("--out", "-o")] = Path("crawlgate-fix.md")) -> None:
    """(Optional, needs `pip install seogate[agent]` + OPENROUTER_API_KEY.) Draft fixes. Never changes the verdict."""
    from . import agent

    r = report.load(path)
    repo = repo.resolve()
    try:
        res, files = agent.propose(r, repo, model=model or load(config).agent_model)
    except agent.ProposeError as e:
        typer.echo(f"propose failed, nothing applied: {e}", err=True)
        raise typer.Exit(2) from e
    edits, rejected = agent.validate(res.proposal, repo, files)
    body = agent.pr_body(res, edits, rejected)
    out.write_text(body, encoding="utf-8", newline="\n")
    typer.echo(body)
    if not edits:
        typer.echo("no valid edits proposed")
        return
    touched = agent.apply(edits, repo)
    typer.echo(f"edited: {', '.join(touched)}")
    if open_pr:
        typer.echo(agent.open_pr(repo, touched, body, base))


if __name__ == "__main__":
    app()
