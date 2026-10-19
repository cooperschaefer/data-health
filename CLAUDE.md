# CLAUDE.md — TEG Tableau-to-webpage dashboard projects

## How this project starts

This repo is already a skeleton (cloned from `teg-dashboard-template`) —
`api/main.py`, `api/refresh.py`, `build_dashboard.py`, and all of `infra/`
exist with the reusable boilerplate filled in and project-specific parts
marked `TODO`. You're filling in a template, not building from zero.

You'll also be given two files:

1. **A static HTML mockup** (`static_dashboard/<slug>.html`) — a hand-built,
   self-contained single-page dashboard with real render functions (table
   sorting, heatmap coloring, sticky headers, CSV export, share-link state,
   apply-gated filters, a loading indicator) working against hardcoded mock
   data. This is the **design source of truth**. Never treat it as scratch —
   it survives the whole project and only gets hand-edited for actual design
   changes, not by the live-data build step.
2. **A markdown build-notes doc** — the Snowflake source table(s), the exact
   calculation logic ported from the original Tableau workbook, filter
   behavior, known data-quality caveats, and suggested API endpoints.

Read both in full before touching any TODO. If you have access to a sibling
repo (`executive-view-dashboard`, `same-store-sales`), a handful of
targeted reads of specific files is fine when exact syntax fidelity matters
and this file's prose isn't enough — but you shouldn't need a broad
exploratory investigation; this file already tells you what matters.

## Architecture

Single-file HTML frontend → FastAPI backend on Lambda (via Mangum) →
Snowflake as the only data source. Terraform for all infra. Cognito
hosted-UI auth gated by a CloudFront Function. S3 + CloudFront for hosting.
A refresh Lambda on an EventBridge schedule for whatever portion of the data
can be pre-baked.

### Naming — set `project_slug` once, nothing else needs renaming

`infra/main.tf`'s `locals` block derives every resource name
(`local.name_prefix`, `local.bucket_name`,
`local.cloudfront_comment`, `local.cognito_domain_prefix`) from
`var.project_slug`. Pass it via `-var="project_slug=..."` (already wired
into `deploy.sh` — set `PROJECT_SLUG` at the top of that file).

**One exception:** `infra/main.tf`'s `backend "s3" { key = "<slug>.state" }`
must be a literal — Terraform backend blocks can't reference variables,
since they're resolved before any variable is. Set this by hand once.

| Thing | Pattern |
|---|---|
| Terraform resources | `tf-<project_slug>-*` (via `local.name_prefix`) |
| S3 bucket | `teg-<project_slug>` |
| Snowflake secret | shared `tf-teg-production-snowflake-tableau_svc-creds` (referenced via `data`, not managed — see below) |
| Cognito domain prefix | `teg-<project_slug>` (override via `cognito_domain_prefix` var if it collides — Cognito domains are globally unique across *all* AWS accounts) |
| Terraform state | shared bucket `teg-terraform-states-bucket`, key `<project_slug>.state` (hand-set, see above) |
| Lambda handlers | `api.main.handler` (API), `api.refresh.handler` (refresh) |

## Snowflake auth — shared service account, key-pair only

Snowflake enforces MFA on human accounts, so password auth fails at runtime
with error 250001. The Lambdas connect as the shared `tableau_svc` service
account (role `prod_ro`) using RSA key-pair auth, read from the centrally
owned Secrets Manager secret `tf-teg-production-snowflake-tableau_svc-creds`
(`infra/secrets.tf` references it as a `data` source — never create a
per-project Snowflake secret). `api/main.py`'s `_auth_kwargs()` prefers
`private_key`; password is only a local-dev fallback. Deploying needs no
Snowflake credentials at all.

**`prod_ro` must be able to read every table the dashboard queries.** Before
wiring a query into FastAPI, confirm each source table is readable under
that role (e.g. `SHOW GRANTS ON TABLE ...` or a `SELECT ... LIMIT 1` run as
`prod_ro`) — a table your own login can see may not be granted to it.

## Before writing any SQL

