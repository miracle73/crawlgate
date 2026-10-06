"""Byte-stable report writers: JSON, Markdown (PR comment), HTML."""

from __future__ import annotations

import html
import json
from itertools import groupby
from pathlib import Path

from .models import Finding, Report, Severity

ICON = {Severity.BLOCK: "🛑", Severity.WARN: "⚠️", Severity.INFO: "ℹ️"}


def to_json(r: Report) -> str:
    data = r.model_dump(mode="json")
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load(path: str | Path) -> Report:
    return Report.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _groups(r: Report):
    fs = sorted(r.findings, key=Finding.sort_key)
    return [(sev, list(items)) for sev, items in groupby(fs, key=lambda f: f.severity)]


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def to_markdown(r: Report, max_rows: int = 50) -> str:
    c = r.counts
    out = [
        "<!-- crawlgate -->",
        f"## crawlgate: **{r.verdict.upper()}** for `{r.base_url}`",
        "",
        f"🛑 BLOCK **{c.get('BLOCK', 0)}** · ⚠️ WARN **{c.get('WARN', 0)}** · ℹ️ INFO **{c.get('INFO', 0)}**"
        f" · known (baselined) {c.get('known', 0)} · pages {sum(1 for p in r.site.pages if p.depth >= 0)}",
        "",
    ]
    for sev, items in _groups(r):
        new = [f for f in items if not f.known or sev == Severity.BLOCK]
        if not new:
            continue
        out += [f"### {ICON[sev]} {sev.name} ({len(new)})", "", "| rule | url | finding |", "|---|---|---|"]
        for f in new[:max_rows]:
            msg = f.message + (f" `{f.detail}`" if f.detail and f.detail not in f.message else "")
            out.append(f"| `{f.rule}` | {_md_cell(f.url)} | {_md_cell(msg)} |")
        if len(new) > max_rows:
            out.append(f"| … | | {len(new) - max_rows} more in report.json |")
        out.append("")
    if c.get("known"):
        out.append(f"<sub>{c['known']} known WARN/INFO findings suppressed by baseline. "
                   "Run `crawlgate baseline update` to accept intentional changes.</sub>")
    return "\n".join(out) + "\n"


def to_html(r: Report) -> str:
    e = html.escape
    rows = []
    for sev, items in _groups(r):
        for f in items:
            rows.append(
                f"<tr class='{sev.name.lower()}{' known' if f.known else ''}'><td>{sev.name}</td><td><code>{e(f.rule)}</code></td>"
                f"<td>{e(f.url)}</td><td>{e(f.message)}</td><td><code>{e(f.detail)}</code></td><td>{'yes' if f.known else ''}</td></tr>"
            )
    c = r.counts
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>crawlgate report</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--block:#c62828;--warn:#b26a00;--info:#1565c0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121212;--fg:#eee;--mut:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top;word-break:break-word}}
tr.block td:first-child{{color:var(--block);font-weight:700}}tr.warn td:first-child{{color:var(--warn);font-weight:700}}
tr.info td:first-child{{color:var(--info)}}tr.known{{opacity:.55}}.wrap{{overflow-x:auto}}
</style></head><body>
<h1>crawlgate: {e(r.verdict.upper())}</h1><p>{e(r.base_url)}</p>
<p>BLOCK {c.get('BLOCK', 0)} · WARN {c.get('WARN', 0)} · INFO {c.get('INFO', 0)} · known {c.get('known', 0)}</p>
<div class="wrap"><table><thead><tr><th>severity</th><th>rule</th><th>url</th><th>finding</th><th>detail</th><th>known</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></body></html>
"""
