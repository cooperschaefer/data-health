# Data Health Dashboard: Handoff Spec

Mockup: `data_health_dashboard_mockup.html` (static HTML; every number is hard-coded from the checks run on 2026-10-22, so each component needs to be wired to its query in `data_health_checks.sql`).
Stack: FastAPI + Cloud Run + Snowflake (`TEG_PROD.DW`), same conventions as the other internal dashboards.

## Scope
TEG and GBGS concept stores only (about 90 active stores). Excludes Remote Adventures (ticker `RA`), Team Building Hub (ticker `UFT`, concept `Other`) and ADM. The shared filter is defined once at the top of the SQL file; implement it as a single reusable CTE or view (`scope_stores`) rather than repeating it.

## Design options
Three visual treatments of the same functional mockup (identical markup, behavior and data; only the theme layer and header differ). Pick one for the build, or mix elements.
- `data_health_dashboard_firewatch.html`: dusk ridgeline hero, gold-on-maroon, ribbon-tag buttons and tabs, Cinzel + Source Sans 3. Dark by default; light is parchment.
- `data_health_dashboard_portal.html`: twilight gradient with stars, content in a frosted glass frame, status as a serif headline, Instrument Serif + Figtree, pill controls. Light by default; dark is a night sky.
- `data_health_dashboard_brand.html`: GBGS navy with diagonal stripe header and outlined game-show shapes, GBGS gradient on the banner border, active tab and progress bars, TEG Feverish Pink pill CTAs. Tenon (fallback Outfit) + DM Sans per the shared design system. Dark by default; light uses the off-white page.
- `data_health_dashboard_central.html`: the Portal design plus **Data Central**, a full-screen view opened from a "Data Central" button left of "Review the issues" (or with `?view=central`). 21 tiles, one per test (the placeholder-rows check lives on the Integrity tab; its Data Central slot became the sheet fill-in tile), each with a status (red/yellow/green/info), a headline number, a mini chart, a one-line detail and the tab it opens. Clicking a tile closes the view, opens that tab and scrolls to and briefly highlights the relevant panel. Tile numbers are computed from the same data as the tabs. The 7x3 grid is sized in viewport units to fit one screen at 1920x1080 (verified at 1920x1080, 1920x950 and 1920x900); below 1800px wide it becomes a scrolling 4-column grid (3 columns below 1150px, 2 below 1000px). Escape and "Back to dashboard" close it; the "9 findings" button opens the attention list.
Fonts load from Google Fonts for the mockups. Tenon and DIN Next are licensed and not embedded, so the brand option falls back to Outfit for headings until Tenon is self-hosted. No logos are used.

## Layout (top to bottom)
1. **Header:** title, scope badge, last-checked time, Share URL, Export CSV, theme toggle.
2. **Filters (Apply-gated):** concept (TEG + GBGS / TEG / GBGS), heatmap window (14 / 28 days). Dirty-state dot and "Unapplied changes" text until Apply. Filters live in the query string.
3. **Status banner:** overall status (worst of all checks), last successful build time, red/yellow counts, tables fresh count.
4. **Category strip:** Freshness, Coverage, Volume, Integrity, Reconciliation, each with status dot, headline value, one-line detail.
5. **Tabs:** Overview, Revenue and volume, Reviews, NPS, Integrity, Targets, Sheet upkeep.

