-- =============================================================================
-- DATA HEALTH DASHBOARD: CHECK QUERIES  (TEG_PROD.DW)
-- Tables: AGG_STORES_DAY, FCT_OPS_BOOKINGS_ADJUSTMENTS, FCT_NPS_DETAILS,
--         FCT_STORES_ALL_REVIEWS (+ DIM_STORES_MASTER)
--
-- Every check was run against TEG_PROD.DW on 2026-10-12 and returned rows (Check 11 and
-- Check 6a/6b were validated in simplified form; Check 8 had an ambiguous-column bug that
-- was fixed after the first run and re-verified as a standalone query).
-- Thresholds are first-pass guesses based on one day of history; tune them after a few weeks.
-- Check 12a is sparse: weeks with zero responses have no row, so the UI must fill zeros.
-- ADM, Remote Adventures and Team Building Hub are out of scope (see SCOPE below).
--
-- GOTCHAS DISCOVERED WHILE WRITING THESE
--  1. AGG_STORES_DAY is a date spine out to 2026-12-01 and revenue exists on
--     FUTURE dates (forward bookings). MAX(DATE_ACTUAL) is NOT a freshness signal.
--     Use INFORMATION_SCHEMA LAST_ALTERED instead (Check 1).
--  2. AGG_STORES_DAY.TEG_GBGS is NULL for every row. Use CONCEPT (TEG/GBGS/ADM/Other).
--  3. A baseline of ~2-6 active stores per day have $0 revenue (closed days etc.),
--     so "zero revenue" must be judged against that store's same-weekday history.
--  4. FCT_OPS_BOOKINGS_ADJUSTMENTS.UPDATED_AT is NULL and BOOKING_ID is NULL in
--     recent rows. Use X_BOOKING_ID and table LAST_ALTERED instead.
--  5. Booking-level paid revenue does NOT equal AGG_STORES_DAY.REVENUE_XOLA_LBS
--     (agg runs ~91-97% of net paid). Reconcile on ratio DRIFT, not equality (Check 7).
--
-- TIME ZONE: show everything in Central time. Snowflake returns timestamps in the session time zone (Pacific on the
-- connection used to write these), so either run  ALTER SESSION SET TIMEZONE = 'America/Chicago'  when the app connects
-- (this also makes CURRENT_DATE and every "yesterday" / "this month" calculation below follow Central), or wrap displayed
-- timestamps in CONVERT_TIMEZONE('America/Chicago', ts) as Check 1 does.
-- Standard "active store" filter used throughout:
--   STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
-- =============================================================================


-- -----------------------------------------------------------------------------
-- CHECK 1: TABLE FRESHNESS  (banner + per-table tiles)
-- Catches: "dbt runs failed overnight, data stopped at 6pm yesterday"
-- Visual: one status tile per table, big "hours since refresh" number
-- -----------------------------------------------------------------------------
SELECT
    TABLE_NAME,
    CONVERT_TIMEZONE('America/Chicago', LAST_ALTERED)  AS last_altered_ct,
    DATEDIFF(hour, LAST_ALTERED, CURRENT_TIMESTAMP())  AS hours_since_refresh,
    CASE WHEN DATEDIFF(hour, LAST_ALTERED, CURRENT_TIMESTAMP()) > 30 THEN 'RED'
         WHEN DATEDIFF(hour, LAST_ALTERED, CURRENT_TIMESTAMP()) > 24 THEN 'YELLOW'
         ELSE 'GREEN' END                               AS status
FROM TEG_PROD.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'DW'
  AND TABLE_NAME IN ('AGG_STORES_DAY','FCT_OPS_BOOKINGS_ADJUSTMENTS','FCT_NPS_DETAILS',
                     'FCT_STORES_ALL_REVIEWS','DIM_STORES_MASTER')
ORDER BY hours_since_refresh DESC;


-- -----------------------------------------------------------------------------
-- CHECK 2: LATEST BUSINESS DATE PER SOURCE  (data-lag, separate from table refresh)
-- A table can refresh on time but still have no new rows upstream.
-- Visual: small table under each tile: "latest record" + "days behind"
-- -----------------------------------------------------------------------------
SELECT 'FCT_OPS_BOOKINGS_ADJUSTMENTS (created)' AS source,
       MAX(BOOKINGS_CREATED_ON)                  AS latest_date,
       DATEDIFF(day, MAX(BOOKINGS_CREATED_ON), CURRENT_DATE) AS days_behind
FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS
UNION ALL
SELECT 'FCT_NPS_DETAILS (survey)', MAX(SURVEY_DATE)::DATE, DATEDIFF(day, MAX(SURVEY_DATE)::DATE, CURRENT_DATE)
FROM TEG_PROD.DW.FCT_NPS_DETAILS
UNION ALL
SELECT 'FCT_STORES_ALL_REVIEWS (' || REVIEW_PLATFORM || ')', MAX(REVIEW_DATE), DATEDIFF(day, MAX(REVIEW_DATE), CURRENT_DATE)
FROM TEG_PROD.DW.FCT_STORES_ALL_REVIEWS
GROUP BY REVIEW_PLATFORM
UNION ALL
-- agg: last day with revenue that is not in the future
SELECT 'AGG_STORES_DAY (last past day with revenue)',
       MAX(DATE_ACTUAL),
       DATEDIFF(day, MAX(DATE_ACTUAL), CURRENT_DATE)
FROM TEG_PROD.DW.AGG_STORES_DAY
WHERE DATE_ACTUAL < CURRENT_DATE
  AND STORE_STATUS IN ('Mature','Ramping','New')
  AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
  AND COALESCE(REVENUE_XOLA_LBS,0) + COALESCE(REVENUE_REVEL_LBS,0) > 0;