Use the Snowflake MCP tools (`get_table_columns`, `run_sql`) to confirm every
column referenced in the build notes against the **live warehouse**, not
just the notes. Then test the actual aggregation queries (literal date
values standing in for bind params) against real data. Mockups in this
family are typically transcribed from real data at build time — a close
match to the mockup's numbers is a strong signal the query logic is right;
a mismatch means dig deeper before wiring it into FastAPI.

## Backend (`api/main.py`)

Already has: dual credential loading, CORS, health check, Mangum handler,
a `pct_change()` null-safety helper. Fill in:

- One query function per report/table, following the try/except pattern
  (Snowflake `DatabaseError` → 503, anything else → 500 with the exception
  inlined into `detail`) already sketched in the commented-out example.
- Any Total/IRL/RA-style type switch: compute all variants once in SQL,
  pick the one requested in Python — don't branch the SQL itself per type.
- Never crash or return NaN/Infinity on div-by-zero (use `pct_change()`).
- Grand-total rows: sum the raw numerator/denominator columns first, *then*
  derive the delta/%, never average per-row percentages.

## Caching strategy — decide per project, don't default blindly

This is the one thing that genuinely varies by dashboard shape:

- **Small parameter space** (e.g. a single `year`): cache the *whole*
  payload to S3 on a schedule; prod page loads read the cache, live query is
  just a dev/manual-refresh fallback (`executive-view-dashboard` pattern).
- **Filter-heavy, arbitrary ranges** (multi-select zone/store, independent
  date pickers): most filter combinations can't be pre-baked. Scope the
  refresh Lambda to just the **default view** (no filters applied), serve
  that from cache on first paint with no share-link overrides, and let every
  Apply click / non-default share link query live (`same-store-sales`
  pattern). Don't try to cache the Cartesian product of filters.

`api/refresh.py` is scaffolded for either — it just needs
`get_default_dashboard()` (or whatever you name it) implemented in
`api/main.py` and reused, not duplicated.

## Frontend build script (`build_dashboard.py`)

Already has: the Cognito auth-gate injection, the `_replace_once` helper
(fails loudly with a clear message if an anchor isn't found, instead of
silently shipping stale JS), the overall script structure. Fill in the
mock-data → live-data replacement steps:

1. Mock data arrays/functions → `let` globals populated by `fetch`.
   Include an `_isLocalDev()` helper in this step (checks `file:`,
   `localhost`, **and** `127.0.0.1`/`::1` — see the auth section below for
   why `127.0.0.1` matters).
2. Null-safety patches for any calc that can divide by zero (the mock data
   usually never hits that edge case, so the mockup's code won't have it).
3. Any ranking/sorting the live API now does server-side → render the
   server's result instead of re-deriving it client-side.
