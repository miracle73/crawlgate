# crawlgate

[![ci](https://github.com/miracle73/crawlgate/actions/workflows/ci.yml/badge.svg)](https://github.com/miracle73/crawlgate/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/seogate.svg)](https://pypi.org/project/seogate/) [![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

SEO breakage is a deploy bug that nobody writes tests for. A refactor points canonicals at staging, `og:image` disappears from three templates, a route starts returning 200 where it should 404. Nobody notices for weeks, until traffic drops or someone shares a link and gets a blank grey card.

crawlgate catches this in the pull request. It crawls the preview deployment, extracts the SEO surface of every page, diffs it against a baseline committed to the repo, and fails the check when something breaks.

![crawlgate PR comment blocking a refactor that added noindex and pointed a canonical at staging](https://raw.githubusercontent.com/miracle73/crawlgate/main/docs/pr-comment.png)

<sub>A real run, open it yourself: [miracle73/crawlgate-action-demo#1](https://github.com/miracle73/crawlgate-action-demo/pull/1). A "refactor head tags" PR quietly added `noindex` to `/about` and pointed `/pricing`'s canonical at staging. The check failed and the merge was blocked.</sub>

## The story: tipmaster.net

Months ago I audited [tipmaster.net](https://tipmaster.net/de). It shipped `og:title`, `og:description` and `twitter:card` across its locales, but no `og:image` at all. Every link shared to WhatsApp or Discord rendered as a blank grey box.

They have since added one. It is **1200x624, six pixels under the 1200x630 spec**. That is exactly the kind of regression a human reviewer signs off on ("looks like a social card to me") and a gate catches.

### 1. First run: the gate fails

```
$ crawlgate check https://tipmaster.net/de --depth 1 --max-pages 40
## crawlgate: FAIL for https://tipmaster.net/de
🛑 BLOCK 3 · ⚠️ WARN 50 · ℹ️ INFO 3 · pages 13

🛑 BLOCK
| noindex_production_route | https://tipmaster.net/daily/de/signin             | page is noindex `noindex, nofollow` |
| noindex_production_route | https://tipmaster.net/daily/de/signup             | page is noindex `noindex, nofollow` |
| noindex_production_route | https://tipmaster.net/daily/de/signup?via=google  | page is noindex `noindex, nofollow` |

⚠️ WARN
| og_image_too_small      | https://tipmaster.net/de  | og:image is 1200x624, need >=1200x630 `https://tipmaster.net/img/og/daily.jpg` |
| og_image_too_small      | https://tipmaster.net/en  | og:image is 1200x624, need >=1200x630 ... |
  ... same on es, fr, id, it, nl, pt, th, tr and the auth pages
| hreflang_not_reciprocal | https://tipmaster.net/daily/de/signin | `en` target does not link back `en https://tipmaster.net/en/` |
  ... and 10 more locales: the auth pages declare locale homepages as alternates, the homepages don't link back
| description_length      | https://tipmaster.net/fr  | meta description is 161 chars (50-160) |
$ echo $?
1
```

The three BLOCKs are **correct behaviour on tipmaster's part**: sign-in and sign-up pages should be `noindex`. The gate blocks anyway, because it can't tell an intentional noindex from a refactor that leaked `noindex` onto production routes. That second case is the one you want to catch.

### 2. Declare intent in config

```toml
# crawlgate.toml
[crawlgate]
production_hosts = ["tipmaster.net"]
noindex_allowed  = ["/signin", "/signup"]   # intentionally noindex
exclude          = ['[?]']                  # don't crawl query-string variants
```

Config can **narrow where a rule applies** (these routes are meant to be noindex). It can **never lower a severity**: `noindex_production_route = "warn"` would be ignored and logged as `severity downgrade ignored`. A prompt is a request, a check is a guarantee.

### 3. Run again: exit 0, warnings visible

```
$ crawlgate check https://tipmaster.net/de --config crawlgate.toml --depth 1
## crawlgate: WARN for https://tipmaster.net/de
🛑 BLOCK 0 · ⚠️ WARN 37 · ℹ️ INFO 2 · pages 12
$ echo $?
0
```

The og:image and hreflang warnings stay in the PR comment. Each difference between steps 1 and 3 traces to the config: the two allowed noindex routes, plus the 15 findings on the excluded `?via=google` page.

### 4. Accept the rest as the baseline

```
$ crawlgate baseline update https://tipmaster.net/de --config crawlgate.toml
$ git add crawlgate.baseline.json   # reviewed like any other diff
```

From then on, existing WARNs show up as `known` and only new regressions fail the build. A BLOCK can't be baselined away.

## Determinism

Byte-stable reports are the headline claim, so they are tested, not asserted:

- The same URL and config, run twice, produces an identical frontier and identical bytes. This was checked live on tipmaster.net (612 URLs) and is enforced in `tests/test_determinism.py` against the full pipeline using `crawlgate.example.toml`.
- The frontier doesn't depend on concurrency (`concurrency=1` and `8` give the same page set).
- Referenced URLs that aren't crawled (hreflang and canonical targets, links, sitemap entries) are verified in priority order: hreflang/canonical targets first, then links, then sitemap. If the `max_verify` budget runs out, the run emits a `verify_truncated` WARN. Checks never skip silently. An earlier version cut this list alphabetically at `max_pages*2`, which made hreflang findings depend on `--max-pages`.
- Known noise: tipmaster's raw HTML differs on every request because of per-request Sentry `sentry-trace`/`baggage` meta tags. crawlgate never reads those, so the extracted surface is identical across 10/10 fetches.

A page with no `og:image` at all (the blank-grey-box case, from the `no-og` fixture in `tests/`):

```
| og_image_missing      | http://127.0.0.1:…/no-og | missing og:image |
| twitter_image_missing | http://127.0.0.1:…/no-og | missing twitter:image (and no og:image fallback) |
```

## Engineering notes

The verify pass, which status-checks URLs the crawl references but doesn't expand, used to have a silent cap. On tipmaster.net it ran out of budget on sitemap entries before reaching the locale homepages that the sign-in page declares as hreflang alternates. The reciprocity check for those pages was skipped, nothing reported it, and the run exited green. The defect wasn't the missing check. It was that nothing said the check was missing. A gate that passes because it didn't look is worse than no gate, because people trust it.

Now the budget is explicit (`max_verify`) and prioritised by what matters most: hreflang and canonical targets first, then internal links, then sitemap entries. Running out of budget is itself a reported WARN (`verify_truncated`). The same discipline applies in the other direction, to real noise. tipmaster's HTML changes on every request because of per-request Sentry `sentry-trace` and `baggage` meta tags. A naive HTML diff would flag every page on every run and teach people to ignore it. crawlgate diffs a typed SEO surface, not markup, so those tags never reach the report.

## Optional: drafted fixes (`crawlgate propose`)

This is separate from the gate. It reads a failing `report.json`, finds the source files behind the failing pages, and asks a model (through OpenRouter, default `anthropic/claude-sonnet-4.5`) for minimal edits as structured JSON.

```bash
pip install "seogate[agent]"
export OPENROUTER_API_KEY=...
crawlgate propose report.json --repo .                       # writes crawlgate-fix.md, edits the working tree
crawlgate propose report.json --repo . --open-pr             # commits to a crawlgate/* branch, opens a draft PR
crawlgate propose report.json --repo . --model openai/gpt-5  # or set agent_model in crawlgate.toml
```

The agent proposes, code and people decide:

- **No silent model swap.** One call, no fallback model, no retries. A refusal, error, truncated output, or a response that doesn't validate against the `Proposal` Pydantic model stops the run with exit code 2 and applies nothing. The PR body records both the requested model and the model that actually answered.
- **Every edit is checked in code.** It must target a file the model was shown, match exactly once, and fix a rule a text edit can fix. A missing og:image needs an asset, so it is never "fixed" with a guessed URL.
- **The verdict's inputs are protected.** The baseline, `crawlgate.toml`, `.github/` and the action are never shown to the model and can't be edited.
- **Draft PRs only.** It opens a **draft** PR from a fresh `crawlgate/*` branch and never pushes to the base branch. The gate never imports the agent, and a test enforces this.

### A real run

Against the failing [demo PR #1](https://github.com/miracle73/crawlgate-action-demo/pull/1), `crawlgate propose --open-pr` opened [draft PR #2](https://github.com/miracle73/crawlgate-action-demo/pull/2):

<img src="https://raw.githubusercontent.com/miracle73/crawlgate/main/docs/propose-pr.png" alt="PR body written by crawlgate propose" width="720">
<img src="https://raw.githubusercontent.com/miracle73/crawlgate/main/docs/propose-gate.png" alt="crawlgate PASS on the drafted PR" width="720">

The edits were correct and minimal: two lines, a byte-exact revert of the breakage (`git diff main -- site` is empty). The gate re-ran on the draft PR and passed: 0 BLOCK, 0 WARN, 0 INFO.

It wasn't perfect. The PR body lists `noindex_in_sitemap` under **"Not fixed"**, but the same edit fixes it. The schema had no way to say "covered by another edit", so the model put it in `skipped`, and the PR body called it unfixed. That is misleading in exactly the place a reviewer looks. The schema now has `also_fixes`, and the prompt requires every finding to land in exactly one place. A second live run accounted for all three findings with no "Not fixed" section. PR #2 is left as it was produced.

## Limits

- **The demo workflow serves files on localhost, not a real preview deploy.** [crawlgate-action-demo](https://github.com/miracle73/crawlgate-action-demo) runs `python -m http.server` on the PR checkout. In real use, point `url:` at your Vercel, Netlify or Cloudflare preview, and set `production_hosts` so canonicals pointing at production aren't flagged as foreign.
- **Lighthouse needs its CLI installed separately** (`npm i -g lighthouse`). Without it, CWV budgets are skipped with an INFO finding, not silently. Lighthouse numbers are also timing-dependent, so only the over/under verdict goes in the report.
- **Orphan detection needs a complete crawl.** If `max_depth` or `max_pages` stops the crawl early, orphan checks are switched off, because "not reached" doesn't mean "not linked".
- **Live sites aren't hermetic.** A request that still times out after retries changes the result. Byte-stable means the same responses give the same bytes. It can't make a flaky network deterministic.
- **`propose` has one real data point.** That's the demo above: a static site where the fix was a two-line revert. Templated sites (Next.js layouts, CMS-driven metadata) are harder, and fix quality there is unmeasured.
- **JSON-LD validation covers required properties for about 25 common schema.org types.** Other types get an INFO finding (`jsonld_unknown_type`), not full schema validation.

## Usage

The PyPI package is `seogate` because `crawlgate` on PyPI belongs to an unrelated search-API SDK. The command and import name are `crawlgate`.

```bash
pip install seogate            # `crawlgate` was taken on PyPI by an unrelated SDK; the command is still `crawlgate`
playwright install chromium

crawlgate crawl https://example.com --out report.json            # extract + findings, no gate
crawlgate baseline update https://example.com                     # writes crawlgate.baseline.json; commit it
crawlgate check https://pr-42.preview.example.com --baseline crawlgate.baseline.json --fail-on block
crawlgate report report.json --format html
```

`check` writes `report.json` and `report.md`, which is the PR comment. It exits 1 when the gate fails.

## The gate

- **Three severities: BLOCK, WARN, INFO.** They are hard-coded in [`severity.py`](src/crawlgate/severity.py). They are not set in config and not decided by a model.
- **Config can raise a severity but never lower it.** `noindex_in_sitemap = "info"` is ignored and logged as `severity downgrade ignored`. A prompt is a request, a check is a guarantee.
- **The baseline handles intentional change.** Findings already in the baseline are marked `known`, and known WARN/INFO findings don't fail the build. BLOCK findings always fail it, baseline or not.
- **Baselines compare paths, not hosts.** A baseline taken from production can gate a preview on `pr-42.vercel.app`.
- **Reports are byte-stable.** URLs are sorted, the crawl runs BFS level by level in a fixed order, JSON keys are sorted, and there are no timestamps or timings. Two runs against the same site produce identical bytes. Lighthouse metric values go to the log, never into the report.

| BLOCK | WARN |
|---|---|
| noindex on a production route | missing og:image, og:image relative / <1200x630 / not 200 / not image/* |
| canonical on a non-production host | meta description outside 50-160 chars |
| canonical pointing at a noindex page | redirect chain > 1 hop |
| sitemap URL returns 404/5xx | internal link to 404 |
| noindex page listed in sitemap.xml | hreflang not reciprocal / incomplete |
| hreflang target is dead | orphan page (in sitemap, unreachable) |
| status regressed 2xx -> 4xx/5xx vs baseline | SEO tags only present after hydration |
| robots.txt disallows everything | JSON-LD invalid or missing required props |

The full table is in [`severity.py`](src/crawlgate/severity.py).

## What it extracts

For each URL: title, meta description, canonical, meta robots, X-Robots-Tag, the hreflang set, `og:*`, `twitter:*`, JSON-LD blocks (parsed, `@type` checked, required properties), HTTP status, redirect chain and final URL. It also probes the og:image itself: status, content type, real pixel dimensions, and the width/height/alt meta tags. Each page is rendered in Chromium, and any SEO field that exists only after hydration becomes a finding, because WhatsApp, Facebook and many bots never run JavaScript.

Across the whole site it checks robots.txt, sitemap.xml (including sitemap indexes), orphan pages, internal links that hit 404s or redirects, hreflang reciprocity, and indexability conflicts. It can also check Lighthouse Core Web Vitals against a budget (`[crawlgate.lighthouse]`, needs `npm i -g lighthouse`).

The crawler works breadth-first in a deterministic order. It uses a bounded worker pool, token-bucket rate limiting, robots.txt, and exponential-backoff retries on 429/5xx and network errors. Depth, page count and include/exclude regexes are configurable.

## GitHub Action

```yaml
on: pull_request
permissions: { contents: read, pull-requests: write }
jobs:
  seo-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: your-org/crawlgate@v0
        with:
          url: ${{ steps.preview.outputs.url }}
          baseline: crawlgate.baseline.json
          fail-on: block
```

The action posts (and updates) one PR comment with changes grouped by severity, writes the job summary, and fails the check on a block. See [`docs/example-workflow.yml`](docs/example-workflow.yml) and the live demo at [miracle73/crawlgate-action-demo](https://github.com/miracle73/crawlgate-action-demo).

## Development

```bash
git clone https://github.com/miracle73/crawlgate && cd crawlgate
uv sync && uv run playwright install chromium
uv run pytest -q
```

The fixtures serve a real local site: a known-good page, a page missing og:image, a noindex page in the sitemap, a broken hreflang set, and a 3-hop redirect chain.