-- -----------------------------------------------------------------------------
-- CHECK 3: NETWORK DAILY REVENUE vs SAME-WEEKDAY BASELINE  (last 14 days)
-- Catches: partial loads, "volume and revenue look low/not updated"
-- Visual: line chart of actual vs expected band; red marker where ratio < 0.8
-- -----------------------------------------------------------------------------
WITH daily AS (
    SELECT DATE_ACTUAL, DAYOFWEEK(DATE_ACTUAL) AS dow,
           SUM(COALESCE(REVENUE_XOLA_LBS,0) + COALESCE(REVENUE_REVEL_LBS,0)) AS total_rev,
           SUM(COALESCE(GAME_COUNT_LBS,0))                                    AS games
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
      AND DATE_ACTUAL BETWEEN DATEADD(day,-70,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
    GROUP BY 1,2
), w AS (
    SELECT *,
           AVG(total_rev) OVER (PARTITION BY dow ORDER BY DATE_ACTUAL ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_rev,
           AVG(games)     OVER (PARTITION BY dow ORDER BY DATE_ACTUAL ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_games
    FROM daily
)
SELECT DATE_ACTUAL, ROUND(total_rev) AS total_rev, ROUND(baseline_rev) AS baseline_rev,
       ROUND(total_rev / NULLIF(baseline_rev,0), 2) AS rev_ratio,
       games, ROUND(baseline_games) AS baseline_games,
       -- Network-level ratio is noisy (holiday weeks in the 4-week baseline push it to ~0.7 on some Mondays,
       -- ~1.3 mid-week), so thresholds are wide. Treat as a "something is badly wrong" tripwire.
       CASE WHEN total_rev / NULLIF(baseline_rev,0) < 0.5 THEN 'RED'
            WHEN total_rev / NULLIF(baseline_rev,0) < 0.65 THEN 'YELLOW' ELSE 'GREEN' END AS status
FROM w
WHERE DATE_ACTUAL >= DATEADD(day,-14,CURRENT_DATE)
ORDER BY DATE_ACTUAL;


-- -----------------------------------------------------------------------------
-- CHECK 4: STORE x DAY HEATMAP  (last 28 days)
-- Catches: a store whose data stopped, zero days, partial days
-- Visual: heatmap, rows = stores, columns = days, cell color = status
--   ZERO = $0 where this store normally earns on that weekday (RED)
--   LOW  = <40% of same-weekday avg (YELLOW);  HIGH = >2.5x (BLUE, informational)
--   N/A  = no baseline (new store / normally closed)  -> grey
-- Note: at a 0.5 LOW threshold ~11% of store-days flagged (too noisy); 0.4 used here.
-- -----------------------------------------------------------------------------
WITH d AS (
    SELECT DIM_STORES_MASTER_ID, LBS_NAME, CONCEPT, DATE_ACTUAL, DAYOFWEEK(DATE_ACTUAL) AS dow,
           COALESCE(REVENUE_XOLA_LBS,0) + COALESCE(REVENUE_REVEL_LBS,0) AS total_rev
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
      AND DATE_ACTUAL BETWEEN DATEADD(day,-63,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
), w AS (
    SELECT *, AVG(total_rev) OVER (PARTITION BY DIM_STORES_MASTER_ID, dow
                                   ORDER BY DATE_ACTUAL ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_rev
    FROM d
)
SELECT LBS_NAME, CONCEPT, DATE_ACTUAL, ROUND(total_rev) AS total_rev, ROUND(baseline_rev) AS baseline_rev,
       CASE WHEN baseline_rev IS NULL OR baseline_rev = 0 THEN 'N/A'
            WHEN total_rev = 0                             THEN 'ZERO'
            WHEN total_rev / baseline_rev < 0.4            THEN 'LOW'
            WHEN total_rev / baseline_rev > 2.5            THEN 'HIGH'
            ELSE 'OK' END AS status
FROM w
WHERE DATE_ACTUAL >= DATEADD(day,-28,CURRENT_DATE)
ORDER BY LBS_NAME, DATE_ACTUAL;


-- -----------------------------------------------------------------------------
-- CHECK 5: "WHAT'S WRONG YESTERDAY" LIST  (drill-down table next to the heatmap)
-- Same logic as Check 4, but only yesterday's offenders, sorted worst first.
-- -----------------------------------------------------------------------------
WITH d AS (
    SELECT DIM_STORES_MASTER_ID, LBS_NAME, CONCEPT, DATE_ACTUAL,
           COALESCE(REVENUE_XOLA_LBS,0) + COALESCE(REVENUE_REVEL_LBS,0) AS total_rev
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
      AND DATE_ACTUAL BETWEEN DATEADD(day,-36,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
), y AS (SELECT * FROM d WHERE DATE_ACTUAL = DATEADD(day,-1,CURRENT_DATE)),
b AS (
    SELECT DIM_STORES_MASTER_ID, AVG(total_rev) AS baseline_rev
    FROM d
    WHERE DATE_ACTUAL < DATEADD(day,-1,CURRENT_DATE)
      AND DAYOFWEEK(DATE_ACTUAL) = DAYOFWEEK(DATEADD(day,-1,CURRENT_DATE))
    GROUP BY 1
)
SELECT y.LBS_NAME, y.CONCEPT, ROUND(y.total_rev) AS yesterday_rev, ROUND(b.baseline_rev) AS same_weekday_avg,
       ROUND(y.total_rev / NULLIF(b.baseline_rev,0), 2) AS ratio
FROM y JOIN b USING (DIM_STORES_MASTER_ID)
WHERE b.baseline_rev > 0 AND y.total_rev / b.baseline_rev < 0.4
ORDER BY ratio;


-- -----------------------------------------------------------------------------
-- CHECK 6: UNATTRIBUTED / UNKNOWN REVENUE  (daily share + offending stores)
-- Catches: products missing from DIM_SALES_PRODUCTS falling into the unknown bucket
-- Visual: line chart of unknown share by day (6a); table of stores with unknown $ (6b)
-- -----------------------------------------------------------------------------
-- 6a. daily share of Revel revenue that is unknown (last 30 days)
SELECT DATE_ACTUAL,
       ROUND(SUM(UNKNOWN_REVEL_REVENUE_LBS), 2)                                         AS unknown_rev,
       ROUND(SUM(UNKNOWN_REVEL_REVENUE_LBS) / NULLIF(SUM(REVENUE_REVEL_LBS),0) * 100, 2) AS unknown_pct_of_revel
FROM TEG_PROD.DW.AGG_STORES_DAY
WHERE DATE_ACTUAL BETWEEN DATEADD(day,-30,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
  AND STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
GROUP BY 1 ORDER BY 1;

-- 6b. which stores (last 7 days)
SELECT LBS_NAME, ROUND(SUM(UNKNOWN_REVEL_REVENUE_LBS),2) AS unknown_rev, SUM(UNKNOWN_REVEL_REVENUE_LBS_QTY) AS unknown_qty
FROM TEG_PROD.DW.AGG_STORES_DAY
WHERE DATE_ACTUAL BETWEEN DATEADD(day,-7,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
  AND STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
GROUP BY 1 HAVING SUM(UNKNOWN_REVEL_REVENUE_LBS) > 0
ORDER BY unknown_rev DESC;


-- -----------------------------------------------------------------------------
-- CHECK 7: RECONCILIATION  bookings (net paid) vs AGG_STORES_DAY.REVENUE_XOLA_LBS
-- Ratio normally sits ~0.91-0.97; alert on DRIFT from the trailing 28-day average.
-- Catches: refund-netting regressions, duplicated or dropped bookings, join fan-out
-- Visual: line chart of ratio with a +/- 5pt band
-- -----------------------------------------------------------------------------
WITH scope_stores AS (
    SELECT DISTINCT DIM_STORES_MASTER_ID FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
), f AS (
    SELECT ARRIVAL_DATE::DATE AS d,
           SUM(AMOUNT_PAID - COALESCE(PAYMENT_REFUND_AMOUNT,0)) AS paid_net,
           COUNT(DISTINCT X_BOOKING_ID)                         AS bookings
    FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS
    WHERE ARRIVAL_DATE::DATE BETWEEN DATEADD(day,-35,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
      AND COALESCE(IS_DEMO_TRAINING,0) = 0
      AND REVENUE_STORE_ID IN (SELECT DIM_STORES_MASTER_ID FROM scope_stores)
    GROUP BY 1
), a AS (
    SELECT DATE_ACTUAL AS d, SUM(REVENUE_XOLA_LBS) AS agg_rev
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE DATE_ACTUAL BETWEEN DATEADD(day,-35,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
      AND DIM_STORES_MASTER_ID IN (SELECT DIM_STORES_MASTER_ID FROM scope_stores)
    GROUP BY 1
), r AS (
    SELECT f.d, ROUND(f.paid_net) AS fct_paid_net, ROUND(a.agg_rev) AS agg_rev,
           a.agg_rev / NULLIF(f.paid_net,0) AS ratio
    FROM f JOIN a USING (d)
), w AS (
    SELECT *, AVG(ratio) OVER (ORDER BY d ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING) AS trailing_ratio
    FROM r
)
SELECT d, fct_paid_net, agg_rev, ROUND(ratio,3) AS ratio, ROUND(trailing_ratio,3) AS trailing_ratio,
       ROUND(ratio - trailing_ratio, 3) AS drift,
       -- normal day-to-day drift in testing reached about +/-4 pts, so 3 pts was too tight
       CASE WHEN ABS(ratio - trailing_ratio) > 0.08 THEN 'RED'
            WHEN ABS(ratio - trailing_ratio) > 0.05 THEN 'YELLOW' ELSE 'GREEN' END AS status
FROM w
WHERE d >= DATEADD(day,-14,CURRENT_DATE)
ORDER BY d;


-- -----------------------------------------------------------------------------
-- CHECK 8: BOOKINGS INTEGRITY  (last 30 days of bookings created)
-- Visual: KPI cards, each with a count (green at 0) and a drill-down list
-- -----------------------------------------------------------------------------
SELECT
    COUNT(*)                                                              AS rows_checked,
    COUNT(*) - COUNT(DISTINCT b.ID)                                       AS duplicate_ids,
    COUNT_IF(b.STORE_ID IS NULL AND b.REVENUE_STORE_ID IS NULL)           AS null_store,
    COUNT_IF(b.AMOUNT_PAID IS NULL)                                       AS null_amount_paid,
    COUNT_IF(b.AMOUNT_PAID > 0 AND COALESCE(b.PAYMENT_REFUND_AMOUNT,0) > b.AMOUNT_PAID) AS refund_exceeds_paid,
    COUNT_IF(d.ID IS NULL)                                                AS orphan_store_rows   -- store key not in DIM_STORES_MASTER
FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS b
LEFT JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = b.REVENUE_STORE_ID
WHERE b.BOOKINGS_CREATED_ON >= DATEADD(day,-30,CURRENT_DATE);

-- 8b. drill-down: orphan store keys
SELECT b.REVENUE_STORE_ID, COUNT(*) AS rows_affected, MIN(b.BOOKINGS_CREATED_ON) AS first_seen, MAX(b.BOOKINGS_CREATED_ON) AS last_seen
FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS b
LEFT JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = b.REVENUE_STORE_ID
WHERE b.BOOKINGS_CREATED_ON >= DATEADD(day,-30,CURRENT_DATE) AND d.ID IS NULL
GROUP BY 1;


-- -----------------------------------------------------------------------------
-- CHECK 9: AGG_STORES_DAY INTEGRITY
-- Visual: KPI cards
-- -----------------------------------------------------------------------------
SELECT
    (SELECT COUNT(*) FROM (SELECT DIM_STORES_MASTER_ID, DATE_ACTUAL
                           FROM TEG_PROD.DW.AGG_STORES_DAY
                           GROUP BY 1,2 HAVING COUNT(*) > 1))                 AS duplicate_store_days,
    (SELECT COUNT(*) FROM TEG_PROD.DW.AGG_STORES_DAY
     WHERE DATE_ACTUAL BETWEEN DATEADD(day,-7,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
       AND STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
       AND REVENUE_XOLA_LBS IS NULL AND REVENUE_REVEL_LBS IS NULL)            AS null_revenue_rows_7d;


-- -----------------------------------------------------------------------------
-- CHECK 10: REVIEW COVERAGE MATRIX  (store x platform), driven by the mapping table
-- Catches: "why no TripAdvisor/Yelp reviews for PCB" before anyone asks, and tells you
-- WHICH kind of problem it is, because the fix differs:
--   MISSING_MAPPING  no usable row in the mapping sheet for this store (or blank platform ID)  -> add the row
--   TICKER_MISMATCH  a mapping row exists but its TICKER_FIN differs from dim_stores_master
--                    in case/whitespace (e.g. GBGSOAKB vs GBGSOakB), so it never joins          -> fix the ticker
--   MAPPED_NO_DATA   mapped correctly but no review has ever landed                             -> check the platform ID / scraper
--   FEED_STALE       mapped, but zero reviews in 30 days where the prior pace was 3+ per 30     -> feed or scraper problem
--   OK
-- Mapping source: TEG_PROD.STAGE.STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING
--   (columns: MAPPING_SOURCE = 'TripAdvisor'|'Yelp', REVIEW_STORE_ID = platform ID, TICKER_FIN, WEBSITE;
--    TripAdvisor can have several rows per store, one per product listing).
-- Google has no row in that table; it is treated as mapped when GOOGLE_PLACES_ID is populated.
-- Visual: matrix, rows = stores, columns = google / tripadvisor / yelp, cell = status.
-- Returns every store x platform cell; the dashboard filters to non-OK for the alert list.
-- -----------------------------------------------------------------------------
WITH scope_stores AS (
    SELECT DIM_STORES_MASTER_ID, MAX(LBS_NAME) AS LBS_NAME, MAX(TICKER_FIN) AS TICKER_FIN,
           MAX(NULLIF(TRIM(GOOGLE_PLACES_ID),'')) AS google_places_id
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
    GROUP BY 1
), plat AS (
    SELECT column1 AS review_platform, column2 AS mapping_source
    FROM VALUES ('google', NULL), ('tripadvisor','TripAdvisor'), ('yelp','Yelp')
), m_ex AS (   -- mapping rows that have a platform ID
    SELECT DISTINCT TICKER_FIN, MAPPING_SOURCE
    FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING
    WHERE NULLIF(TRIM(REVIEW_STORE_ID),'') IS NOT NULL
), m_lo AS (   -- same rows with the ticker normalised, to detect case/whitespace mismatches
    SELECT DISTINCT UPPER(TRIM(TICKER_FIN)) AS tk_norm, MAPPING_SOURCE FROM m_ex
), r AS (
    SELECT DIM_STORES_MASTER_ID, REVIEW_PLATFORM,
           MAX(REVIEW_DATE) AS last_review,
           COUNT_IF(REVIEW_DATE >= DATEADD(day,-30,CURRENT_DATE)) AS n_30d,
           COUNT_IF(REVIEW_DATE BETWEEN DATEADD(day,-120,CURRENT_DATE) AND DATEADD(day,-31,CURRENT_DATE)) / 3.0 AS avg_30d_prior
    FROM TEG_PROD.DW.FCT_STORES_ALL_REVIEWS
    GROUP BY 1,2
), j AS (
    SELECT s.LBS_NAME, s.TICKER_FIN, p.review_platform, r.last_review, COALESCE(r.n_30d,0) AS n_30d, r.avg_30d_prior,
           CASE WHEN p.review_platform = 'google' THEN IFF(s.google_places_id IS NULL, 'NONE', 'EXACT')
                WHEN m_ex.TICKER_FIN IS NOT NULL    THEN 'EXACT'
                WHEN m_lo.tk_norm IS NOT NULL       THEN 'LOOSE'
                ELSE 'NONE' END AS mapped_state
    FROM scope_stores s CROSS JOIN plat p
    LEFT JOIN m_ex ON m_ex.TICKER_FIN = s.TICKER_FIN AND m_ex.MAPPING_SOURCE = p.mapping_source
    LEFT JOIN m_lo ON m_lo.tk_norm = UPPER(TRIM(s.TICKER_FIN)) AND m_lo.MAPPING_SOURCE = p.mapping_source
    LEFT JOIN r ON r.DIM_STORES_MASTER_ID = s.DIM_STORES_MASTER_ID AND r.REVIEW_PLATFORM = p.review_platform
)
SELECT LBS_NAME, TICKER_FIN, review_platform, last_review,
       DATEDIFF(day, last_review, CURRENT_DATE) AS days_since, n_30d, ROUND(avg_30d_prior,1) AS prior_avg_30d,
       CASE WHEN mapped_state = 'NONE'  THEN 'MISSING_MAPPING'
            WHEN mapped_state = 'LOOSE' THEN 'TICKER_MISMATCH'
            WHEN last_review IS NULL    THEN 'MAPPED_NO_DATA'
            WHEN n_30d = 0 AND avg_30d_prior >= 3 THEN 'FEED_STALE'
            ELSE 'OK' END AS status
FROM j
ORDER BY status, LBS_NAME, review_platform;


-- -----------------------------------------------------------------------------
-- CHECK 10b: MAPPING ROWS THAT DON'T JOIN TO ANY STORE  (the mapping sheet itself)
-- Catches typos and case mismatches in TICKER_FIN, plus rows with a blank platform ID.
-- Visual: small table under the review matrix; any row here is a problem.
-- -----------------------------------------------------------------------------
WITH dim_t AS (SELECT DISTINCT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER)
SELECT m.MAPPING_SOURCE, m.TICKER_FIN AS mapping_ticker, m.REVIEW_STORE_ID, m.WEBSITE,
       CASE WHEN NULLIF(TRIM(m.REVIEW_STORE_ID),'') IS NULL THEN 'BLANK PLATFORM ID'
            WHEN EXISTS (SELECT 1 FROM dim_t d WHERE UPPER(TRIM(d.TICKER_FIN)) = UPPER(TRIM(m.TICKER_FIN))) THEN 'CASE/WHITESPACE MISMATCH'
            ELSE 'NO MATCHING TICKER' END AS problem,
       (SELECT MIN(d.TICKER_FIN) FROM dim_t d WHERE UPPER(TRIM(d.TICKER_FIN)) = UPPER(TRIM(m.TICKER_FIN))) AS dim_ticker
FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING m
WHERE NULLIF(TRIM(m.REVIEW_STORE_ID),'') IS NULL
   OR NOT EXISTS (SELECT 1 FROM dim_t d WHERE d.TICKER_FIN = m.TICKER_FIN)
ORDER BY problem, m.MAPPING_SOURCE, m.TICKER_FIN;


-- -----------------------------------------------------------------------------
-- CHECK 11: UNMAPPED REVIEWS / SURVEYS  (rows that landed but have no store)
-- Catches: the other half of a mapping problem; data exists but joins to nothing
-- Visual: KPI per source (click a source to open its drill-down: 11b for reviews, 11c for NPS)
-- Network-wide on purpose (an unmapped row has no store to scope on).
-- -----------------------------------------------------------------------------
SELECT 'review:' || REVIEW_PLATFORM AS source,
       COUNT(*)                                        AS total_30d,
       COUNT_IF(DIM_STORES_MASTER_ID IS NULL)          AS unmapped_30d,
       ROUND(COUNT_IF(DIM_STORES_MASTER_ID IS NULL) / COUNT(*) * 100, 1) AS unmapped_pct
FROM TEG_PROD.DW.FCT_STORES_ALL_REVIEWS
WHERE REVIEW_DATE >= DATEADD(day,-30,CURRENT_DATE)
GROUP BY REVIEW_PLATFORM
UNION ALL
SELECT 'nps:' || COALESCE(SOURCE,'(null)'),
       COUNT(*), COUNT_IF(DIM_STORES_MASTER_ID IS NULL),
       ROUND(COUNT_IF(DIM_STORES_MASTER_ID IS NULL) / COUNT(*) * 100, 1)
FROM TEG_PROD.DW.FCT_NPS_DETAILS
WHERE SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE)
GROUP BY COALESCE(SOURCE,'(null)')
ORDER BY unmapped_pct DESC;

-- 11b. DRILL-DOWN: unmapped TripAdvisor / Yelp reviews, grouped by the listing they came from.
-- Each row shows the platform listing (name, ID, URL) so you can tell which store it belongs to,
-- plus a DIAGNOSIS from looking that listing ID up in the mapping sheet:
--   ID NOT IN MAPPING            -> add a row
--   ID IN MAPPING, ticker differs -> fix the ticker (case/typo), see Check 10b
--   ID IN MAPPING, ticker matches -> look at the downstream join
-- Mapping IDs: TripAdvisor = TRIPADVISOR_LOCATION_ID; Yelp = SERPAPI_PLACE_ID (NOT YELP_BUSINESS_ID,
-- which holds the URL alias, e.g. great-big-game-show-oak-brook-4).
-- Add a WHERE on review_platform to power the per-source click-through.
WITH u AS (
  SELECT 'tripadvisor' AS review_platform, 'TripAdvisor' AS mapping_source, SOURCE_BRAND::STRING AS source_brand,
         TRIPADVISOR_LOCATION_ID::STRING AS platform_id, TRIPADVISOR_STORE_NAME::STRING AS platform_name,
         MIN(TRIPADVISOR_URL)::STRING AS url, COUNT(*) AS reviews, MAX(TRIPADVISOR_REVIEW_DATE) AS latest_review
  FROM TEG_PROD.DW.FCT_STORES_TRIPADVISOR_REVIEWS
  WHERE DIM_STORES_MASTER_ID IS NULL AND TRIPADVISOR_REVIEW_DATE >= DATEADD(day,-30,CURRENT_DATE)
  GROUP BY 1,2,3,4,5
  UNION ALL
  SELECT 'yelp', 'Yelp', SOURCE_BRAND::STRING, SERPAPI_PLACE_ID::STRING, YELP_STORE_NAME::STRING,
         MIN(YELP_URL)::STRING, COUNT(*), MAX(YELP_REVIEW_DATE)
  FROM TEG_PROD.DW.FCT_STORES_YELP_REVIEWS
  WHERE DIM_STORES_MASTER_ID IS NULL AND YELP_REVIEW_DATE >= DATEADD(day,-30,CURRENT_DATE)
  GROUP BY 1,2,3,4,5
)
SELECT u.review_platform, u.source_brand, u.platform_name, u.platform_id, u.url, u.reviews, u.latest_review,
       MAX(m.TICKER_FIN) AS ticker_in_mapping,
       CASE WHEN COUNT(m.TICKER_FIN) = 0 THEN 'ID NOT IN MAPPING: add a row'
            WHEN MAX(d.TICKER_FIN) IS NULL THEN 'ID IN MAPPING but ticker does not match dim_stores_master (case/typo)'
            ELSE 'ID IN MAPPING and ticker matches: check downstream join' END AS diagnosis
FROM u
LEFT JOIN TEG_PROD.STAGE.STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING m
       ON m.MAPPING_SOURCE = u.mapping_source AND m.REVIEW_STORE_ID = u.platform_id
LEFT JOIN (SELECT DISTINCT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER) d ON d.TICKER_FIN = m.TICKER_FIN
GROUP BY 1,2,3,4,5,6,7
ORDER BY u.reviews DESC;

-- 11c. DRILL-DOWN: unmapped NPS responses, grouped by the location text the survey captured.
-- A (null) location means the respondent never had a last-location value, which is a data-capture
-- problem rather than a missing mapping.
SELECT SOURCE, COALESCE(LAST_LOCATION_PLAYED,'(null)') AS last_location_played,
       COALESCE(LAST_GBGS_LOCATION_PLAYED,'(null)') AS last_gbgs_location_played,
       COALESCE(LBS_NAME,'(null)') AS lbs_name,
       COUNT(*) AS responses, MIN(SURVEY_DATE)::DATE AS first_seen, MAX(SURVEY_DATE)::DATE AS last_seen
FROM TEG_PROD.DW.FCT_NPS_DETAILS
WHERE DIM_STORES_MASTER_ID IS NULL AND SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE)
GROUP BY 1,2,3,4
ORDER BY responses DESC;


-- -----------------------------------------------------------------------------
-- CHECK 12: NPS HEALTH  (response volume by store + validity)
-- Catches: a store's survey feed stopping, out-of-range scores, duplicate responses
-- Visual: heatmap of weekly responses by store (12a) + KPI cards (12b)
-- -----------------------------------------------------------------------------
-- 12a. weekly response counts per active store, last 10 weeks, with trailing-avg flag
WITH wk AS (
    SELECT s.LBS_NAME, DATE_TRUNC('week', n.SURVEY_DATE)::DATE AS week_start, COUNT(*) AS responses
    FROM TEG_PROD.DW.FCT_NPS_DETAILS n
    JOIN (SELECT DISTINCT DIM_STORES_MASTER_ID, LBS_NAME FROM TEG_PROD.DW.AGG_STORES_DAY
          WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')) s
      ON s.DIM_STORES_MASTER_ID = n.DIM_STORES_MASTER_ID
    WHERE n.SURVEY_DATE >= DATEADD(week,-10,CURRENT_DATE)
    GROUP BY 1,2
)
SELECT *, ROUND(AVG(responses) OVER (PARTITION BY LBS_NAME ORDER BY week_start ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING),1) AS trailing_avg
FROM wk
ORDER BY LBS_NAME, week_start;

-- 12b. validity (last 30 days)
SELECT COUNT(*)                                       AS rows_checked,
       COUNT(*) - COUNT(DISTINCT ID)                  AS duplicate_ids,
       COUNT_IF(NPS_RATING IS NULL)                   AS null_rating,
       COUNT_IF(NPS_RATING < 0 OR NPS_RATING > 10)    AS out_of_range,
       COUNT_IF(DIM_STORES_MASTER_ID IS NULL)         AS unmapped_store
FROM TEG_PROD.DW.FCT_NPS_DETAILS
WHERE SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE);


-- -----------------------------------------------------------------------------
-- CHECK 13: DIMENSION DRIFT  (active stores with no recent data in a fact)
-- Catches: newly opened/renamed stores that never flowed downstream
-- Visual: "stores in dim but missing from fact" list, per fact table
-- -----------------------------------------------------------------------------
SELECT d.LBS_NAME, d.STORE_STATUS, d.CONCEPT,
       COALESCE(b.bookings_14d,0) AS bookings_14d,
       COALESCE(n.nps_30d,0)      AS nps_responses_30d
FROM (SELECT DISTINCT DIM_STORES_MASTER_ID, LBS_NAME, STORE_STATUS, CONCEPT
      FROM TEG_PROD.DW.AGG_STORES_DAY
      WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')) d
LEFT JOIN (SELECT REVENUE_STORE_ID AS id, COUNT(*) AS bookings_14d
           FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS
           WHERE ARRIVAL_DATE::DATE BETWEEN DATEADD(day,-14,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
           GROUP BY 1) b ON b.id = d.DIM_STORES_MASTER_ID
LEFT JOIN (SELECT DIM_STORES_MASTER_ID AS id, COUNT(*) AS nps_30d
           FROM TEG_PROD.DW.FCT_NPS_DETAILS
           WHERE SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE)
           GROUP BY 1) n ON n.id = d.DIM_STORES_MASTER_ID
WHERE COALESCE(b.bookings_14d,0) = 0 OR COALESCE(n.nps_30d,0) = 0
ORDER BY d.LBS_NAME;


-- -----------------------------------------------------------------------------
-- CHECK 14: NEXT-MONTH TARGETS CHECKLIST  (store x target type)
-- Catches: targets that haven't been loaded for the coming month, per store, per target type
-- Visual: "Targets" tab. One card per target type ("N / M stores loaded"), and a store x type matrix.
--   LOADED          a positive target exists for the store in the target month
--   MISSING         none for the target month, but the store HAD one in the current month
--   MISSING_NEVER   none for the target month OR the current month (store may be missing from the sheet)
--   N/A             target type doesn't apply to the store's concept (see applies_to below)
-- Change target_month to look at any other month, e.g. DATE '2026-12-01'.
-- Sources (all keyed by store ticker; tickers are compared UPPER(TRIM()) because the review mapping
-- sheet showed case drift):
--   REPORTING.LBS_KEYS__DAILY_VOL_TARGETS / DAILY_REV_TARGETS / MONTHLY_REV_TARGETS  (DATE = 'YYYYMMDD' text, IRL_TARGET)
--   REPORTING.LBS_KEYS__DAILY_STAFFING_TARGETS                                       (keyed on the 1st of each month)
--   REPORTING.TARGETS_LOADING__*  (from the 2025 targets loading sheet; MONTH_DATE_KEY = YYYYMMDD of the 1st)
-- These are the same columns AGG_STORES_DAY exposes (KEYS_DAILY_VOL_IRL_TARGET, KEYS_DAILY_REV_IRL_TARGET,
-- KEYS_MONTHLY_REV_IRL_TARGET, KEYS_DAILY_STAFFING_TARGET, STAFFING_EFFICIENCY_TARGET, STAFF_BUDGET,
-- STAFF_HPG_TARGET, STAFF_HPG_BUDGET). STAFFING_BUDGET and STAFF_HPG_TRENDLINE are in the loading sheet
-- but not in AGG_STORES_DAY.
-- ASSUMPTION to confirm: Staff budget and Staff HPG budget are only loaded for TEG stores (59 of 63 TEG,
-- 0 GBGS in every month checked), so they are treated as N/A for GBGS.
-- -----------------------------------------------------------------------------
WITH params AS (
    SELECT DATE_TRUNC('month', DATEADD(month, 1, CURRENT_DATE)) AS target_month,
           DATE_TRUNC('month', CURRENT_DATE)                    AS prior_month
), scope_stores AS (
    SELECT DIM_STORES_MASTER_ID, MAX(LBS_NAME) AS LBS_NAME, MAX(CONCEPT) AS CONCEPT, MAX(TICKER_FIN) AS TICKER_FIN
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
    GROUP BY 1
), loaded AS (
    SELECT 'Daily volume target' AS target_type, UPPER(TRIM(STORE_TICKER)) AS tk, DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD')) AS mth
      FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_VOL_TARGETS WHERE IRL_TARGET > 0
    UNION SELECT 'Daily revenue target', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_REV_TARGETS WHERE IRL_TARGET > 0
    UNION SELECT 'Monthly revenue target', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.LBS_KEYS__MONTHLY_REV_TARGETS WHERE IRL_TARGET > 0
    UNION SELECT 'Daily staffing target', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_STAFFING_TARGETS WHERE STAFFING_TARGET > 0
    UNION SELECT 'Staffing efficiency target', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_EFF_TARGETS WHERE STAFFING_EFFICIENCY_TARGET > 0
    UNION SELECT 'Staff budget', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_BUDGETS WHERE STAFF_BUDGET > 0
    UNION SELECT 'Staffing budget', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_BUDGET WHERE STAFFING_BUDGET > 0
    UNION SELECT 'Staff HPG target', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_TARGET WHERE STAFF_HPG_TARGET > 0
    UNION SELECT 'Staff HPG budget', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_BUDGET WHERE STAFF_HPG_BUDGET > 0
    UNION SELECT 'Staff HPG trendline', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_TRENDLINE WHERE STAFF_HPG_TRENDLINE > 0
), types AS (
    SELECT DISTINCT target_type,
           IFF(target_type IN ('Staff budget','Staff HPG budget'), 'TEG', 'BOTH') AS applies_to
    FROM loaded
)
SELECT s.LBS_NAME, s.CONCEPT, s.TICKER_FIN, t.target_type, t.applies_to,
       CASE WHEN t.applies_to = 'TEG' AND s.CONCEPT <> 'TEG' THEN 'N/A'
            WHEN n.tk IS NOT NULL THEN 'LOADED'
            WHEN c.tk IS NOT NULL THEN 'MISSING'
            ELSE 'MISSING_NEVER' END AS status
FROM scope_stores s CROSS JOIN types t CROSS JOIN params p
LEFT JOIN loaded n ON n.target_type = t.target_type AND n.tk = UPPER(TRIM(s.TICKER_FIN)) AND n.mth = p.target_month
LEFT JOIN loaded c ON c.target_type = t.target_type AND c.tk = UPPER(TRIM(s.TICKER_FIN)) AND c.mth = p.prior_month
ORDER BY t.target_type, status, s.LBS_NAME;

-- 14b. Per-type rollup for the cards: wrap Check 14 as a CTE named m and run
--   SELECT target_type, COUNT_IF(status <> 'N/A') AS applicable_stores, COUNT_IF(status = 'LOADED') AS loaded,
--          COUNT_IF(status = 'MISSING') AS missing_had_prior, COUNT_IF(status = 'MISSING_NEVER') AS missing_never
--   FROM m GROUP BY 1 ORDER BY 1;
-- Severity suggestion: a type that is not 100% loaded is yellow until 7 days before the month starts,
-- then red (and stays red once the month has started).

-- -----------------------------------------------------------------------------
-- CHECK 14c: TARGET ROWS WHOSE TICKER ISN'T IN dim_stores_master  (informational)
-- Typos, placeholders (TEMP...) or stores that haven't opened yet. Useful for spotting targets
-- loaded under the wrong ticker.
-- -----------------------------------------------------------------------------
WITH dim_t AS (SELECT DISTINCT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE TICKER_FIN IS NOT NULL),
l AS (
    SELECT 'Monthly revenue target' AS target_type, STORE_TICKER FROM TEG_PROD.REPORTING.LBS_KEYS__MONTHLY_REV_TARGETS WHERE DATE >= TO_CHAR(DATE_TRUNC('month', CURRENT_DATE),'YYYYMMDD')
    UNION ALL SELECT 'Staffing budget', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_BUDGET WHERE MONTH_DATE_KEY >= TO_NUMBER(TO_CHAR(DATE_TRUNC('month', CURRENT_DATE),'YYYYMMDD'))
    UNION ALL SELECT 'Staff budget', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_BUDGETS WHERE MONTH_DATE_KEY >= TO_NUMBER(TO_CHAR(DATE_TRUNC('month', CURRENT_DATE),'YYYYMMDD'))
    UNION ALL SELECT 'Staff HPG budget', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_BUDGET WHERE MONTH_DATE_KEY >= TO_NUMBER(TO_CHAR(DATE_TRUNC('month', CURRENT_DATE),'YYYYMMDD'))
    UNION ALL SELECT 'Daily volume target', STORE_TICKER FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_VOL_TARGETS WHERE DATE >= TO_CHAR(DATE_TRUNC('month', CURRENT_DATE),'YYYYMMDD')
    UNION ALL SELECT 'Staffing efficiency target', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_EFF_TARGETS WHERE MONTH_DATE_KEY >= TO_NUMBER(TO_CHAR(DATE_TRUNC('month', CURRENT_DATE),'YYYYMMDD'))
)
SELECT target_type, STORE_TICKER, COUNT(*) AS rows_
FROM l
WHERE STORE_TICKER NOT IN (SELECT TICKER_FIN FROM dim_t)
GROUP BY 1,2
ORDER BY 1,2;


-- -----------------------------------------------------------------------------
-- CHECK 15: STORE SETUP, bookings or Xola sellers not matched to a row in the store sheet
-- The store sheet ("2024 LBS Keys - Store Team Info") feeds DIM_STORES_MASTER. A new location shows up
-- in Xola first (a new seller), often takes bookings before anyone has added its x_seller_id to the sheet.
-- Seller IDs live in three dim columns, so all three are checked: SELLER_ID, XOLA_SELLER_ID and
-- _GBGS_XOLA_SELLER_ID (GBGS stores usually share the TEG seller of the same city, or carry their own).
-- Source of truth for "what sellers exist": TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE (one row per Xola seller).
-- Visual: Integrity tab panel + a Data Central tile. Network-wide on purpose (a new store isn't in scope yet).
-- -----------------------------------------------------------------------------
-- 15a. Xola sellers that are not in DIM_STORES_MASTER, with any bookings already landing for them.
--      RED = taking bookings now; YELLOW = exists in Xola but no bookings yet.
--      possible_sheet_row suggests the unfilled sheet row to put the ID on, by matching the city name
--      against sheet rows that have no seller ID (heuristic; confirm before pasting).
WITH dim_ids AS (
  SELECT SELLER_ID AS sid FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE SELLER_ID IS NOT NULL
  UNION SELECT XOLA_SELLER_ID FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE XOLA_SELLER_ID IS NOT NULL
  UNION SELECT _GBGS_XOLA_SELLER_ID FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE _GBGS_XOLA_SELLER_ID IS NOT NULL
), bk AS (
  SELECT X_SELLER_ID AS sid, COUNT(*) AS booking_rows, MIN(BOOKINGS_CREATED_ON)::DATE AS first_created, MAX(BOOKINGS_CREATED_ON)::DATE AS last_created,
         MIN(ARRIVAL_DATE)::DATE AS first_arrival, ROUND(SUM(AMOUNT_PAID)) AS paid
  FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS WHERE X_SELLER_ID IS NOT NULL GROUP BY 1
), open_rows AS (   -- sheet rows that still have no seller ID
  SELECT LBS_NAME, XOLA_STORE_ID, REVEL_STORE_ID FROM TEG_PROD.DW.DIM_STORES_MASTER
  WHERE COALESCE(NULLIF(XOLA_SELLER_ID,''), NULLIF(_GBGS_XOLA_SELLER_ID,'')) IS NULL AND NULLIF(TRIM(LBS_NAME),'') IS NOT NULL
)
SELECT s.SELLER_ID AS x_seller_id, s.SELLER_NAME, s.CITY, s.STATE, REPLACE(s.STREET_ADDRESS, CHR(10), ', ') AS street_address, s.IS_ENABLED,
       s.SOURCE_CREATE_DATE::DATE AS created_in_xola,
       COALESCE(bk.booking_rows,0) AS booking_rows, bk.first_created, bk.last_created, bk.first_arrival, COALESCE(bk.paid,0) AS paid,
       LISTAGG(DISTINCT o.LBS_NAME || ' (xola_store_id ' || COALESCE(o.XOLA_STORE_ID::STRING,'blank') || ')', '; ') AS possible_sheet_row,
       CASE WHEN COALESCE(bk.booking_rows,0) > 0 THEN 'RED' ELSE 'YELLOW' END AS severity
FROM TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE s
LEFT JOIN bk ON bk.sid = s.SELLER_ID
LEFT JOIN open_rows o ON POSITION(LOWER(TRIM(REPLACE(o.LBS_NAME,'GBGS ',''))), LOWER(s.SELLER_NAME)) > 0
                     AND (s.SELLER_NAME ILIKE '%Great Big Game Show%') = (o.LBS_NAME ILIKE 'GBGS%')
WHERE s.SELLER_ID NOT IN (SELECT sid FROM dim_ids)
GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12
ORDER BY booking_rows DESC, created_in_xola DESC;

-- 15b. Seller IDs on recent bookings that match neither the dim nor the Xola seller table (30 days created).
--      Normally empty. Bookings with a NULL X_SELLER_ID are excluded: that is a legacy source (93,884 rows,
--      last created 2026-06-12) and not a new store.
SELECT b.X_SELLER_ID, COUNT(*) AS booking_rows, MIN(b.BOOKINGS_CREATED_ON)::DATE AS first_created, MAX(b.BOOKINGS_CREATED_ON)::DATE AS last_created, ROUND(SUM(b.AMOUNT_PAID)) AS paid
FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS b
WHERE b.BOOKINGS_CREATED_ON >= DATEADD(day,-30,CURRENT_DATE) AND b.X_SELLER_ID IS NOT NULL
  AND b.X_SELLER_ID NOT IN (SELECT SELLER_ID FROM TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE)
GROUP BY 1
ORDER BY booking_rows DESC;

-- 15c. Sheet rows that are still placeholders (no concept, ticker UNK or no seller ID) but whose store is
--      already taking bookings in the last 60 days. The row below is the "flagged with bookings" list;
--      the placeholder count underneath is context for the Data Central tile.
WITH d AS (
  SELECT ID, LBS_NAME, "_CONCEPT" AS concept, "_STORE_STATUS" AS status, TICKER_FIN,
         COALESCE(NULLIF(XOLA_SELLER_ID,''), NULLIF("_GBGS_XOLA_SELLER_ID",'')) AS any_seller
  FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE NULLIF(TRIM(LBS_NAME),'') IS NOT NULL
), b AS (
  SELECT REVENUE_STORE_ID AS id, COUNT(*) AS booking_rows, MAX(BOOKINGS_CREATED_ON)::DATE AS last_created, MIN(ARRIVAL_DATE)::DATE AS first_arrival
  FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS WHERE BOOKINGS_CREATED_ON >= DATEADD(day,-60,CURRENT_DATE) GROUP BY 1
)
SELECT d.LBS_NAME, d.concept, d.status, d.TICKER_FIN, b.booking_rows, b.last_created, b.first_arrival,
       ARRAY_TO_STRING(ARRAY_CONSTRUCT_COMPACT(IFF(d.concept IS NULL,'concept',NULL), IFF(d.status IS NULL,'status',NULL),
         IFF(d.TICKER_FIN IS NULL OR d.TICKER_FIN='UNK','ticker',NULL), IFF(d.any_seller IS NULL,'seller_id',NULL)), ', ') AS missing
FROM d JOIN b ON b.id = d.ID
WHERE d.concept IS NULL OR d.status IS NULL OR d.TICKER_FIN IS NULL OR d.TICKER_FIN = 'UNK' OR d.any_seller IS NULL
ORDER BY b.booking_rows DESC;
-- placeholder count: SELECT COUNT(*) FROM d WHERE concept IS NULL OR TICKER_FIN IS NULL OR TICKER_FIN = 'UNK' OR any_seller IS NULL;


-- -----------------------------------------------------------------------------
-- CHECK 16: SUGGESTED STORE-SHEET VALUES for sellers that aren't in the sheet yet
-- For each Xola seller from Check 15a, proposes values for the 2024 LBS Keys sheet, one row per field, with the
-- basis for each so you can decide what to trust. Visual: "Suggested values for the <store> row" panel on the
-- Integrity tab (click cells, copy, paste into the sheet) and the "Sheet fill-in suggestions" Data Central tile.
-- Confidence used in the dashboard: "xola" = taken from the seller record; "pattern" = follows the sheet's pattern
-- in the most recent openings; "confirm" = reasonable guess, check before pasting.
-- Things this deliberately does NOT suggest: lat/long (Xola's coordinates are city-level, about 4 km off for
-- Fulton; use the Google Places lookup), _google_places_id, _GM, _ADP Location, slots, game mix, pay.
-- Assumptions: next store number = highest numeric _STORE_ORDER for the concept + 1; the neighbour store is the most
-- recently opened store of the same concept in the same state; the ticker comes from the staffing budget sheet
-- (a ticker that starts the city name and no store uses yet), so it only appears when the targets sheet already has one.
-- -----------------------------------------------------------------------------
WITH dim_ids AS (
  SELECT SELLER_ID AS sid FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE SELLER_ID IS NOT NULL
  UNION SELECT XOLA_SELLER_ID FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE XOLA_SELLER_ID IS NOT NULL
  UNION SELECT _GBGS_XOLA_SELLER_ID FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE _GBGS_XOLA_SELLER_ID IS NOT NULL
), s AS (   -- Xola sellers that are not in the dim
  SELECT SELLER_ID, SELLER_NAME, CITY, STATE AS state_name, ZIP_CODE, SPLIT_PART(STREET_ADDRESS, CHR(10), 1) AS street,
         IFF(SELLER_NAME ILIKE '%Great Big Game Show%', 'GBGS', 'TEG') AS concept
  FROM TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE
  WHERE SELLER_ID NOT IN (SELECT sid FROM dim_ids) AND IS_ENABLED
), state_map AS (   -- Xola state name -> the sheet's state abbreviation, learned from existing stores
  SELECT DISTINCT x.STATE AS state_name, d._STATE AS st
  FROM TEG_PROD.DW.DIM_STORES_MASTER d JOIN TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE x ON x.SELLER_ID = d.XOLA_SELLER_ID
  WHERE d._STATE IS NOT NULL
), nxt AS (        -- next store number per concept
  SELECT _CONCEPT AS concept, MAX(TRY_TO_NUMBER(_STORE_ORDER)) + 1 AS next_no
  FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE _CONCEPT IN ('TEG','GBGS') GROUP BY 1
), nb AS (         -- most recently opened store in the same state and concept, as the neighbour to copy zone info from
  SELECT s.SELLER_ID, d.LBS_NAME, d._ZONE_2026 AS zone, d._REGION_LEADER AS leader, d._AREA AS area, d._SHOPIFY_MARKET AS market,
         ROW_NUMBER() OVER (PARTITION BY s.SELLER_ID ORDER BY TRY_TO_DATE(d._DATE_OPENED, 'MM/DD/YYYY') DESC NULLS LAST) AS rn
  FROM s JOIN state_map m ON m.state_name = s.state_name
         JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d._STATE = m.st AND d._CONCEPT = s.concept AND d._ZONE_2026 IS NOT NULL
), tick AS (       -- a ticker already used in the targets sheets that starts the city name and isn't a store yet
  SELECT s.SELLER_ID, MIN(t.tk) AS ticker
  FROM s JOIN (SELECT DISTINCT STORE_TICKER AS tk FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_BUDGET) t
    ON STARTSWITH(UPPER(s.CITY), UPPER(t.tk)) AND LENGTH(t.tk) >= 3 AND t.tk NOT ILIKE 'GBGS%' AND t.tk NOT ILIKE 'TEMP%'
   AND t.tk NOT IN (SELECT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE TICKER_FIN IS NOT NULL)
  GROUP BY 1
), b AS (
  SELECT s.*, m.st, n.next_no, k.ticker, nb.LBS_NAME AS neighbour, nb.zone, nb.leader, nb.area, nb.market
  FROM s LEFT JOIN state_map m ON m.state_name = s.state_name LEFT JOIN nxt n ON n.concept = s.concept
         LEFT JOIN tick k ON k.SELLER_ID = s.SELLER_ID LEFT JOIN nb ON nb.SELLER_ID = s.SELLER_ID AND nb.rn = 1
)
SELECT SELLER_ID, SELLER_NAME, field, suggested_value, basis FROM (
  SELECT SELLER_ID, SELLER_NAME, 1 AS ord, 'xola_seller_id' AS field, SELLER_ID AS suggested_value, 'Xola seller' AS basis FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 2, '_Xola Store Name', SELLER_NAME, 'Xola seller name (confirm against i3/Revel)' FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 3, 'City', CITY, 'Xola seller city' FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 4, '_State', st, 'State abbreviation learned from existing stores in ' || state_name FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 5, '_Store Address', street || ', ' || CITY || ', ' || st || ' ' || ZIP_CODE, 'Xola address in the sheet''s format' FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 6, '_Concept', concept, 'Seller name' FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 7, '_Store Order / Store Number', next_no::STRING, 'Next number after the highest ' || concept || ' store' FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 8, 'Ticker (FIN) / Ticker (OPS)', ticker, 'Already used in the staffing budget sheet; no store has it yet (confirm)' FROM b WHERE ticker IS NOT NULL
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 9, '_TEG Ticker', ticker || ' (' || next_no || ')', 'Ticker plus store number' FROM b WHERE ticker IS NOT NULL
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 10, 'NetSuite Name', LPAD(next_no::STRING, 4, '0') || ' ' || ticker, 'Four-digit store number plus ticker' FROM b WHERE ticker IS NOT NULL
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 11, '_Every 5', '(' || (FLOOR((next_no - 1) / 5) * 5 + 1) || '-' || (FLOOR((next_no - 1) / 5) * 5 + 5) || ')', 'Five-store bucket for the store number' FROM b
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 12, '_Zone / _Zone 2026 / Region', zone, 'Same as ' || neighbour || ' (same state; confirm)' FROM b WHERE zone IS NOT NULL
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 13, '_Region Leader', leader, 'Leader of that zone per ' || neighbour FROM b WHERE leader IS NOT NULL
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 14, '_Area', area, 'Same as ' || neighbour FROM b WHERE area IS NOT NULL
  UNION ALL SELECT SELLER_ID, SELLER_NAME, 15, 'Hubspot / Paycor / Marketing / _ADP Name / _Shopify Market', CITY, 'City name, the pattern in recent rows (confirm)' FROM b
) ORDER BY SELLER_ID, ord;


-- =============================================================================
-- MANUAL-UPDATE UPKEEP (values that come from Google Sheets and need a person to keep them current)
-- The sheets land in TEG_PROD.STAGE as STG_GOOGLE_DRIVE__* views over Fivetran. Fivetran's sync time says nothing about
-- when someone last EDITED a sheet, so these checks look at the CONTENT: which periods have values, which stores have a
-- column, and whether the aggregate actually picked the values up.
-- Most of these sheets are "wide": one column per store ticker, one row per month. They are read with
-- OBJECT_CONSTRUCT_KEEP_NULL(*) + FLATTEN so a new store column is picked up without editing the SQL.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- CHECK 17: SLOTS UPKEEP  (Historical slots sheet vs Store Team Info vs what the aggregate shows)
-- Catches: a store open and taking bookings but with 0 slots in AGG_STORES_DAY, no column for a new store in the
-- Historical slots sheet, a column header that doesn't match the ticker, next month blank, Team Info disagreeing.
-- Visual: "Sheet upkeep" tab, Slots panel, plus the "Slots upkeep" Data Central tile.
-- Note: AGG_STORES_DAY.SLOTS is 0 for these stores even where the sheet HAS values (A_15, GBGSA_15, GBGSOAK_B), so the
-- cause is in the header/model mapping, not a missing entry. ATL_2 also has an underscore and works, so don't assume
-- underscores alone are the problem; check how the model maps sheet columns to tickers.
-- -----------------------------------------------------------------------------
-- 17a. Stores whose slots are missing, zero or unmatched (exactly the rows to fix)
WITH t AS (
  SELECT SLOTS AS month_label, OBJECT_CONSTRUCT_KEEP_NULL(*) AS o FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2024_LBS_KEYS_HISTORICAL_SLOTS
), f AS (
  SELECT TRY_TO_DATE('01 ' || month_label, 'DD Mon YYYY') AS mth, key AS col, TRY_TO_NUMBER(NULLIF(TRIM(value::STRING), '')) AS v
  FROM t, LATERAL FLATTEN(input => o) WHERE key NOT IN ('_LINE','_FIVETRAN_SYNCED','SLOTS')
), c AS (   -- one row per store column in the sheet, with this month's and next month's value
  SELECT col, REGEXP_REPLACE(UPPER(col), '[^A-Z0-9]', '') AS ncol,
         MAX(IFF(mth = DATE_TRUNC('month', CURRENT_DATE), v, NULL)) AS this_month,
         MAX(IFF(mth = DATE_TRUNC('month', DATEADD(month, 1, CURRENT_DATE)), v, NULL)) AS next_month
  FROM f GROUP BY col
), s AS (   -- active TEG and GBGS stores with the slots the aggregate shows for yesterday
  SELECT a.DIM_STORES_MASTER_ID AS id, MAX(a.LBS_NAME) AS lbs, MAX(a.CONCEPT) AS concept, MAX(a.STORE_STATUS) AS status,
         MAX(a.TICKER_FIN) AS tf, MAX(d.TICKER_OPS) AS tops, MAX(a.SLOTS) AS agg_slots, MAX(TRY_TO_NUMBER(d.SLOTS::STRING)) AS team_info_slots
  FROM TEG_PROD.DW.AGG_STORES_DAY a JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = a.DIM_STORES_MASTER_ID
  WHERE a.DATE_ACTUAL = DATEADD(day,-1,CURRENT_DATE) AND a.STORE_STATUS IN ('Mature','Ramping','New') AND a.CONCEPT IN ('TEG','GBGS')
    AND COALESCE(a.TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND a.LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
  GROUP BY 1
), ex AS (   -- the sheet column whose header matches the store's ticker
  SELECT s.id, MIN(c.col) AS exact_col, MAX(c.this_month) AS this_month, MAX(c.next_month) AS next_month
  FROM s JOIN c ON c.ncol = REGEXP_REPLACE(UPPER(s.tf), '[^A-Z0-9]', '') OR c.ncol = REGEXP_REPLACE(UPPER(s.tops), '[^A-Z0-9]', '')
  GROUP BY s.id
), sim AS (  -- a column that starts with the ticker (for example GBGSRCHM vs GBGSRCH)
  SELECT s.id, MIN(c.col) AS similar_col
  FROM s JOIN c ON c.ncol LIKE REGEXP_REPLACE(UPPER(s.tf), '[^A-Z0-9]', '') || '%'
  GROUP BY s.id
), j AS (
  SELECT s.*, ex.exact_col, ex.this_month, ex.next_month, IFF(ex.exact_col IS NULL, sim.similar_col, NULL) AS similar_col
  FROM s LEFT JOIN ex ON ex.id = s.id LEFT JOIN sim ON sim.id = s.id
)
SELECT lbs AS store, concept, status, tf AS ticker, agg_slots, team_info_slots, exact_col AS sheet_column, this_month AS sheet_this_month, next_month AS sheet_next_month, similar_col,
       CASE WHEN exact_col IS NULL AND similar_col IS NOT NULL THEN 'COLUMN UNDER A DIFFERENT NAME: ' || similar_col
            WHEN exact_col IS NULL THEN 'NO COLUMN IN THE SHEET'
            WHEN COALESCE(this_month, 0) = 0 THEN 'COLUMN EXISTS BUT THIS MONTH IS BLANK'
            WHEN COALESCE(agg_slots, 0) = 0 THEN 'SHEET HAS VALUES BUT THE AGGREGATE SHOWS 0'
            WHEN COALESCE(next_month, 0) = 0 THEN 'NEXT MONTH IS BLANK' END AS issue
FROM j
WHERE COALESCE(agg_slots, 0) = 0 OR exact_col IS NULL OR COALESCE(this_month, 0) = 0 OR COALESCE(next_month, 0) = 0
ORDER BY issue, lbs;

-- 17b. Store Team Info "Slots" vs the Historical slots sheet for the current month (one of the two was updated and not the other)
WITH t AS (
  SELECT SLOTS AS month_label, OBJECT_CONSTRUCT_KEEP_NULL(*) AS o FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2024_LBS_KEYS_HISTORICAL_SLOTS
), f AS (
  SELECT TRY_TO_DATE('01 ' || month_label, 'DD Mon YYYY') AS mth, key AS col, TRY_TO_NUMBER(NULLIF(TRIM(value::STRING), '')) AS v
  FROM t, LATERAL FLATTEN(input => o) WHERE key NOT IN ('_LINE','_FIVETRAN_SYNCED','SLOTS')
), c AS (
  SELECT col, REGEXP_REPLACE(UPPER(col), '[^A-Z0-9]', '') AS ncol, MAX(IFF(mth = DATE_TRUNC('month', CURRENT_DATE), v, NULL)) AS this_month
  FROM f GROUP BY col
), s AS (
  SELECT a.DIM_STORES_MASTER_ID AS id, MAX(a.LBS_NAME) AS lbs, MAX(a.TICKER_FIN) AS tf, MAX(d.TICKER_OPS) AS tops, MAX(TRY_TO_NUMBER(d.SLOTS::STRING)) AS team_info_slots
  FROM TEG_PROD.DW.AGG_STORES_DAY a JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = a.DIM_STORES_MASTER_ID
  WHERE a.DATE_ACTUAL = DATEADD(day,-1,CURRENT_DATE) AND a.STORE_STATUS IN ('Mature','Ramping','New') AND a.CONCEPT IN ('TEG','GBGS')
    AND COALESCE(a.TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND a.LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
  GROUP BY 1
)
SELECT s.lbs AS store, s.team_info_slots AS store_team_info_slots, MAX(c.this_month) AS historical_slots_this_month, MAX(c.this_month) - s.team_info_slots AS difference
FROM s JOIN c ON c.ncol = REGEXP_REPLACE(UPPER(s.tf), '[^A-Z0-9]', '') OR c.ncol = REGEXP_REPLACE(UPPER(s.tops), '[^A-Z0-9]', '')
GROUP BY s.lbs, s.team_info_slots
HAVING MAX(c.this_month) IS NOT NULL AND s.team_info_slots IS NOT NULL AND MAX(c.this_month) <> s.team_info_slots
ORDER BY ABS(MAX(c.this_month) - s.team_info_slots) DESC, s.lbs;


-- -----------------------------------------------------------------------------
-- CHECK 18: STORE COVERAGE ACROSS THE PER-STORE SHEETS  (which active stores have no column in each sheet)
-- The new-store checklist in query form: a store that opens needs a column in each of these sheets.
-- Included: sheets where every store should appear. Left out on purpose: the three "adjustment" sheets (exceptions
-- only), gift card breakage and the 2024 LBS Keys SLOTS sheet (both stop at 2025 / predate the newer stores).
-- A column under a slightly different ticker (GBGSRCHM vs GBGSRCH) counts as missing here; 17a shows the near-match.
-- Visual: "Sheet upkeep" tab, coverage panel, plus the "Stores missing from sheets" Data Central tile.
-- -----------------------------------------------------------------------------
WITH s AS (
  SELECT MAX(a.LBS_NAME) AS lbs, REGEXP_REPLACE(UPPER(MAX(a.TICKER_FIN)), '[^A-Z0-9]', '') AS n_fin, REGEXP_REPLACE(UPPER(MAX(d.TICKER_OPS)), '[^A-Z0-9]', '') AS n_ops
  FROM TEG_PROD.DW.AGG_STORES_DAY a JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = a.DIM_STORES_MASTER_ID
  WHERE a.DATE_ACTUAL = DATEADD(day,-1,CURRENT_DATE) AND a.STORE_STATUS IN ('Mature','Ramping','New') AND a.CONCEPT IN ('TEG','GBGS')
    AND COALESCE(a.TEG_TICKER,'') NOT IN ('RA','TBH','UFT') AND a.LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
  GROUP BY a.DIM_STORES_MASTER_ID
), tbls AS (SELECT column1 AS tname, column2 AS label FROM VALUES
  ('STG_GOOGLE_DRIVE___2024_LBS_KEYS_HISTORICAL_SLOTS','Historical slots'),
  ('STG_GOOGLE_DRIVE___2024_LBS_KEYS_SEASONALITY','Seasonality'),
  ('STG_GOOGLE_DRIVE___2024_LBS_KEYS_FLAT_MANAGEMENT_COST','Flat management cost'),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_NS_REVENUE_BUDGETS','NetSuite revenue budgets'),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_4_WALL_BUDGETS','NetSuite 4-wall budgets'),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_NS_REVENUE_ACTUALS','NetSuite revenue actuals'),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_4_WALL_ACTUALS','NetSuite 4-wall actuals'),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_COACHABILITY','GM score: coachability'),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_GUEST_SERVICE','GM score: guest service'),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_HQ_RESOURCE','GM score: HQ resource'),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_TIMELINESS','GM score: timeliness')
), cols AS (
  SELECT t.label, REGEXP_REPLACE(UPPER(c.COLUMN_NAME), '[^A-Z0-9]', '') AS ncol
  FROM tbls t JOIN TEG_PROD.INFORMATION_SCHEMA.COLUMNS c ON c.TABLE_SCHEMA = 'STAGE' AND c.TABLE_NAME = t.tname
)
SELECT t.label AS sheet, COUNT(*) AS active_stores, COUNT_IF(m.lbs IS NULL) AS stores_without_a_column,
       LISTAGG(IFF(m.lbs IS NULL, s.lbs, NULL), '; ') WITHIN GROUP (ORDER BY s.lbs) AS missing_stores
FROM tbls t CROSS JOIN s
LEFT JOIN (SELECT DISTINCT c.label, s2.lbs FROM cols c JOIN s s2 ON c.ncol IN (s2.n_fin, s2.n_ops)) m ON m.label = t.label AND m.lbs = s.lbs
GROUP BY t.label
ORDER BY stores_without_a_column DESC, t.label;


-- -----------------------------------------------------------------------------
-- CHECK 19: MONTH-BY-MONTH SHEETS, latest month that has any value
-- Catches: a monthly sheet that quietly stopped being filled in (Revenue adjustment: 76 stores a month through June,
-- nothing since), actuals that haven't been loaded after month close (NetSuite through August).
-- How to read it:  "actuals" and "regular series" sheets should be within 1 month of today (2 right after month end);
-- "budgets" should run to the end of the year; the adjustment sheets are exception-only, so a gap there is information,
-- not a failure. Compare stores_filled_that_month with the previous months to spot a half-filled month.
-- Visual: "Sheet upkeep" tab, month-by-month panel, plus the "Monthly sheets behind" Data Central tile.
-- -----------------------------------------------------------------------------
WITH u AS (
  SELECT 'Revenue adjustment' AS sheet, DATE::STRING AS d, 'MM/DD/YYYY' AS fmt, OBJECT_CONSTRUCT_KEEP_NULL(*) AS o FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2024_LBS_KEYS_REV_ADJUSTMENT
  UNION ALL SELECT 'Flat management cost', DATE::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2024_LBS_KEYS_FLAT_MANAGEMENT_COST
  UNION ALL SELECT 'Staffing cost adjustment', DATE::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2024_LBS_KEYS_STAFFING_COST_ADJUSTMENT
  UNION ALL SELECT 'Staffing hours adjustment', DATE::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__2024_LBS_KEYS_STAFFING_HOURS_ADJUSTMENT
  UNION ALL SELECT 'NetSuite salaries adjustment', DATE::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__2024_LBS_KEYS_NETSUITE_SALARIES_ADJUSTMENT
  UNION ALL SELECT 'NetSuite revenue actuals', DATE::STRING, 'YYYYMMDD', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_NS_REVENUE_ACTUALS
  UNION ALL SELECT 'NetSuite 4-wall actuals', DATE::STRING, 'YYYYMMDD', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_4_WALL_ACTUALS
  UNION ALL SELECT 'NetSuite revenue budgets', DATE::STRING, 'YYYYMMDD', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_NS_REVENUE_BUDGETS
  UNION ALL SELECT 'NetSuite 4-wall budgets', DATE::STRING, 'YYYYMMDD', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_4_WALL_BUDGETS
), f AS (
  SELECT sheet, TRY_TO_DATE(d, fmt) AS mth, TRY_TO_NUMBER(NULLIF(TRIM(value::STRING),'')) AS v
  FROM u, LATERAL FLATTEN(input => o) WHERE key NOT IN ('_LINE','_FIVETRAN_SYNCED','DATE')
), m AS (SELECT sheet, mth, COUNT_IF(v IS NOT NULL AND v <> 0) AS stores_filled FROM f GROUP BY sheet, mth),
latest AS (SELECT sheet, MAX(IFF(stores_filled > 0, mth, NULL)) AS latest_month_with_data FROM m GROUP BY sheet)
SELECT l.sheet, l.latest_month_with_data::STRING AS latest_month_with_data, m.stores_filled AS stores_filled_that_month,
       DATEDIFF(month, l.latest_month_with_data, DATE_TRUNC('month', CURRENT_DATE)) AS months_behind_current
FROM latest l JOIN m ON m.sheet = l.sheet AND m.mth = l.latest_month_with_data
ORDER BY l.sheet;

-- 19b. GM score qualitative tabs (one row per month, one column per store; the period label column is named after the tab,
--      for example COACHABILITY). Same idea: the latest month, and how many stores have a score that month.
WITH u AS (
  SELECT 'GM score: coachability' AS sheet, COACHABILITY::STRING AS lab, OBJECT_CONSTRUCT_KEEP_NULL(*) AS o FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_COACHABILITY
  UNION ALL SELECT 'GM score: guest service', GUEST_SERVICE::STRING, OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_GUEST_SERVICE
  UNION ALL SELECT 'GM score: HQ resource', HQ_RESOURCE::STRING, OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_HQ_RESOURCE
  UNION ALL SELECT 'GM score: timeliness', TIMELINESS::STRING, OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_TIMELINESS
), f AS (
  SELECT sheet, TRY_TO_DATE(lab, 'MM/DD/YYYY') AS mth, NULLIF(TRIM(value::STRING),'') AS v
  FROM u, LATERAL FLATTEN(input => o) WHERE key NOT IN ('_LINE','_FIVETRAN_SYNCED','COACHABILITY','GUEST_SERVICE','HQ_RESOURCE','TIMELINESS')
), m AS (SELECT sheet, mth, COUNT_IF(v IS NOT NULL) AS stores_scored FROM f GROUP BY sheet, mth),
latest AS (SELECT sheet, MAX(IFF(stores_scored > 0, mth, NULL)) AS latest_month FROM m GROUP BY sheet)
SELECT l.sheet, l.latest_month::STRING AS latest_month_with_scores, m.stores_scored
FROM latest l JOIN m ON m.sheet = l.sheet AND m.mth = l.latest_month ORDER BY l.sheet;

-- 19c. Things worth watching that the queries above don't automate (found while exploring, 2026-10-22):
--   * Guest care sheets (STG_GOOGLE_DRIVE__FIVETRAN_2026_GC_DATA_FOR_SNOWFLAKE_GC_*_DAILY_2026): TEG revenue is filled through
--     Oct 12; the GBGS rows in the "2026" revenue table carry 2025 dates, so GBGS guest care for 2026 doesn't look loaded.
--     The older FIVETRAN_GUEST_CARE_GC_CALL_VOLUME_DAILY table stopped on 2025-12-31.
--   * Sheets with no 2026 rows (probably retired, confirm): 2024 LBS Keys STORE_GAME_COUNT, GC_BREAKAGE (both end Dec 2025),
--     ADP_DATA_IMPORT (ends Sep 2024), and REPORTING.GM_STRETCH_GOAL (last Jan 2026; the aggregate column is 0 for every store).
--   * Resova sales sheet (STG_GOOGLE_DRIVE__RESOVA_DATA_RESOVA_SALES): latest sale date per store. Most stores stopped when they
--     moved to Xola, so only alert for stores that are still on Resova.


-- -----------------------------------------------------------------------------
-- CHECK 20: 3RD-PARTY BOOKINGS WITH NO RATE IN THE 3RD PARTY INFO TAB  (2024 LBS Keys)
-- Catches: a store selling through a partner (Viator, Groupon, Funex, MOA bundle...) whose row in the 3rd Party Info tab
-- has no rate for that program, so dbt never swaps in the partner rate and the booking keeps what the guest paid (often
-- full price, or $0 when the voucher is entered as a 100% discount). Found 2026-10-23: no GBGS row has a Viator or
-- MOA bundle rate, though seven GBGS stores took ~1,700 MOA bundle bookings in the past year.
-- How it works: the tab's first data row (_LINE 1) holds the Xola tag for each program column. A booking with one of those
-- tags and IS_THIRD_PARTY = 0 didn't match a rate; dbt sets IS_THIRD_PARTY = 1 on every booking it did (verified across
-- all matched tags). Tags match case- and space-insensitively, as in dbt (bookings carry them upper-cased).
-- Store join: STORE_ID = DIM_STORES_ID, the same as _AGG_BOOKINGS_DAY (not REVENUE_STORE_ID; the two differ only for ~1,300
-- Remote Adventures bookings a year, none 3rd party, checked 2026-10-23). Zone is DIM_STORES_MASTER._ZONE_2026.
-- Limits: a brand-new partner tag that isn't in the tab's tag row at all can't be seen here. Resova bookings (GBGS stores
-- until about March 2026) carry made-up tag lists, so treat Resova rows with suspicion.
-- Lookback: 90 days by default; the dashboard lets you pick up to 365 (the API's tp_days parameter).
-- Visual: "Sheet upkeep" tab, 3rd-party rates panel, plus the "3rd-party rates missing" Data Central tile.
-- -----------------------------------------------------------------------------
WITH scope_stores AS (
    SELECT DIM_STORES_MASTER_ID, MAX(LBS_NAME) AS LBS_NAME, MAX(CONCEPT) AS CONCEPT, MAX(TICKER_FIN) AS TICKER_FIN
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE STORE_STATUS IN ('Mature','Ramping','New') AND CONCEPT IN ('TEG','GBGS') AND COALESCE(TEG_TICKER,'') NOT IN ('RA','TBH','UFT')
      AND LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')
    GROUP BY 1
), sheet AS (
    SELECT _LINE AS line, UPPER(TRIM(TICKER_FIN)) AS tk, OBJECT_CONSTRUCT_KEEP_NULL(*) AS o
    FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2024_LBS_KEYS_3_RD_PARTY_INFO
), tag_cols AS (   -- program column -> Xola tag, from the tab's tag row
    SELECT UPPER(TRIM(f.value::STRING)) AS tag, MIN(f.key) AS col
    FROM sheet, LATERAL FLATTEN(input => o) f
    WHERE line = 1 AND f.key NOT IN ('_LINE','_FIVETRAN_SYNCED','PROGRAM_NAME','TICKER_FIN','TEG_RETAIL_RATE','TEG_RACK_RATE')
      AND NULLIF(TRIM(f.value::STRING),'') IS NOT NULL
    GROUP BY 1
), sheet_rows AS (
    SELECT tk, MIN(line) AS line FROM sheet WHERE line > 1 GROUP BY 1
), bk AS (
    SELECT DISTINCT b.ID, s.LBS_NAME, s.CONCEPT, s.TICKER_FIN, d._ZONE_2026 AS ZONE, t.tag, t.col, b.AMOUNT_PAID, b.DISCOUNT_AMOUNT, b.ARRIVAL_DATE
    FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS b
    JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.DIM_STORES_ID = b.STORE_ID
    JOIN scope_stores s ON s.DIM_STORES_MASTER_ID = d.ID,
         LATERAL FLATTEN(input => b.TAGS_ARRAY) f
    JOIN tag_cols t ON t.tag = UPPER(TRIM(f.value::STRING))
    WHERE b.ARRIVAL_DATE >= DATEADD(day, -90, CURRENT_DATE) AND b.ARRIVAL_DATE < CURRENT_DATE
      AND COALESCE(b.IS_THIRD_PARTY, 0) = 0
      AND COALESCE(b.IS_DEMO_TRAINING, 0) = 0
      AND COALESCE(b.BOOKING_STATUS, '') <> '700'   -- cancelled
)
SELECT bk.LBS_NAME AS store, bk.CONCEPT AS concept, bk.ZONE AS zone, bk.TICKER_FIN AS ticker, bk.tag,
       INITCAP(REPLACE(TRIM(bk.col, '_'), '_', ' ')) AS sheet_column,
       r.line + 2 AS sheet_row,   -- matches the sheet: ALX is _LINE 2 and sheet row 4
       COUNT(*) AS bookings, COUNT_IF(bk.AMOUNT_PAID = 0) AS fully_discounted,
       COUNT_IF(bk.AMOUNT_PAID > 0 AND COALESCE(bk.DISCOUNT_AMOUNT, 0) = 0) AS full_price,
       ROUND(SUM(bk.AMOUNT_PAID), 2) AS paid, MIN(bk.ARRIVAL_DATE)::DATE AS first_seen, MAX(bk.ARRIVAL_DATE)::DATE AS last_seen
FROM bk LEFT JOIN sheet_rows r ON r.tk = UPPER(TRIM(bk.TICKER_FIN))
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY bookings DESC, store;