## Components and data sources
| Component | Check | Notes |
|---|---|---|
| Table tiles | 1, 2 | `INFORMATION_SCHEMA.TABLES.LAST_ALTERED`; do not use `MAX(date)` on `agg_stores_day` (date spine runs to 2026-12-01 and includes future bookings). |
| Needs-attention table | all | Built from the check results (below). Sortable. Last column is a static "usual cause" string per check. |
| Network revenue line | 3 | Actual vs same-weekday baseline; red under 0.5, yellow under 0.65. Wide on purpose. |
| Reconciliation line | 7 | Ratio of `agg_stores_day.revenue_xola_lbs` to booking net paid; alert on drift from 28-day average (yellow over 5 pts, red over 8 pts). Ratio normally 0.91-0.97, so never test for equality. |
| Store x day heatmap | 4 | Statuses: OK, LOW (<0.4 of same-weekday avg), ZERO, HIGH (>2.5), NA (no baseline). Sticky header row and store column. |
| Yesterday's outliers | 5 | Same logic as heatmap, yesterday only. |
| Unknown revenue | 6a, 6b | Share of Revel revenue in `unknown_revel_revenue_lbs`. |
| Review coverage matrix | 10 | Store x platform, driven by `STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING`. Statuses: MISSING_MAPPING, TICKER_MISMATCH (mapping ticker differs from `dim_stores_master` by case/whitespace), MAPPED_NO_DATA, FEED_STALE (mapped, 0 reviews in 30 days, prior pace 3+ per 30), OK. Google counts as mapped when `GOOGLE_PLACES_ID` is populated. |
| Mapping sheet problems | 10b | Mapping rows with a blank platform ID or a ticker that doesn't join to the dimension. Any row is a problem. |
| Unmapped rows | 11 | KPI per source (Google, TripAdvisor, Yelp). **Click a source to open its drill-down (11b):** the platform listings behind the unmapped reviews (name, brand, platform ID, URL, count, latest review) plus a diagnosis from looking that ID up in the mapping sheet (not in sheet: add a row; in sheet but ticker differs: fix ticker; in sheet and ticker matches: check the downstream join). TripAdvisor joins on `TRIPADVISOR_LOCATION_ID`; Yelp joins on `SERPAPI_PLACE_ID` (not `YELP_BUSINESS_ID`, which holds the URL alias). NPS drill-down is 11c, grouped by the captured location text; a null location is a survey-capture problem, not a mapping one. |
| NPS coverage | 12a, 12b, 13 | 12a is sparse (no row for zero-response weeks), so zero-fill in the UI. |
| Integrity KPIs | 8, 8b, 9 | Duplicate IDs, null store, refund over paid, orphan store keys, duplicate store-days. |
| Xola sellers not set up | 15a | Xola sellers (`STG_XOLA_DATABASE__SELLER_SOURCE`) whose ID isn't in `SELLER_ID`, `XOLA_SELLER_ID` or `_GBGS_XOLA_SELLER_ID` of `dim_stores_master`, with bookings already landing for them. Red when bookings exist, yellow when not. Shows the seller, address, booking count, first arrival and a suggested sheet row (city-name match against rows with no seller ID; heuristic). Copy ID button. |
| Unknown booking sellers | 15b | Seller IDs on recent bookings that match neither the dim nor Xola. Normally empty. Bookings with a NULL `X_SELLER_ID` are a legacy source (last created 2026-06-12) and are excluded. |
| Placeholder store rows | 15c | Sheet rows with no concept, ticker `UNK` or no seller ID, and whether they have bookings (60 days). |
| Suggested sheet values | 16 | Panel on the Integrity tab, opened from the "Sheet fill-in suggestions" Data Central tile. For each unmatched seller, one strip laid out in the sheet's own column order (column letters, field name, what the sheet has now, suggested value). Each suggestion carries a basis and one of three confidences: from Xola (green), follows the sheet's pattern (blue), check before pasting (yellow). Click a cell to select it, Shift-click for a range, "Select all suggested" / "Select Xola and pattern only" / Clear. **Copy selected** copies a tab-separated row for one contiguous group and shows where to paste it (e.g. N114). Non-adjacent selections are split into groups with a Copy button each, or **Copy as one range**, where unselected cells keep their value from the uploaded file (warn the user that this overwrites those cells). Compact view skips empty columns and marks gaps; "Show all columns" shows all 58. Deliberately blank: lat/long (Xola's are city-level, about 4 km off), `_google_places_id`, `_GM`, `_ADP Location`, slots, game mix, pay. |
| Slots upkeep | 17a, 17b | Stores open with 0 or missing slots in `agg_stores_day` and why (no column in the Historical slots sheet, a column under a different name, or values in the sheet that the aggregate isn't picking up), plus stores where Store Team Info slots differ from the Historical slots sheet for the current month. Note `ATL_2` has an underscore and works, so don't assume headers alone are the cause. |
| New-store checklist | 18 | Stores (rows) x per-store sheets (columns): Historical slots, Seasonality, NetSuite revenue and 4-wall budgets and actuals. Flat management cost and the GM score tabs are checked but currently clean. |
| Month-by-month sheets | 19a, 19b | Latest month with any value per monthly sheet and the stores filled that month. "Behind" means behind where the sheet should be (actuals within about a month, budgets to year end); adjustment sheets are exception-only and shown as information. |
| Sheets that look retired | 19c | Static list for now: store game count, gift card breakage, ADP data import, GM stretch goal, the older guest care call volume table, Resova sales. The user should confirm which are truly retired. |

## Targets tab (Check 14)
Checklist of targets loaded for the **next calendar month** (default: month after today; make the month a parameter).
- **Cards (one per target type):** loaded / applicable stores, progress bar, chip (Complete / Partial / None loaded / Doesn't apply), and how many missing stores had one this month. Clicking a card filters the matrix to stores missing that type; click again or "Clear card filter" to reset.
- **Matrix:** store x target type. Cell states: Loaded; Missing (store had a target this month); Missing, outlined (none this month either, so the store may be absent from the sheet); n/a. Sorted by number missing. "Only stores with something missing" checkbox. The concept filter applies.
- **Target types (10):** monthly revenue, daily revenue, daily volume (`LBS_KEYS__*` tables, `DATE` is `YYYYMMDD` text); daily staffing target (`LBS_KEYS__DAILY_STAFFING_TARGETS`, keyed on the 1st of the month); staffing efficiency, staffing budget, staff budget, staff HPG target, staff HPG budget, staff HPG trendline (`TARGETS_LOADING__*`, from the 2025 targets loading sheet, `MONTH_DATE_KEY` = `YYYYMMDD` of the 1st). Eight of these are also columns on `agg_stores_day`; staffing budget and HPG trendline are not.
- **Applicability (assumed, confirm):** staff budget and staff HPG budget are loaded only for TEG stores (0 GBGS in any month), so they show n/a for GBGS. Make this a small config map so it's easy to change.
- **Severity:** not complete = yellow until 7 days before the month starts, then red (and red once the month has started).
- **Check 14c panel:** target rows whose ticker doesn't exist in `dim_stores_master` (typos, `TEMP...` placeholders, unopened stores). Informational.

## Results table (recommended)
Have a scheduled job run the checks and write one row per check per run to a small table (for example `DATA_HEALTH.CHECK_RESULTS`: `run_ts, check_id, severity, metric_value, threshold, detail_json`). The dashboard reads the latest run, and history gives "how often has this failed" and a basis for alerting later. The mockup's attention table maps one-to-one onto this table.

## Severity rules (first pass, tune after a few weeks)
- Freshness: yellow over 24h, red over 30h since `LAST_ALTERED`.
- Coverage: MISSING_MAPPING and TICKER_MISMATCH = red; MAPPED_NO_DATA and FEED_STALE = yellow.
- Volume: store LOW/ZERO yesterday = yellow; network under 0.5 = red.
- Reconciliation: drift thresholds above.
- Integrity: any non-zero count = yellow unless noted; duplicate IDs or null store keys = red.

## Known data quirks
- The review mapping sheet has 10 rows with a blank platform ID and 6 rows whose ticker is upper-cased (GBGSCOLONY, GBGSOAKB, GBGSOPRY) while the dimension uses GBGSColony, GBGSOakB, GBGSOpry. The joins are case-sensitive, so those stores look unmapped.
- TripAdvisor can have several mapping rows per store (one per product listing), so de-duplicate before joining.
- `agg_stores_day.teg_gbgs` is null everywhere; use `concept`.
- `fct_ops_bookings_adjustments.updated_at` is null and recent `booking_id` is null; use `x_booking_id` and table `LAST_ALTERED`.
- **Time zone:** show everything in Central time. Snowflake returns timestamps in the session time zone (Pacific on the connection used here). Set `ALTER SESSION SET TIMEZONE = 'America/Chicago'` when the app connects so `CURRENT_DATE` and the "yesterday" and "this month" logic in the checks follow Central, and use `CONVERT_TIMEZONE('America/Chicago', ts)` for displayed timestamps. The tile times, the last-build time and the "last checked" time in the HTML are already written as CT.
- 2-6 active stores show $0 on a normal day, so zero-revenue checks must compare to the store's own weekday history.

## Sheet upkeep notes
- Fivetran's `_FIVETRAN_SYNCED` shows when a sheet was synced, not edited, so these checks read the contents.
- Wide sheets (one column per store ticker) are read with `OBJECT_CONSTRUCT_KEEP_NULL(*)` plus `FLATTEN`, so new store columns are picked up without editing the SQL. `OBJECT_CONSTRUCT(*)` drops NULL columns, which hides empty store columns.
- Date formats differ by sheet: LBS Keys sheets use `MM/DD/YYYY`, NetSuite entries use `YYYYMMDD`, Historical slots uses labels like `Sep 2026`.
- Slots per day cannot be compared with Xola event counts (peak events run far above slots).

## Hard-coded pieces to replace with live data
- The store x day heatmap is generated in JavaScript from a seeded random function (only the four low-revenue stores for the latest day are real). Replace it with Check 4.
- The "Weekly responses by store" panel on the NPS tab is a placeholder (Check 12a).
- Everything else (tiles, charts, tables, the attention list, the Data Central numbers, the suggested sheet values) is a literal copy of a check result and needs its query behind it. Data Central computes its counts from the same arrays the tabs use, so wiring the arrays is enough.

## Out of scope for v1
- dbt run/test status (needs run artifacts loaded somewhere queryable).
- Orphan-product check against the raw sales source and `dim_sales_products`.
- Alerting (email/Slack) on red checks.