4. The Apply-button handler → build real query params, fetch live
   endpoints, handle fetch failures visibly (don't fail silently).
5. The init block → fetch filter options + initial data, exposed as a
   function named exactly `initDashboard()` — the already-written
   AUTH_SCRIPT calls this after the auth check passes.

Never hand-edit the generated `dashboard.html` directly — any change should
go through the mockup + rebuild, or it'll be silently clobbered by the next
build.

## Cognito / CloudFront auth pattern (already wired in `infra/cloudfront.tf`)

- The CloudFront Function on the **default** behavior redirects to the
  Cognito hosted UI when no `id_token` cookie is present.
- It already lets `/api/*`, `/health`, `/cache/*`, and any request with
  `?code=` through unauthenticated — don't remove any of these; forgetting
  `/cache/*` or the callback bypass causes an infinite redirect loop.
- The Cognito client id is looked up at CloudFront-Function *runtime* via a
  Key-Value Store, not embedded as a literal — this deliberately breaks the
  Terraform dependency cycle `distribution -> function -> cognito client ->
  distribution`. Already wired; don't "simplify" it back to a literal.
- **Local-dev bypass must check hostname is `localhost` OR `127.0.0.1` (and
  ideally `::1`/`[::1]`), or protocol is `file:`.** Checking only
  `'localhost'` sends anyone testing via `127.0.0.1` into a real — and
  likely not-yet-deployed — Cognito redirect that 404s. Use one shared
  `_isLocalDev()` helper referenced everywhere; don't duplicate the check.
- **First deploy is two-pass.** `COGNITO_CLIENT` in `build_dashboard.py`
  starts as a placeholder (the real id doesn't exist until Cognito is
  created). Deploy once to create the Cognito app client, copy the real id
  from `terraform output cognito_client_id`, paste it into
  `build_dashboard.py`, rebuild, deploy again — `deploy.sh` already prints
  this reminder at the end.

## Terraform

- One shared IAM role for both the API Lambda and the refresh Lambda
  (already wired in `iam.tf`).
- `aws_lambda_function_url` with `authorization_type = "NONE"` — auth is
  enforced entirely at the CloudFront layer, never at the Lambda/IAM layer.
- **`terraform` CLI is typically not available in this sandbox.** Hand-verify
  new/changed `.tf` files line-by-line rather than assuming
  `terraform validate` will catch mistakes, and say so explicitly to the
  user before their first `terraform apply` — don't imply it's been
  dry-run when it hasn't.

## Testing checklist before declaring "done"

This environment typically has no Node.js, no Playwright, no
`chromium-cli`, and no `terraform` CLI. Work around that rather than
skipping verification:

1. Validate every SQL query against live Snowflake via the MCP tools
   *before* wiring it into FastAPI.
2. Start the API locally (`python -m uvicorn api.main:app --reload`) and
   hit every endpoint with `curl` — including edge cases (a filter that
   changes row count, a type switch, a div-by-zero scenario).
3. Run `build_dashboard.py`, then serve `dashboard.html` via
   `python -m http.server` (not `file://`, so relative fetches resolve) next
   to the running API.
4. With no browser-automation tool installed, drive a real Chrome/Edge
   headlessly and read the rendered DOM:
   ```
   "<path to chrome.exe>" --headless --disable-gpu --no-sandbox \
     --virtual-time-budget=8000 --run-all-compositor-stages-before-draw \
     --dump-dom <url> > dump.html
   ```
   Then grep/parse the dump for real data (row counts, specific values —
   not placeholders or zeros). Check stderr for JS errors, filtering out
   unrelated Chrome telemetry noise (GCM registration errors,
   `externally_managed_app_manager` lines are not your bug).
5. Test interactive flows (Apply with changed filters, share links) by
   driving them through URL query params the page's own
   `applyUrlParams()`/share-link logic already reads — this exercises the
   exact same code path a real click would, with no click-simulation tool
   needed.
6. **On Windows specifically:** prefer the Bash tool's `run_in_background`
   over manual shell `&` backgrounding, and stop background dev servers via
   `TaskStop` rather than cross-referencing PIDs between `bash`/`ps` and
   PowerShell's `Get-NetTCPConnection`/`Stop-Process` — they don't always
   agree (Git Bash/MSYS reports its own internal PID; only the *last*
   column of `ps aux` is the real Windows PID `taskkill` sees), and
   `uvicorn --reload` spawns a reloader-parent + worker-child pair that
   makes this worse. If a code change doesn't seem to take effect, don't
   assume the code is wrong — check for a stale duplicate listener before
   debugging the app itself.

## Known caveats worth re-checking on every new dashboard in this family

- Gross-revenue fields (e.g. `REVENUE_COMBINED_LBS`) typically do **not**
  net out refunds. Don't silently "fix" this by netting refunds yourself —
  it would make the new dashboard disagree with every sibling dashboard
  built on the same field. Just flag it in a footnote/tooltip if asked.
- Ticker-style columns often carry a baked-in store-order suffix (e.g.
  `"TPA (46)"`) — never use them as a raw display label if a dedicated
  name column exists instead.
- Prefer a warehouse's already-computed classification column (e.g.
  `CONCEPT`) over re-deriving a fragile string-parsing calc from the
  original Tableau workbook.
- When a build-notes doc says a chart intentionally ignores the page's main
  filters (e.g. a fixed YTD trend vs. an adjustable snapshot comparison),
  preserve that split. If asked to "fix" it later, treat it as a product
  change to confirm, not a bug to quietly patch.

## Keeping this template current

If a real bug gets fixed in a project built from this template (like the
`localhost`/`127.0.0.1` auth-bypass issue), port the fix back into
`teg-dashboard-template` too — otherwise every future project quietly
inherits the same already-solved bug.
