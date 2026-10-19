# teg-dashboard-template

Starting point for a new "Tableau dashboard to webpage" TEG project: FastAPI
+ Snowflake + Terraform + Lambda + Cognito + CloudFront, following the same
architecture as `executive-view-dashboard` and `same-store-sales`.

## Using this template

1. Copy/clone this repo as the new project (or use GitHub's "use this
   template" if this is set up as a template repository there).
2. Read `CLAUDE.md` — it's the full build guide and gets picked up
   automatically by Claude Code.
3. Drop the mockup HTML you were given at `static_dashboard/<slug>.html`,
   and the build-notes markdown wherever's convenient.
4. Set `project_slug` in one place: pass it via
   `terraform apply -var="project_slug=..."` (already wired into
   `deploy.sh` — just edit `PROJECT_SLUG` at the top of that file). Every
   AWS resource name derives from it.
5. Fill in the TODOs in `api/main.py`, `api/refresh.py`, and
   `build_dashboard.py` — each has inline comments pointing at the relevant
   `CLAUDE.md` section.
6. One manual edit Terraform can't avoid: `infra/main.tf`'s
   `backend "s3" { key = "<slug>.state" }` — Terraform backend blocks can't
   use variables, so this literal has to be set by hand.

## What's already done for you

- Snowflake dual-credential loading (shared `tableau_svc` key-pair secret in
  Lambda, `.env` locally), CORS, health check, Mangum handler — `api/main.py`.
- The Cognito auth-gate injection and anchored-replacement scaffold —
  `build_dashboard.py`.
- All 9 Terraform files, parameterized by a single `project_slug` variable
  (see `infra/main.tf`'s `locals` block) — nothing else to rename.
- `.gitignore`/`requirements.txt`/`deploy.sh` — generic, shouldn't need
  changes beyond `PROJECT_SLUG`.

## What's genuinely new per project

- The SQL/business-logic query functions in `api/main.py`.
- The mockup's specific JS structure that `build_dashboard.py`'s
  replacement steps need to target.
- Whichever parts of the data get cached by `api/refresh.py` (depends on
  the caching-strategy decision in `CLAUDE.md`).
