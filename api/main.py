"""
TEG Data Health — FastAPI backend
Pipeline and sheet-upkeep checks for the TEG + GBGS warehouse (TEG_PROD.DW),
one payload for the whole dashboard.

Endpoints:
  GET /health                             → liveness check
  GET /api/data-health/dashboard          → every check, shaped for the page
        ?target_month=YYYY-MM               (Targets tab month; default = next month)
        ?live=1                             (skip the S3 cache for the default view)
  GET /api/data-health/checks/{section}   → one section's slice of the payload
                                            (debugging / curl; see SECTIONS)

Run locally:  python -m uvicorn api.main:app --reload

Caching: the payload has no filter parameters that change the SQL (the page's
concept and heatmap-window filters are applied client-side to data that is
already network-wide), so api/refresh.py caches the whole default payload to
S3 and /api/data-health/dashboard serves that; running live is the dev /
non-default-month / cache-miss fallback. See CLAUDE.md "Caching strategy".

Auth: every /api/* route requires a verified Cognito id_token (see "Auth"
below). The cache is served through the API, never straight from S3, so it
sits behind the same check.

The SQL is ported from data_health_checks.sql and was validated against live
Snowflake as TABLEAU_SVC / PROD_RO. Deviations from that file are commented
where they happen.
"""

from __future__ import annotations

import base64
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

import boto3
import snowflake.connector
from cryptography.hazmat.primitives import serialization
import jwt
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from snowflake.connector import DictCursor

# Local dev: load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Snowflake connection — reusable as-is, don't rewrite this per project
# ---------------------------------------------------------------------------

def _load_snowflake_creds() -> dict:
    """Return creds from AWS Secrets Manager (Lambda) or env vars (local)."""
    secret_arn = os.environ.get("SNOWFLAKE_SECRET_ARN")
    if secret_arn:
        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION_NAME", "us-east-2"),
        )
        secret = client.get_secret_value(SecretId=secret_arn)
        return json.loads(secret["SecretString"])
    return {
        "account":                os.environ["SNOWFLAKE_ACCOUNT"],
        "user":                   os.environ["SNOWFLAKE_USER"],
        "private_key":            os.environ.get("SNOWFLAKE_PRIVATE_KEY"),
        "private_key_passphrase": os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"),
        "password":               os.environ.get("SNOWFLAKE_PASSWORD"),
        "warehouse":              os.environ.get("SNOWFLAKE_WAREHOUSE", "WH_DEV1_READ"),
        "role":                   os.environ.get("SNOWFLAKE_ROLE") or None,
    }


def _private_key_der(creds: dict) -> bytes:
    """Load the RSA private key from the shared service-account secret and
    hand it to the connector as PKCS8 DER, which is the only form
    snowflake-connector-python accepts for `private_key=`. The secret stores
    the PEM base64-encoded (and encrypted, with the passphrase alongside it),
    so decode before parsing when it isn't already armored text."""
    raw = creds["private_key"]
    if isinstance(raw, str):
        raw = raw.encode()
    if b"-----BEGIN" not in raw:
        raw = base64.b64decode(raw)
    passphrase = creds.get("private_key_passphrase") or None
    key = serialization.load_pem_private_key(
        raw,
        password=passphrase.encode() if passphrase else None,
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _auth_kwargs(creds: dict) -> dict:
    """Key-pair auth is the only thing that works against Snowflake now that
    MFA is enforced on human accounts; password is kept purely as a local-dev
    convenience for anyone still running against their own login."""
    if creds.get("private_key"):
        return {"private_key": _private_key_der(creds)}
    if creds.get("password"):
        return {"password": creds["password"]}
    raise RuntimeError(
        "Snowflake credentials must supply either private_key or password"
    )


def get_conn() -> snowflake.connector.SnowflakeConnection:
    creds = _load_snowflake_creds()
    kwargs = dict(
        account=creds["account"],
        user=creds["user"],
        warehouse=creds["warehouse"],
        database=os.environ.get("SNOWFLAKE_DATABASE", "TEG_PROD"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "DW"),
        # Everything on this dashboard is Central time: CURRENT_DATE and every
        # "yesterday" / "this month" in the checks follow the session zone,
        # which is Pacific on the service account unless set here.
        session_parameters={"TIMEZONE": "America/Chicago"},
        **_auth_kwargs(creds),
    )
    if creds.get("role"):
        kwargs["role"] = creds["role"]
    return snowflake.connector.connect(**kwargs)


# ---------------------------------------------------------------------------
# App — reusable as-is
# ---------------------------------------------------------------------------

app = FastAPI(title="TEG Data Health API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Null-safety helper — keep this pattern for any ratio/percentage calc.
# Build notes will often not mention the div-by-zero edge case explicitly
# because the source Tableau workbook doesn't guard it either — null it out
# rather than letting the endpoint crash or return NaN/Infinity to the client.
# ---------------------------------------------------------------------------

def pct_change(new: float, old: float) -> Optional[float]:
    if not old:
        return None
    return (new - old) / old


def safe_ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return num / den


# ---------------------------------------------------------------------------
# Scope: TEG and GBGS concept stores only (the "standard active store filter"
# from the top of data_health_checks.sql). Defined once here; every query
# below goes through SCOPE / scope() / SCOPE_STORES_CTE. prod_ro is read-only,
# so this can't be a view.
# ---------------------------------------------------------------------------

_SCOPE_FILTER = (
    "{a}STORE_STATUS IN ('Mature','Ramping','New') AND {a}CONCEPT IN ('TEG','GBGS') "
    "AND COALESCE({a}TEG_TICKER,'') NOT IN ('RA','TBH','UFT') "
    "AND {a}LBS_NAME NOT IN ('Remote Adventures','Team Building Hub')"
)


def scope(alias: str = "") -> str:
    return _SCOPE_FILTER.format(a=f"{alias}." if alias else "")


SCOPE = scope()

SCOPE_STORES_CTE = f"""scope_stores AS (
    SELECT DIM_STORES_MASTER_ID, MAX(LBS_NAME) AS LBS_NAME, MAX(CONCEPT) AS CONCEPT,
           MAX(TICKER_FIN) AS TICKER_FIN, MAX(NULLIF(TRIM(GOOGLE_PLACES_ID),'')) AS GOOGLE_PLACES_ID
    FROM TEG_PROD.DW.AGG_STORES_DAY WHERE {SCOPE} GROUP BY 1
)"""

# Active stores as of yesterday, with the sheet tickers (Checks 17 and 18).
_YDAY_STORES = f"""SELECT a.DIM_STORES_MASTER_ID AS id, MAX(a.LBS_NAME) AS lbs, MAX(a.CONCEPT) AS concept,
           MAX(a.STORE_STATUS) AS status, MAX(a.TICKER_FIN) AS tf, MAX(d.TICKER_OPS) AS tops,
           MAX(a.SLOTS) AS agg_slots, MAX(TRY_TO_NUMBER(d.SLOTS::STRING)) AS team_info_slots
    FROM TEG_PROD.DW.AGG_STORES_DAY a JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = a.DIM_STORES_MASTER_ID
    WHERE a.DATE_ACTUAL = DATEADD(day,-1,CURRENT_DATE) AND {scope('a')}
    GROUP BY 1"""

# Seller IDs live in three dim columns (Checks 15a and 16).
_DIM_SELLER_IDS = """dim_ids AS (
    SELECT SELLER_ID AS sid FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE SELLER_ID IS NOT NULL
    UNION SELECT XOLA_SELLER_ID FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE XOLA_SELLER_ID IS NOT NULL
    UNION SELECT _GBGS_XOLA_SELLER_ID FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE _GBGS_XOLA_SELLER_ID IS NOT NULL
)"""

# The store sheet ("LBS Keys - Store Team Info"). The 2026 tab is the one
# that carries XOLA_SELLER_ID; bump this when the sheet rolls to a new year.
STORE_SHEET_TABLE = "TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2026_LBS_KEYS_STORE_TEAM_INFO"
# Fivetran's _LINE is the 0-based data row; +1 for the header, +1 for 1-based.
SHEET_ROW_OFFSET = 2

_HIST_SLOTS = """t AS (
    SELECT SLOTS AS month_label, OBJECT_CONSTRUCT_KEEP_NULL(*) AS o
    FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE___2024_LBS_KEYS_HISTORICAL_SLOTS
), f AS (
    SELECT TRY_TO_DATE('01 ' || month_label, 'DD Mon YYYY') AS mth, key AS col,
           TRY_TO_NUMBER(NULLIF(TRIM(value::STRING), '')) AS v
    FROM t, LATERAL FLATTEN(input => o) WHERE key NOT IN ('_LINE','_FIVETRAN_SYNCED','SLOTS')
)"""

# Target types (Check 14). appl = which concepts the type applies to.
# ASSUMPTION from the build notes, to confirm: staff budget and staff HPG
# budget are TEG-only (0 GBGS rows in every month checked).
TARGET_TYPES = [
    {"k": "mrev", "label": "Monthly revenue target",     "src": "LBS_KEYS__MONTHLY_REV_TARGETS",          "appl": "BOTH"},
    {"k": "rev",  "label": "Daily revenue target",       "src": "LBS_KEYS__DAILY_REV_TARGETS",            "appl": "BOTH"},
    {"k": "vol",  "label": "Daily volume target",        "src": "LBS_KEYS__DAILY_VOL_TARGETS",            "appl": "BOTH"},
    {"k": "stf",  "label": "Daily staffing target",      "src": "LBS_KEYS__DAILY_STAFFING_TARGETS",       "appl": "BOTH"},
    {"k": "eff",  "label": "Staffing efficiency target", "src": "TARGETS_LOADING__STAFFING_EFF_TARGETS",  "appl": "BOTH"},
    {"k": "stfb", "label": "Staffing budget",            "src": "TARGETS_LOADING__STAFFING_BUDGET",       "appl": "BOTH"},
    {"k": "sb",   "label": "Staff budget",               "src": "TARGETS_LOADING__STAFF_BUDGETS",         "appl": "TEG"},
    {"k": "hpgb", "label": "Staff HPG budget",           "src": "TARGETS_LOADING__STAFF_HPG_BUDGET",      "appl": "TEG"},
    {"k": "hpgt", "label": "Staff HPG target",           "src": "TARGETS_LOADING__STAFF_HPG_TARGET",      "appl": "BOTH"},
    {"k": "hpgl", "label": "Staff HPG trendline",        "src": "TARGETS_LOADING__STAFF_HPG_TRENDLINE",   "appl": "BOTH"},
]

# Month-by-month sheets (Check 19). kind decides where the sheet should be:
#   actuals   → last month, once ~2 weeks past month end (else the month before)
#   current   → this month
#   prior     → last month (scored after the month closes)
#   budget    → December of this year
#   exception → adjustment sheets that only hold exceptions: information only
MONTHLY_SHEETS = {
    "Revenue adjustment":           "actuals",
    "NetSuite revenue actuals":     "actuals",
    "NetSuite 4-wall actuals":      "actuals",
    "NetSuite revenue budgets":     "budget",
    "NetSuite 4-wall budgets":      "budget",
    "Flat management cost":         "current",
    "GM scores (four tabs)":        "prior",
    "Staffing cost adjustment":     "exception",
    "Staffing hours adjustment":    "exception",
    "NetSuite salaries adjustment": "exception",
}

# Store sheet columns in sheet order, for the Check 16 fill-in strip. Column
# letters and headers come from the sheet itself (Fivetran doesn't preserve
# column order), stage column = where the current value lives in
# STORE_SHEET_TABLE (None = not synced). Update if columns move.
SHEET_COLUMNS = [
    ("xola_store_id", "XOLA_STORE_ID"), ("revel_store_id", "REVEL_STORE_ID"),
    ("i3_merchant_id_Revel", "I_3_MERCHANT_ID_REVEL"), ("i3_merchant_id_Xola", "I_3_MERCHANT_ID_XOLA"),
    ("i3 Merchant Name_Revel", "I_3_MERCHANT_NAME_REVEL"), ("i3 Merchant Name_Xola", "I_3_MERCHANT_NAME_XOLA"),
    ("Hubspot Name", "HUBSPOT_NAME"), ("Paycor Name", "PAYCOR_NAME"), ("LBS Name", "LBS_NAME"),
    ("_Store Order", "_STORE_ORDER"), ("Marketing Name", "MARKETING_NAME"), ("City", "CITY"),
    ("Ticker (FIN)", "TICKER_FIN"), ("Ticker (OPS)", "TICKER_OPS"), ("_TEG Ticker", "_TEG_TICKER"),
    ("_Xola Store Name", "_XOLA_STORE_NAME"), ("_Concept", "_CONCEPT"), ("_Zone", "_ZONE"),
    ("_Zone 2026", "_ZONE_2026"), ("Region", "REGION"), ("_Store Status", "_STORE_STATUS"),
    ("_Region Leader", "_REGION_LEADER"), ("_GM", "_GM"), ("_Tier 2025", "_TIER_2025"),
    ("NetSuite Name", "NET_SUITE_NAME"), ("_Vintage", "_VINTAGE"), ("_Every 5", "_EVERY_5"),
    ("Store Number", "STORE_NUMBER"), ("_Date Opened", "_DATE_OPENED"), ("_First Comp", "_FIRST_COMP"),
    ("_Store Year", "_STORE_YEAR"), ("_Senority", "_SENORITY"), ("_Store Address", "_STORE_ADDRESS"),
    ("_Area", "_AREA"), ("_State", "_STATE"), ("_MSA", "_MSA"), ("_Center Type", "_CENTER_TYPE"),
    ("_Center Score", "_CENTER_SCORE"), ("_Center Dominant", "_CENTER_DOMINANT"),
    ("_Cannalization", "_CANNALIZATION"), ("_Slot Change", "_SLOT_CHANGE"),
    ("_New Store Impact", "_NEW_STORE_IMPACT"), ("Slots", "SLOTS"), ("_Latest Slot", "_LATEST_SLOT"),
    ("Game Count", "GAME_COUNT"), ("_Slots By Day", None), ("Game Mix", "GAME_MIX"),
    ("Minimum Wage", "MINIMUM_WAGE"), ("Starting Pay", "STARTING_PAY"), ("_ADP Name", "_ADP_NAME"),
    ("_ADP Location", "_ADP_LOCATION"), ("_Shopify Market", "_SHOPIFY_MARKET"),
    ("xola_seller_id", "XOLA_SELLER_ID"), ("_google_places_id", "_GOOGLE_PLACES_ID"),
    ("_longitude", "_LONGITUDE"), ("_latitude", "_LATITUDE"),
    ("_gbgs_redirect_store_id", "_GBGS_REDIRECT_STORE_ID"), ("_gbgs_xola_seller_id", "_GBGS_XOLA_SELLER_ID"),
]  # first entry is column B (column A is not part of the row data)


# ---------------------------------------------------------------------------
# Queries. One per check; {target_month} is the only substitution and is
# always a validated date literal (see _target_month()).
# ---------------------------------------------------------------------------

QUERIES: dict[str, str] = {}

QUERIES["meta"] = """
SELECT CURRENT_DATE AS today, TO_CHAR(CURRENT_TIMESTAMP, 'YYYY-MM-DD HH24:MI') AS now_ct
"""

# Check 1. Freshness uses LAST_ALTERED, never MAX(date) on agg_stores_day
# (that table is a date spine with forward bookings on future dates).
QUERIES["freshness"] = """
SELECT TABLE_NAME AS table_name,
       TO_CHAR(CONVERT_TIMEZONE('America/Chicago', LAST_ALTERED), 'HH24:MI') AS at_ct,
       TO_CHAR(CONVERT_TIMEZONE('America/Chicago', LAST_ALTERED), 'YYYY-MM-DD HH24:MI') AS ts_ct,
       DATEDIFF(minute, LAST_ALTERED, CURRENT_TIMESTAMP()) / 60.0 AS hours, ROW_COUNT AS row_count
FROM TEG_PROD.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = 'DW'
  AND TABLE_NAME IN ('AGG_STORES_DAY','FCT_OPS_BOOKINGS_ADJUSTMENTS','FCT_NPS_DETAILS',
                     'FCT_STORES_ALL_REVIEWS','DIM_STORES_MASTER')
"""

# Check 2
QUERIES["lag"] = f"""
SELECT 'bookings' AS k, MAX(BOOKINGS_CREATED_ON)::DATE AS latest_date FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS
UNION ALL SELECT 'nps', MAX(SURVEY_DATE)::DATE FROM TEG_PROD.DW.FCT_NPS_DETAILS
UNION ALL SELECT 'reviews:' || REVIEW_PLATFORM, MAX(REVIEW_DATE) FROM TEG_PROD.DW.FCT_STORES_ALL_REVIEWS GROUP BY REVIEW_PLATFORM
UNION ALL SELECT 'agg', MAX(DATE_ACTUAL) FROM TEG_PROD.DW.AGG_STORES_DAY
WHERE DATE_ACTUAL < CURRENT_DATE AND {SCOPE}
  AND COALESCE(REVENUE_XOLA_LBS,0) + COALESCE(REVENUE_REVEL_LBS,0) > 0
"""

# Check 3
QUERIES["network"] = f"""
WITH daily AS (
    SELECT DATE_ACTUAL, DAYOFWEEK(DATE_ACTUAL) AS dow,
           SUM(COALESCE(REVENUE_XOLA_LBS,0) + COALESCE(REVENUE_REVEL_LBS,0)) AS total_rev
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE {SCOPE} AND DATE_ACTUAL BETWEEN DATEADD(day,-70,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
    GROUP BY 1,2
), w AS (
    SELECT *, AVG(total_rev) OVER (PARTITION BY dow ORDER BY DATE_ACTUAL ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_rev
    FROM daily
)
SELECT DATE_ACTUAL AS d, ROUND(total_rev) AS actual, ROUND(baseline_rev) AS base
FROM w WHERE DATE_ACTUAL >= DATEADD(day,-14,CURRENT_DATE) ORDER BY DATE_ACTUAL
"""

# Check 4. Check 5 ("yesterday's outliers") is derived from these rows in
# Python: the build notes say it uses the same logic as the heatmap, and the
# SQL file's separate Check 5 query uses a ~5-week baseline that disagrees
# with the heatmap's last column.
QUERIES["heatmap"] = f"""
WITH d AS (
    SELECT DIM_STORES_MASTER_ID, LBS_NAME, CONCEPT, DATE_ACTUAL, DAYOFWEEK(DATE_ACTUAL) AS dow,
           COALESCE(REVENUE_XOLA_LBS,0) + COALESCE(REVENUE_REVEL_LBS,0) AS total_rev
    FROM TEG_PROD.DW.AGG_STORES_DAY
    WHERE {SCOPE} AND DATE_ACTUAL BETWEEN DATEADD(day,-63,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
), w AS (
    SELECT *, AVG(total_rev) OVER (PARTITION BY DIM_STORES_MASTER_ID, dow
                                   ORDER BY DATE_ACTUAL ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING) AS baseline_rev
    FROM d
)
SELECT LBS_NAME AS store, CONCEPT AS concept, DATE_ACTUAL AS d, ROUND(total_rev) AS rev, ROUND(baseline_rev) AS base,
       CASE WHEN baseline_rev IS NULL OR baseline_rev = 0 THEN 'NA'
            WHEN total_rev = 0                             THEN 'ZERO'
            WHEN total_rev / baseline_rev < 0.4            THEN 'LOW'
            WHEN total_rev / baseline_rev > 2.5            THEN 'HIGH'
            ELSE 'OK' END AS status
FROM w WHERE DATE_ACTUAL >= DATEADD(day,-28,CURRENT_DATE)
"""

# Check 6 (7-day share of Revel revenue that is unknown + stores with any)
QUERIES["unknown"] = f"""
SELECT NULL AS store, SUM(UNKNOWN_REVEL_REVENUE_LBS) AS unknown_rev, SUM(REVENUE_REVEL_LBS) AS revel_rev, NULL AS qty
FROM TEG_PROD.DW.AGG_STORES_DAY
WHERE DATE_ACTUAL BETWEEN DATEADD(day,-7,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE) AND {SCOPE}
UNION ALL
SELECT LBS_NAME, SUM(UNKNOWN_REVEL_REVENUE_LBS), NULL, SUM(UNKNOWN_REVEL_REVENUE_LBS_QTY)
FROM TEG_PROD.DW.AGG_STORES_DAY
WHERE DATE_ACTUAL BETWEEN DATEADD(day,-7,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE) AND {SCOPE}
GROUP BY 1 HAVING SUM(UNKNOWN_REVEL_REVENUE_LBS) > 0
"""

# Check 7
QUERIES["recon"] = f"""
WITH {SCOPE_STORES_CTE}, f AS (
    SELECT ARRIVAL_DATE::DATE AS d, SUM(AMOUNT_PAID - COALESCE(PAYMENT_REFUND_AMOUNT,0)) AS paid_net
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
    SELECT f.d, f.paid_net, a.agg_rev, a.agg_rev / NULLIF(f.paid_net,0) AS ratio FROM f JOIN a USING (d)
), w AS (
    SELECT *, AVG(ratio) OVER (ORDER BY d ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING) AS trailing_ratio FROM r
)
SELECT d, ROUND(paid_net) AS paid_net, ROUND(agg_rev) AS agg_rev, ROUND(ratio,3) AS ratio, ROUND(trailing_ratio,3) AS trail
FROM w WHERE d >= DATEADD(day,-14,CURRENT_DATE) ORDER BY d
"""

# Check 8 (+ distinct orphan keys for the detail line)
QUERIES["bookings_int"] = """
SELECT COUNT(*) AS rows_checked, COUNT(*) - COUNT(DISTINCT b.ID) AS duplicate_ids,
       COUNT_IF(b.STORE_ID IS NULL AND b.REVENUE_STORE_ID IS NULL) AS null_store,
       COUNT_IF(b.AMOUNT_PAID IS NULL) AS null_amount_paid,
       COUNT_IF(b.AMOUNT_PAID > 0 AND COALESCE(b.PAYMENT_REFUND_AMOUNT,0) > b.AMOUNT_PAID) AS refund_exceeds_paid,
       COUNT_IF(d.ID IS NULL) AS orphan_store_rows
FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS b
LEFT JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = b.REVENUE_STORE_ID
WHERE b.BOOKINGS_CREATED_ON >= DATEADD(day,-30,CURRENT_DATE)
"""

# Check 8b
QUERIES["orphans"] = """
SELECT b.REVENUE_STORE_ID AS store_key, COUNT(*) AS rows_affected,
       MIN(b.BOOKINGS_CREATED_ON)::DATE AS first_seen, MAX(b.BOOKINGS_CREATED_ON)::DATE AS last_seen,
       MAX(b.X_SELLER_ID) AS x_seller_id
FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS b
LEFT JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d.ID = b.REVENUE_STORE_ID
WHERE b.BOOKINGS_CREATED_ON >= DATEADD(day,-30,CURRENT_DATE) AND d.ID IS NULL
GROUP BY 1 ORDER BY 2 DESC
"""

# Check 9
QUERIES["agg_int"] = f"""
SELECT
    (SELECT COUNT(*) FROM (SELECT DIM_STORES_MASTER_ID, DATE_ACTUAL FROM TEG_PROD.DW.AGG_STORES_DAY
                           GROUP BY 1,2 HAVING COUNT(*) > 1)) AS duplicate_store_days,
    (SELECT COUNT(*) FROM TEG_PROD.DW.AGG_STORES_DAY
     WHERE DATE_ACTUAL BETWEEN DATEADD(day,-7,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE) AND {SCOPE}
       AND REVENUE_XOLA_LBS IS NULL AND REVENUE_REVEL_LBS IS NULL) AS null_revenue_rows_7d
"""

# Check 10 (every store x platform cell; also the store list for the page)
QUERIES["coverage"] = f"""
WITH {SCOPE_STORES_CTE}, plat AS (
    SELECT column1 AS review_platform, column2 AS mapping_source
    FROM VALUES ('google', NULL), ('tripadvisor','TripAdvisor'), ('yelp','Yelp')
), m_ex AS (
    SELECT DISTINCT TICKER_FIN, MAPPING_SOURCE
    FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING
    WHERE NULLIF(TRIM(REVIEW_STORE_ID),'') IS NOT NULL
), m_lo AS (
    SELECT DISTINCT UPPER(TRIM(TICKER_FIN)) AS tk_norm, MAPPING_SOURCE FROM m_ex
), r AS (
    SELECT DIM_STORES_MASTER_ID, REVIEW_PLATFORM, MAX(REVIEW_DATE) AS last_review,
           COUNT_IF(REVIEW_DATE >= DATEADD(day,-30,CURRENT_DATE)) AS n_30d,
           COUNT_IF(REVIEW_DATE BETWEEN DATEADD(day,-120,CURRENT_DATE) AND DATEADD(day,-31,CURRENT_DATE)) / 3.0 AS avg_30d_prior
    FROM TEG_PROD.DW.FCT_STORES_ALL_REVIEWS GROUP BY 1,2
), j AS (
    SELECT s.LBS_NAME, s.CONCEPT, s.TICKER_FIN, p.review_platform, r.last_review, COALESCE(r.n_30d,0) AS n_30d, r.avg_30d_prior,
           CASE WHEN p.review_platform = 'google' THEN IFF(s.GOOGLE_PLACES_ID IS NULL, 'NONE', 'EXACT')
                WHEN m_ex.TICKER_FIN IS NOT NULL THEN 'EXACT'
                WHEN m_lo.tk_norm IS NOT NULL    THEN 'LOOSE'
                ELSE 'NONE' END AS mapped_state
    FROM scope_stores s CROSS JOIN plat p
    LEFT JOIN m_ex ON m_ex.TICKER_FIN = s.TICKER_FIN AND m_ex.MAPPING_SOURCE = p.mapping_source
    LEFT JOIN m_lo ON m_lo.tk_norm = UPPER(TRIM(s.TICKER_FIN)) AND m_lo.MAPPING_SOURCE = p.mapping_source
    LEFT JOIN r ON r.DIM_STORES_MASTER_ID = s.DIM_STORES_MASTER_ID AND r.REVIEW_PLATFORM = p.review_platform
)
SELECT LBS_NAME AS store, CONCEPT AS concept, TICKER_FIN AS ticker, review_platform AS platform,
       DATEDIFF(day, last_review, CURRENT_DATE) AS days_since, n_30d, ROUND(avg_30d_prior,1) AS prior_avg_30d,
       CASE WHEN mapped_state = 'NONE'  THEN 'MISSING'
            WHEN mapped_state = 'LOOSE' THEN 'MISMATCH'
            WHEN last_review IS NULL    THEN 'NODATA'
            WHEN n_30d = 0 AND avg_30d_prior >= 3 THEN 'STALE'
            ELSE 'OK' END AS status
FROM j
"""

# Check 10b
QUERIES["map_problems"] = """
WITH dim_t AS (SELECT DISTINCT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER)
SELECT m.MAPPING_SOURCE AS source, m.TICKER_FIN AS sheet_ticker, m.REVIEW_STORE_ID AS platform_id,
       CASE WHEN NULLIF(TRIM(m.REVIEW_STORE_ID),'') IS NULL THEN 'Blank platform ID'
            WHEN EXISTS (SELECT 1 FROM dim_t d WHERE UPPER(TRIM(d.TICKER_FIN)) = UPPER(TRIM(m.TICKER_FIN))) THEN 'Case mismatch'
            ELSE 'No matching ticker' END AS problem,
       (SELECT MIN(d.TICKER_FIN) FROM dim_t d WHERE UPPER(TRIM(d.TICKER_FIN)) = UPPER(TRIM(m.TICKER_FIN))) AS dim_ticker
FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING m
WHERE NULLIF(TRIM(m.REVIEW_STORE_ID),'') IS NULL
   OR NOT EXISTS (SELECT 1 FROM dim_t d WHERE d.TICKER_FIN = m.TICKER_FIN)
ORDER BY problem DESC, m.MAPPING_SOURCE DESC, m.TICKER_FIN
"""

# Check 11
QUERIES["unmapped_src"] = """
SELECT 'review' AS kind, REVIEW_PLATFORM AS source, COUNT(*) AS total_30d, COUNT_IF(DIM_STORES_MASTER_ID IS NULL) AS unmapped_30d
FROM TEG_PROD.DW.FCT_STORES_ALL_REVIEWS WHERE REVIEW_DATE >= DATEADD(day,-30,CURRENT_DATE)
GROUP BY REVIEW_PLATFORM
UNION ALL
SELECT 'nps', COALESCE(SOURCE,'(null)'), COUNT(*), COUNT_IF(DIM_STORES_MASTER_ID IS NULL)
FROM TEG_PROD.DW.FCT_NPS_DETAILS WHERE SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE)
GROUP BY COALESCE(SOURCE,'(null)')
"""

# Check 11b. Deviation from the SQL file: starts from the unmapped rows in
# fct_stores_all_reviews (what Check 11 counts) and reaches the listing via
# SOURCE_REVIEW_ID = the platform table's ID, so the drill-down always adds
# up to the KPI. That surfaced a fourth diagnosis (STALE): the platform table
# already keys the review to a store but fct_stores_all_reviews doesn't —
# all-reviews isn't re-keying older rows (likely an incremental model).
QUERIES["unmapped_reviews"] = """
WITH a AS (
  SELECT REVIEW_PLATFORM, SOURCE_REVIEW_ID, REVIEW_DATE FROM TEG_PROD.DW.FCT_STORES_ALL_REVIEWS
  WHERE DIM_STORES_MASTER_ID IS NULL AND REVIEW_DATE >= DATEADD(day,-30,CURRENT_DATE)
    AND REVIEW_PLATFORM IN ('tripadvisor','yelp')
), u AS (
  SELECT a.REVIEW_PLATFORM AS review_platform,
         IFF(a.REVIEW_PLATFORM = 'tripadvisor', 'TripAdvisor', 'Yelp') AS mapping_source,
         COALESCE(t.SOURCE_BRAND, y.SOURCE_BRAND)::STRING AS source_brand,
         COALESCE(t.TRIPADVISOR_LOCATION_ID::STRING, y.SERPAPI_PLACE_ID::STRING) AS platform_id,
         COALESCE(t.TRIPADVISOR_STORE_NAME, y.YELP_STORE_NAME)::STRING AS platform_name,
         MIN(COALESCE(t.TRIPADVISOR_URL, y.YELP_URL))::STRING AS url,
         COUNT(*) AS reviews, MAX(a.REVIEW_DATE) AS latest_review,
         COUNT(COALESCE(t.DIM_STORES_MASTER_ID, y.DIM_STORES_MASTER_ID)) AS keyed_in_platform_table
  FROM a
  LEFT JOIN TEG_PROD.DW.FCT_STORES_TRIPADVISOR_REVIEWS t ON a.REVIEW_PLATFORM = 'tripadvisor' AND t.ID = a.SOURCE_REVIEW_ID
  LEFT JOIN TEG_PROD.DW.FCT_STORES_YELP_REVIEWS y ON a.REVIEW_PLATFORM = 'yelp' AND y.ID = a.SOURCE_REVIEW_ID
  GROUP BY 1,2,3,4,5
)
SELECT u.review_platform AS platform, u.source_brand AS brand, u.platform_name AS name, u.platform_id AS id,
       u.url, u.reviews, u.latest_review AS latest, MAX(m.TICKER_FIN) AS ticker,
       CASE WHEN u.keyed_in_platform_table > 0 THEN 'STALE'
            WHEN COUNT(m.TICKER_FIN) = 0 THEN 'NOTIN'
            WHEN MAX(d.TICKER_FIN) IS NULL THEN 'MISMATCH'
            ELSE 'MATCH' END AS dx
FROM u
LEFT JOIN TEG_PROD.STAGE.STG_GOOGLE_DRIVE__ADDITIONAL_STORE_MAPPING_REVIEW_MAPPING m
       ON m.MAPPING_SOURCE = u.mapping_source AND m.REVIEW_STORE_ID = u.platform_id
LEFT JOIN (SELECT DISTINCT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER) d ON d.TICKER_FIN = m.TICKER_FIN
GROUP BY 1,2,3,4,5,6,7, u.keyed_in_platform_table
ORDER BY u.reviews DESC, u.platform_name
"""

# Check 11c
QUERIES["unmapped_nps"] = """
SELECT SOURCE AS source, COALESCE(LAST_LOCATION_PLAYED,'(null)') AS loc,
       COALESCE(LAST_GBGS_LOCATION_PLAYED,'(null)') AS gbgs_loc,
       COUNT(*) AS responses, MIN(SURVEY_DATE)::DATE AS first_seen, MAX(SURVEY_DATE)::DATE AS last_seen
FROM TEG_PROD.DW.FCT_NPS_DETAILS
WHERE DIM_STORES_MASTER_ID IS NULL AND SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE)
GROUP BY 1,2,3 ORDER BY responses DESC
"""

# Check 12a. Sparse (no row for a zero week); zero-filled and the trailing
# average computed in Python, since a SQL window over sparse rows skips the
# zero weeks.
QUERIES["nps_weekly"] = f"""
WITH {SCOPE_STORES_CTE}
SELECT s.LBS_NAME AS store, DATE_TRUNC('week', n.SURVEY_DATE)::DATE AS week_start, COUNT(*) AS responses
FROM TEG_PROD.DW.FCT_NPS_DETAILS n JOIN scope_stores s ON s.DIM_STORES_MASTER_ID = n.DIM_STORES_MASTER_ID
WHERE n.SURVEY_DATE >= DATE_TRUNC('week', DATEADD(week,-9,CURRENT_DATE))
GROUP BY 1,2
"""

# Check 12b
QUERIES["nps_valid"] = """
SELECT COUNT(*) AS rows_checked, COUNT(*) - COUNT(DISTINCT ID) AS duplicate_ids,
       COUNT_IF(NPS_RATING IS NULL) AS null_rating, COUNT_IF(NPS_RATING < 0 OR NPS_RATING > 10) AS out_of_range,
       COUNT_IF(DIM_STORES_MASTER_ID IS NULL) AS unmapped_store
FROM TEG_PROD.DW.FCT_NPS_DETAILS WHERE SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE)
"""

# Check 13
QUERIES["nps_none"] = f"""
WITH {SCOPE_STORES_CTE}
SELECT s.LBS_NAME AS store, s.CONCEPT AS concept, COALESCE(b.bookings_14d,0) AS bookings_14d, COALESCE(n.nps_30d,0) AS nps_30d
FROM scope_stores s
LEFT JOIN (SELECT REVENUE_STORE_ID AS id, COUNT(*) AS bookings_14d FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS
           WHERE ARRIVAL_DATE::DATE BETWEEN DATEADD(day,-14,CURRENT_DATE) AND DATEADD(day,-1,CURRENT_DATE)
           GROUP BY 1) b ON b.id = s.DIM_STORES_MASTER_ID
LEFT JOIN (SELECT DIM_STORES_MASTER_ID AS id, COUNT(*) AS nps_30d FROM TEG_PROD.DW.FCT_NPS_DETAILS
           WHERE SURVEY_DATE >= DATEADD(day,-30,CURRENT_DATE) GROUP BY 1) n ON n.id = s.DIM_STORES_MASTER_ID
WHERE COALESCE(b.bookings_14d,0) = 0 OR COALESCE(n.nps_30d,0) = 0
ORDER BY s.LBS_NAME
"""

# Check 14. Returns loaded flags per store x type for the target month and
# the month before; status and applicability (TARGET_TYPES) are applied in
# Python so the TEG-only assumption lives in one config map.
QUERIES["targets"] = f"""
WITH params AS (
    SELECT {{target_month}}::DATE AS target_month, DATEADD(month, -1, {{target_month}}::DATE) AS prior_month
), {SCOPE_STORES_CTE}, loaded AS (
    SELECT 'vol' AS k, UPPER(TRIM(STORE_TICKER)) AS tk, DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD')) AS mth
      FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_VOL_TARGETS WHERE IRL_TARGET > 0
    UNION SELECT 'rev', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_REV_TARGETS WHERE IRL_TARGET > 0
    UNION SELECT 'mrev', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.LBS_KEYS__MONTHLY_REV_TARGETS WHERE IRL_TARGET > 0
    UNION SELECT 'stf', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(DATE,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_STAFFING_TARGETS WHERE STAFFING_TARGET > 0
    UNION SELECT 'eff', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_EFF_TARGETS WHERE STAFFING_EFFICIENCY_TARGET > 0
    UNION SELECT 'sb', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_BUDGETS WHERE STAFF_BUDGET > 0
    UNION SELECT 'stfb', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_BUDGET WHERE STAFFING_BUDGET > 0
    UNION SELECT 'hpgt', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_TARGET WHERE STAFF_HPG_TARGET > 0
    UNION SELECT 'hpgb', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_BUDGET WHERE STAFF_HPG_BUDGET > 0
    UNION SELECT 'hpgl', UPPER(TRIM(STORE_TICKER)), DATE_TRUNC('month', TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD'))
      FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_TRENDLINE WHERE STAFF_HPG_TRENDLINE > 0
), types AS (
    SELECT column1 AS k FROM VALUES ('mrev'),('rev'),('vol'),('stf'),('eff'),('stfb'),('sb'),('hpgb'),('hpgt'),('hpgl')
)
SELECT s.LBS_NAME AS store, s.CONCEPT AS concept, t.k,
       MAX(IFF(l.mth = p.target_month, 1, 0)) AS in_target, MAX(IFF(l.mth = p.prior_month, 1, 0)) AS in_prior
FROM scope_stores s CROSS JOIN types t CROSS JOIN params p
LEFT JOIN loaded l ON l.k = t.k AND l.tk = UPPER(TRIM(s.TICKER_FIN)) AND l.mth IN (p.target_month, p.prior_month)
GROUP BY 1,2,3
"""

# Check 14c. Deviation: all ten target tables, from the month before the
# target month onward (the SQL file checked six, from the current month).
QUERIES["target_orphans"] = """
WITH dim_t AS (SELECT DISTINCT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE TICKER_FIN IS NOT NULL),
p AS (SELECT DATEADD(month, -1, {target_month}::DATE) AS m),
l AS (
    SELECT 'Monthly revenue' AS target_type, STORE_TICKER FROM TEG_PROD.REPORTING.LBS_KEYS__MONTHLY_REV_TARGETS, p WHERE TRY_TO_DATE(DATE,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Daily revenue', STORE_TICKER FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_REV_TARGETS, p WHERE TRY_TO_DATE(DATE,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Daily volume', STORE_TICKER FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_VOL_TARGETS, p WHERE TRY_TO_DATE(DATE,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Daily staffing', STORE_TICKER FROM TEG_PROD.REPORTING.LBS_KEYS__DAILY_STAFFING_TARGETS, p WHERE TRY_TO_DATE(DATE,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Staffing efficiency', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_EFF_TARGETS, p WHERE TRY_TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Staffing budget', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_BUDGET, p WHERE TRY_TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Staff budget', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_BUDGETS, p WHERE TRY_TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Staff HPG target', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_TARGET, p WHERE TRY_TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Staff HPG budget', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_BUDGET, p WHERE TRY_TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD') >= p.m
    UNION ALL SELECT 'Staff HPG trendline', STORE_TICKER FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFF_HPG_TRENDLINE, p WHERE TRY_TO_DATE(MONTH_DATE_KEY::STRING,'YYYYMMDD') >= p.m
)
SELECT STORE_TICKER AS ticker, LISTAGG(DISTINCT target_type, ', ') WITHIN GROUP (ORDER BY target_type) AS types
FROM l WHERE STORE_TICKER IS NOT NULL AND STORE_TICKER NOT IN (SELECT TICKER_FIN FROM dim_t)
GROUP BY 1 ORDER BY 1
"""

# Check 15a
QUERIES["sellers"] = f"""
WITH {_DIM_SELLER_IDS}, bk AS (
  SELECT X_SELLER_ID AS sid, COUNT(*) AS booking_rows, MIN(BOOKINGS_CREATED_ON)::DATE AS first_created,
         MAX(BOOKINGS_CREATED_ON)::DATE AS last_created, MIN(ARRIVAL_DATE)::DATE AS first_arrival, ROUND(SUM(AMOUNT_PAID)) AS paid
  FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS WHERE X_SELLER_ID IS NOT NULL GROUP BY 1
), open_rows AS (
  SELECT LBS_NAME, XOLA_STORE_ID FROM TEG_PROD.DW.DIM_STORES_MASTER
  WHERE COALESCE(NULLIF(XOLA_SELLER_ID,''), NULLIF(_GBGS_XOLA_SELLER_ID,'')) IS NULL AND NULLIF(TRIM(LBS_NAME),'') IS NOT NULL
)
SELECT s.SELLER_ID AS x_seller_id, s.SELLER_NAME AS seller_name, s.CITY AS city, s.STATE AS state,
       REPLACE(s.STREET_ADDRESS, CHR(10), ', ') AS street_address, s.IS_ENABLED AS is_enabled,
       s.SOURCE_CREATE_DATE::DATE AS created_in_xola,
       COALESCE(bk.booking_rows,0) AS booking_rows, bk.first_created, bk.last_created, bk.first_arrival, COALESCE(bk.paid,0) AS paid,
       LISTAGG(DISTINCT o.LBS_NAME || ' (xola_store_id ' || COALESCE(o.XOLA_STORE_ID::STRING,'blank') || ')', '; ') AS possible_sheet_row
FROM TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE s
LEFT JOIN bk ON bk.sid = s.SELLER_ID
LEFT JOIN open_rows o ON POSITION(LOWER(TRIM(REPLACE(o.LBS_NAME,'GBGS ',''))), LOWER(s.SELLER_NAME)) > 0
                     AND (s.SELLER_NAME ILIKE '%Great Big Game Show%') = (o.LBS_NAME ILIKE 'GBGS%')
WHERE s.SELLER_ID NOT IN (SELECT sid FROM dim_ids)
GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12
ORDER BY booking_rows DESC, created_in_xola DESC
"""

# Checks 15b + 15c (counts and lists for the placeholder panel)
QUERIES["placeholders"] = """
WITH d AS (
  SELECT ID, LBS_NAME, "_CONCEPT" AS concept, "_STORE_STATUS" AS status, TICKER_FIN,
         COALESCE(NULLIF(XOLA_SELLER_ID,''), NULLIF("_GBGS_XOLA_SELLER_ID",'')) AS any_seller
  FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE NULLIF(TRIM(LBS_NAME),'') IS NOT NULL
), b AS (
  SELECT REVENUE_STORE_ID AS id, COUNT(*) AS booking_rows, MAX(BOOKINGS_CREATED_ON)::DATE AS last_created
  FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS WHERE BOOKINGS_CREATED_ON >= DATEADD(day,-60,CURRENT_DATE) GROUP BY 1
)
SELECT 'placeholder' AS k, d.LBS_NAME AS name, b.booking_rows, b.last_created,
       ARRAY_TO_STRING(ARRAY_CONSTRUCT_COMPACT(IFF(d.concept IS NULL,'concept',NULL), IFF(d.status IS NULL,'status',NULL),
         IFF(d.TICKER_FIN IS NULL OR d.TICKER_FIN='UNK','ticker',NULL), IFF(d.any_seller IS NULL,'seller_id',NULL)), ', ') AS missing
FROM d LEFT JOIN b ON b.id = d.ID
WHERE d.concept IS NULL OR d.TICKER_FIN IS NULL OR d.TICKER_FIN = 'UNK' OR d.any_seller IS NULL
UNION ALL
SELECT 'unknown_seller', b.X_SELLER_ID, COUNT(*), MAX(b.BOOKINGS_CREATED_ON)::DATE, NULL
FROM TEG_PROD.DW.FCT_OPS_BOOKINGS_ADJUSTMENTS b
WHERE b.BOOKINGS_CREATED_ON >= DATEADD(day,-30,CURRENT_DATE) AND b.X_SELLER_ID IS NOT NULL
  AND b.X_SELLER_ID NOT IN (SELECT SELLER_ID FROM TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE)
GROUP BY 2
"""

# Check 16. Returns one base row per enabled unmatched seller; the per-column
# suggestions are built in Python (_suggestions) so each carries its basis
# and confidence. Additions over the SQL file: the sheet row (_LINE) and its
# current values, the GBGS twin row, the recent-openings tier, and the status
# unopened rows use — each confirmed against the sheet's existing rows.
QUERIES["suggest"] = f"""
WITH {_DIM_SELLER_IDS}, s AS (
  SELECT SELLER_ID, SELLER_NAME, CITY, STATE AS state_name, ZIP_CODE, SPLIT_PART(STREET_ADDRESS, CHR(10), 1) AS street,
         IFF(SELLER_NAME ILIKE '%Great Big Game Show%', 'GBGS', 'TEG') AS concept
  FROM TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE
  WHERE SELLER_ID NOT IN (SELECT sid FROM dim_ids) AND IS_ENABLED
), state_map AS (
  SELECT DISTINCT x.STATE AS state_name, d._STATE AS st
  FROM TEG_PROD.DW.DIM_STORES_MASTER d JOIN TEG_PROD.STAGE.STG_XOLA_DATABASE__SELLER_SOURCE x ON x.SELLER_ID = d.XOLA_SELLER_ID
  WHERE d._STATE IS NOT NULL
), nxt AS (
  SELECT _CONCEPT AS concept, MAX(TRY_TO_NUMBER(_STORE_ORDER)) + 1 AS next_no
  FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE _CONCEPT IN ('TEG','GBGS') GROUP BY 1
), nb AS (
  SELECT s.SELLER_ID, d.LBS_NAME, d._ZONE AS zone, d._ZONE_2026 AS zone_2026, d.REGION AS region,
         d._REGION_LEADER AS leader, d._AREA AS area,
         ROW_NUMBER() OVER (PARTITION BY s.SELLER_ID ORDER BY TRY_TO_DATE(d._DATE_OPENED, 'MM/DD/YYYY') DESC NULLS LAST) AS rn
  FROM s JOIN state_map m ON m.state_name = s.state_name
         JOIN TEG_PROD.DW.DIM_STORES_MASTER d ON d._STATE = m.st AND d._CONCEPT = s.concept AND d._ZONE_2026 IS NOT NULL
), tick AS (
  SELECT s.SELLER_ID, MIN(t.tk) AS ticker
  FROM s JOIN (SELECT DISTINCT STORE_TICKER AS tk FROM TEG_PROD.REPORTING.TARGETS_LOADING__STAFFING_BUDGET) t
    ON STARTSWITH(UPPER(s.CITY), UPPER(t.tk)) AND LENGTH(t.tk) >= 3 AND t.tk NOT ILIKE 'GBGS%' AND t.tk NOT ILIKE 'TEMP%'
   AND t.tk NOT IN (SELECT TICKER_FIN FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE TICKER_FIN IS NOT NULL)
  GROUP BY 1
), tier AS (
  SELECT _CONCEPT AS concept, ARRAY_AGG(DISTINCT _TIER_2025) AS tiers, LISTAGG(LBS_NAME, ', ') AS tier_from FROM (
    SELECT _CONCEPT, _TIER_2025, LBS_NAME,
           ROW_NUMBER() OVER (PARTITION BY _CONCEPT ORDER BY TRY_TO_NUMBER(_STORE_ORDER) DESC NULLS LAST) AS rn
    FROM TEG_PROD.DW.DIM_STORES_MASTER WHERE _CONCEPT IN ('TEG','GBGS') AND _TIER_2025 IS NOT NULL) WHERE rn <= 3
  GROUP BY 1
), unopened AS (
  SELECT MODE(_STORE_STATUS) AS status, MODE(_STORE_YEAR) AS store_year FROM {STORE_SHEET_TABLE}
  WHERE _CONCEPT IS NOT NULL AND TRY_TO_DATE(_DATE_OPENED, 'MM/DD/YYYY') > CURRENT_DATE
), sheet AS (
  SELECT _LINE, LBS_NAME, XOLA_STORE_ID, OBJECT_CONSTRUCT_KEEP_NULL(*) AS obj FROM {STORE_SHEET_TABLE}
  WHERE NULLIF(TRIM(XOLA_SELLER_ID),'') IS NULL AND NULLIF(TRIM(_GBGS_XOLA_SELLER_ID),'') IS NULL
    AND NULLIF(TRIM(LBS_NAME),'') IS NOT NULL
)
SELECT s.SELLER_ID AS seller_id, s.SELLER_NAME AS seller_name, s.CITY AS city, s.state_name, s.ZIP_CODE AS zip, s.street,
       s.concept, m.st, n.next_no, k.ticker, nb.LBS_NAME AS neighbour, nb.zone, nb.zone_2026, nb.region, nb.leader, nb.area,
       tier.tiers, tier.tier_from, u.status AS unopened_status, u.store_year AS unopened_year,
       row_.LBS_NAME AS row_name, row_._LINE AS row_line, row_.obj AS row_values,
       twin.LBS_NAME AS twin_name, twin.XOLA_STORE_ID AS twin_xola_store_id
FROM s LEFT JOIN state_map m ON m.state_name = s.state_name LEFT JOIN nxt n ON n.concept = s.concept
       LEFT JOIN tick k ON k.SELLER_ID = s.SELLER_ID LEFT JOIN nb ON nb.SELLER_ID = s.SELLER_ID AND nb.rn = 1
       LEFT JOIN tier ON tier.concept = s.concept CROSS JOIN unopened u
       LEFT JOIN sheet row_ ON LOWER(TRIM(REPLACE(row_.LBS_NAME,'GBGS ',''))) = LOWER(TRIM(s.CITY))
                           AND (row_.LBS_NAME ILIKE 'GBGS%') = (s.concept = 'GBGS')
       LEFT JOIN sheet twin ON s.concept = 'TEG' AND LOWER(twin.LBS_NAME) = 'gbgs ' || LOWER(TRIM(s.CITY))
"""

# Check 17a
QUERIES["slots"] = f"""
WITH {_HIST_SLOTS}, c AS (
  SELECT col, REGEXP_REPLACE(UPPER(col), '[^A-Z0-9]', '') AS ncol,
         MAX(IFF(mth = DATE_TRUNC('month', CURRENT_DATE), v, NULL)) AS this_month,
         MAX(IFF(mth = DATE_TRUNC('month', DATEADD(month, 1, CURRENT_DATE)), v, NULL)) AS next_month
  FROM f GROUP BY col
), s AS ({_YDAY_STORES}
), ex AS (
  SELECT s.id, MIN(c.col) AS exact_col, MAX(c.this_month) AS this_month, MAX(c.next_month) AS next_month
  FROM s JOIN c ON c.ncol = REGEXP_REPLACE(UPPER(s.tf), '[^A-Z0-9]', '') OR c.ncol = REGEXP_REPLACE(UPPER(s.tops), '[^A-Z0-9]', '')
  GROUP BY s.id
), sim AS (
  SELECT s.id, MIN(c.col) AS similar_col FROM s JOIN c ON c.ncol LIKE REGEXP_REPLACE(UPPER(s.tf), '[^A-Z0-9]', '') || '%'
  GROUP BY s.id
)
SELECT s.lbs AS store, s.concept, s.tf AS ticker, s.agg_slots, s.team_info_slots, ex.exact_col AS sheet_column,
       ex.this_month AS sheet_this_month, ex.next_month AS sheet_next_month, IFF(ex.exact_col IS NULL, sim.similar_col, NULL) AS similar_col
FROM s LEFT JOIN ex ON ex.id = s.id LEFT JOIN sim ON sim.id = s.id
WHERE COALESCE(s.agg_slots, 0) = 0 OR ex.exact_col IS NULL OR COALESCE(ex.this_month, 0) = 0 OR COALESCE(ex.next_month, 0) = 0
ORDER BY s.lbs
"""

# Check 17b
QUERIES["slot_mismatch"] = f"""
WITH {_HIST_SLOTS}, c AS (
  SELECT col, REGEXP_REPLACE(UPPER(col), '[^A-Z0-9]', '') AS ncol, MAX(IFF(mth = DATE_TRUNC('month', CURRENT_DATE), v, NULL)) AS this_month
  FROM f GROUP BY col
), s AS ({_YDAY_STORES})
SELECT s.lbs AS store, s.team_info_slots AS team_info, MAX(c.this_month) AS sheet
FROM s JOIN c ON c.ncol = REGEXP_REPLACE(UPPER(s.tf), '[^A-Z0-9]', '') OR c.ncol = REGEXP_REPLACE(UPPER(s.tops), '[^A-Z0-9]', '')
GROUP BY s.lbs, s.team_info_slots
HAVING MAX(c.this_month) IS NOT NULL AND s.team_info_slots IS NOT NULL AND MAX(c.this_month) <> s.team_info_slots
ORDER BY ABS(MAX(c.this_month) - s.team_info_slots) DESC, s.lbs
"""

# Check 18
QUERIES["sheet_cov"] = f"""
WITH s AS (
  SELECT MAX(lbs) AS lbs, REGEXP_REPLACE(UPPER(MAX(tf)), '[^A-Z0-9]', '') AS n_fin, REGEXP_REPLACE(UPPER(MAX(tops)), '[^A-Z0-9]', '') AS n_ops
  FROM ({_YDAY_STORES}) GROUP BY id
), tbls AS (SELECT column1 AS tname, column2 AS label, column3 AS ord FROM VALUES
  ('STG_GOOGLE_DRIVE___2024_LBS_KEYS_HISTORICAL_SLOTS','Historical slots',1),
  ('STG_GOOGLE_DRIVE___2024_LBS_KEYS_SEASONALITY','Seasonality',2),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_NS_REVENUE_BUDGETS','NetSuite revenue budgets',3),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_4_WALL_BUDGETS','NetSuite 4-wall budgets',4),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_NS_REVENUE_ACTUALS','NetSuite revenue actuals',5),
  ('STG_GOOGLE_DRIVE__NETSUITE_ENTRIES_4_WALL_ACTUALS','NetSuite 4-wall actuals',6),
  ('STG_GOOGLE_DRIVE___2024_LBS_KEYS_FLAT_MANAGEMENT_COST','Flat management cost',7),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_COACHABILITY','GM score: coachability',8),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_GUEST_SERVICE','GM score: guest service',9),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_HQ_RESOURCE','GM score: HQ resource',10),
  ('STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_TIMELINESS','GM score: timeliness',11)
), cols AS (
  SELECT t.label, REGEXP_REPLACE(UPPER(c.COLUMN_NAME), '[^A-Z0-9]', '') AS ncol
  FROM tbls t JOIN TEG_PROD.INFORMATION_SCHEMA.COLUMNS c ON c.TABLE_SCHEMA = 'STAGE' AND c.TABLE_NAME = t.tname
)
SELECT t.label AS sheet, MIN(t.ord) AS ord, COUNT_IF(m.lbs IS NULL) AS missing_n,
       ARRAY_AGG(IFF(m.lbs IS NULL, s.lbs, NULL)) WITHIN GROUP (ORDER BY s.lbs) AS missing
FROM tbls t CROSS JOIN s
LEFT JOIN (SELECT DISTINCT c.label, s2.lbs FROM cols c JOIN s s2 ON c.ncol IN (s2.n_fin, s2.n_ops)) m ON m.label = t.label AND m.lbs = s.lbs
GROUP BY t.label ORDER BY ord
"""

# Checks 19 + 19b in one pass (the GM score tabs count non-blank text cells)
QUERIES["monthly"] = """
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
  UNION ALL SELECT 'GM score: coachability', COACHABILITY::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_COACHABILITY
  UNION ALL SELECT 'GM score: guest service', GUEST_SERVICE::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_GUEST_SERVICE
  UNION ALL SELECT 'GM score: HQ resource', HQ_RESOURCE::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_HQ_RESOURCE
  UNION ALL SELECT 'GM score: timeliness', TIMELINESS::STRING, 'MM/DD/YYYY', OBJECT_CONSTRUCT_KEEP_NULL(*) FROM TEG_PROD.STAGE.STG_GOOGLE_DRIVE__GM_SCORE_QUALITATIVE_TIMELINESS
), f AS (
  SELECT sheet, TRY_TO_DATE(d, fmt) AS mth,
         IFF(sheet LIKE 'GM score%', IFF(NULLIF(TRIM(value::STRING),'') IS NOT NULL, 1, NULL),
             TRY_TO_NUMBER(NULLIF(TRIM(value::STRING),''))) AS v
  FROM u, LATERAL FLATTEN(input => o)
  WHERE key NOT IN ('_LINE','_FIVETRAN_SYNCED','DATE','COACHABILITY','GUEST_SERVICE','HQ_RESOURCE','TIMELINESS')
), m AS (SELECT sheet, mth, COUNT_IF(v IS NOT NULL AND v <> 0) AS stores_filled FROM f GROUP BY sheet, mth),
latest AS (SELECT sheet, MAX(IFF(stores_filled > 0, mth, NULL)) AS latest_month FROM m GROUP BY sheet)
SELECT l.sheet, l.latest_month, m.stores_filled, p.stores_filled AS prior_filled
FROM latest l
LEFT JOIN m ON m.sheet = l.sheet AND m.mth = l.latest_month
LEFT JOIN m p ON p.sheet = l.sheet AND p.mth = DATEADD(month, -1, l.latest_month)
"""

# 19c, automated for guest care: latest day this year with revenue, per concept.
QUERIES["guest_care"] = """
SELECT c.concept, MAX(IFF(YEAR(TRY_TO_DATE(g.DATE::STRING)) = YEAR(CURRENT_DATE) AND TRY_TO_NUMBER(g.REVENUE::STRING) <> 0,
                          TRY_TO_DATE(g.DATE::STRING), NULL)) AS latest,
       COUNT_IF(YEAR(TRY_TO_DATE(g.DATE::STRING)) = YEAR(CURRENT_DATE)) AS rows_this_year
FROM (SELECT column1 AS concept FROM VALUES ('TEG'), ('GBGS')) c
LEFT JOIN TEG_PROD.STAGE.STG_GOOGLE_DRIVE__FIVETRAN_2026_GC_DATA_FOR_SNOWFLAKE_GC_REVENUE_DATA_DAILY_2026 g ON g.CONCEPT = c.concept
GROUP BY 1
"""


# ---------------------------------------------------------------------------
# Query runner. Every check is independent, so they run concurrently on one
# connection (one cursor per thread). Rows come back as lower-cased dicts of
# plain JSON types.
# ---------------------------------------------------------------------------

def _clean(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _target_month(value: Optional[str], today: date) -> date:
    """`YYYY-MM` (or a full date) → the 1st of that month; default next month."""
    if not value:
        return (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    try:
        return date.fromisoformat(value + "-01" if len(value) == 7 else value).replace(day=1)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"target_month must be YYYY-MM, got {value!r}")


def run_queries(names: list[str], target_month: Optional[str] = None) -> dict:
    conn = get_conn()
    try:
        cur = conn.cursor(DictCursor)
        cur.execute(QUERIES["meta"])
        meta = {k.lower(): _clean(v) for k, v in cur.fetchone().items()}
        today = date.fromisoformat(meta["today"])
        tm = _target_month(target_month, today)
        meta["target_month"] = tm.isoformat()
        timings: dict[str, float] = {}

        def one(name: str) -> list[dict]:
            t0 = time.time()
            c = conn.cursor(DictCursor)
            try:
                c.execute(QUERIES[name].replace("{target_month}", f"'{tm.isoformat()}'"))
                rows = [{k.lower(): _clean(v) for k, v in row.items()} for row in c.fetchall()]
            finally:
                c.close()
            timings[name] = round(time.time() - t0, 2)
            return rows

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(zip(names, pool.map(one, names)))
    finally:
        conn.close()
    results["meta"] = meta
    results["_timings"] = timings
    return results


# ---------------------------------------------------------------------------
# Formatting helpers for the human-readable strings in the payload
# ---------------------------------------------------------------------------

SEV_RANK = {"RED": 0, "YELLOW": 1, "GREEN": 2, "INFO": 3}
_WORDS = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
          "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
          "Eighteen", "Nineteen", "Twenty"]


def _worst(*statuses: str) -> str:
    real = [s for s in statuses if s in ("RED", "YELLOW", "GREEN")]
    return min(real, key=SEV_RANK.get) if real else "GREEN"


def _md(iso: Optional[str]) -> str:
    """'2026-10-25' → 'Oct 25'."""
    if not iso:
        return "never"
    d = date.fromisoformat(iso[:10])
    return f"{d:%b} {d.day}"


def _mon(iso: str) -> str:
    """'2026-06-01' → 'Jun 2026'."""
    return f"{date.fromisoformat(iso[:10]):%b %Y}"


def _money(v: Optional[float]) -> str:
    return "$0" if not v else f"${v:,.0f}"


def _plural(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _list(items: list[str], limit: int = 6) -> str:
    items = [i for i in items if i]
    more = len(items) - limit
    return ", ".join(items[:limit]) + (f" and {more} more" if more > 0 else "")


def _col_letter(i: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


PLATFORM_LABEL = {"google": "Google", "tripadvisor": "TripAdvisor", "yelp": "Yelp"}


# ---------------------------------------------------------------------------
# Section builders. Each takes the query results (r) and returns its slice of
# the payload; `findings` collects the attention rows (one per failing check,
# the same shape the recommended CHECK_RESULTS table would hold).
# ---------------------------------------------------------------------------

def build_pipeline(r: dict, findings: list) -> dict:
    """Checks 1 and 2: table freshness tiles and per-source data lag."""
    today = date.fromisoformat(r["meta"]["today"])
    lag = {row["k"]: row["latest_date"] for row in r["lag"]}

    def days_behind(iso):
        return (today - date.fromisoformat(iso)).days if iso else None

    rev_latest = [v for k, v in lag.items() if k.startswith("reviews:") and v]
    lag_rows = [
        {"source": "Bookings created", "latest": lag.get("bookings"), "expect": 0},
        {"source": "NPS surveys", "latest": lag.get("nps"), "expect": 0},
        *({"source": f"{PLATFORM_LABEL.get(k[8:], k[8:])} reviews", "latest": v, "expect": 0}
          for k, v in sorted(lag.items()) if k.startswith("reviews:")),
        {"source": "Aggregate revenue (past days)", "latest": lag.get("agg"), "expect": 1},
    ]
    for row in lag_rows:
        row["days"] = days_behind(row["latest"])
        late = None if row["days"] is None else row["days"] - row["expect"]
        row["s"] = "RED" if late is None or late > 3 else "YELLOW" if late > 1 else "GREEN"

    names = {
        "AGG_STORES_DAY": ("agg_stores_day", f"Latest day with revenue: {_md(lag.get('agg'))}"),
        "FCT_OPS_BOOKINGS_ADJUSTMENTS": ("fct_ops_bookings_adjustments", f"Latest booking created: {_md(lag.get('bookings'))}"),
        "FCT_NPS_DETAILS": ("fct_nps_details", f"Latest survey: {_md(lag.get('nps'))}"),
        "FCT_STORES_ALL_REVIEWS": ("fct_stores_all_reviews", f"Latest review: {_md(max(rev_latest) if rev_latest else None)} (all platforms)"),
        "DIM_STORES_MASTER": ("dim_stores_master", None),
    }
    tables = []
    for row in sorted(r["freshness"], key=lambda x: list(names).index(x["table_name"])):
        name, meta = names[row["table_name"]]
        hrs = row["hours"] or 0
        tables.append({
            "name": name, "at": row["at_ct"], "ts": row["ts_ct"], "hrs": int(hrs),
            "meta": meta or f"{row['row_count']:,} rows",
            "s": "RED" if hrs > 30 else "YELLOW" if hrs > 24 else "GREEN",
        })
    missing = set(names) - {row["table_name"] for row in r["freshness"]}
    for t in missing:  # a table that vanished from INFORMATION_SCHEMA is as late as it gets
        tables.append({"name": names[t][0], "at": "—", "ts": None, "hrs": 999, "meta": "Not found", "s": "RED"})

    late = [t for t in tables if t["s"] != "GREEN"]
    if late:
        sev = _worst(*(t["s"] for t in late))
        findings.append({
            "sev": sev, "area": "Freshness",
            "finding": f"{_plural(len(late), 'table')} {'has' if len(late) == 1 else 'have'} not refreshed in over 24 hours",
            "detail": _list([f"{t['name']} {t['hrs']}h (last {t['at']} CT)" for t in late]),
            "hint": "The dbt run probably failed or skipped. Check the overnight job and its logs before trusting anything below.",
        })
    lagging = [x for x in lag_rows if x["s"] != "GREEN"]
    if lagging:
        findings.append({
            "sev": _worst(*(x["s"] for x in lagging)), "area": "Data lag",
            "finding": f"{_plural(len(lagging), 'source')} {'is' if len(lagging) == 1 else 'are'} behind even though the tables refreshed",
            "detail": _list([f"{x['source']}: latest {_md(x['latest'])} ({x['days']} days)" for x in lagging]),
            "hint": "The table rebuilt but no new rows arrived upstream. Check the source connector (Fivetran / scraper) for that feed.",
        })
    last_build = max((t["ts"] for t in tables if t["ts"]), default=None)
    return {"TABLES": tables, "LAG": lag_rows, "last_build": last_build}


def build_revenue(r: dict, findings: list, stores: list[dict]) -> dict:
    """Checks 3-7: network line, reconciliation line, heatmap, outliers, unknown revenue."""
    today = date.fromisoformat(r["meta"]["today"])
    yday = (today - timedelta(days=1)).isoformat()

    net = r["network"]
    NET = {
        "d": [f"{date.fromisoformat(x['d']).month}/{date.fromisoformat(x['d']).day}" for x in net],
        "actual": [x["actual"] for x in net],
        "base": [x["base"] for x in net],
        "ratio": [None if (rt := safe_ratio(x["actual"], x["base"])) is None else round(rt, 2) for x in net],
    }
    last_ratio = next((v for v in reversed(NET["ratio"]) if v is not None), None)
    net_s = "GREEN" if last_ratio is None else "RED" if last_ratio < 0.5 else "YELLOW" if last_ratio < 0.65 else "GREEN"
    if net_s != "GREEN":
        findings.append({
            "sev": net_s, "area": "Volume",
            "finding": f"Network revenue on {_md(net[-1]['d'])} is {last_ratio:.2f}x its same-weekday baseline",
            "detail": f"{_money(net[-1]['actual'])} vs {_money(net[-1]['base'])} expected",
            "hint": "A network-wide drop this size is usually a partial load. Check the latest dbt run and the Xola/Revel source loads.",
        })

    rec = r["recon"]
    RECON = {
        "d": [f"{date.fromisoformat(x['d']).month}/{date.fromisoformat(x['d']).day}" for x in rec],
        "ratio": [x["ratio"] for x in rec],
        "trail": [x["trail"] for x in rec],
    }
    recon_last = next((x for x in reversed(rec) if x["ratio"] is not None and x["trail"] is not None), None)
    drift = None if recon_last is None else recon_last["ratio"] - recon_last["trail"]
    recon_s = "GREEN" if drift is None else "RED" if abs(drift) > 0.08 else "YELLOW" if abs(drift) > 0.05 else "GREEN"
    RECON.update({"last": recon_last and recon_last["ratio"], "last_trail": recon_last and recon_last["trail"],
                  "drift": None if drift is None else round(drift, 3), "s": recon_s})
    if recon_s != "GREEN":
        findings.append({
            "sev": recon_s, "area": "Reconciliation",
            "finding": f"Aggregate vs booking net paid is {recon_last['ratio']:.3f} on {_md(recon_last['d'])}, "
                       f"{abs(drift) * 100:.1f} pts from its 28-day average ({recon_last['trail']:.3f})",
            "detail": f"{_money(recon_last['agg_rev'])} aggregate vs {_money(recon_last['paid_net'])} booking net paid",
            "hint": "Drift (not the level) is the signal: look for refund-netting changes, duplicated or dropped bookings, or a join fan-out in the last model change.",
        })

    # heatmap: store -> one [status, rev, base] per day, oldest first, NA-filled
    days = [(today - timedelta(days=i)).isoformat() for i in range(28, 0, -1)]
    cells: dict[str, dict] = {}
    for x in r["heatmap"]:
        cells.setdefault(x["store"], {})[x["d"]] = [x["status"], x["rev"], x["base"]]
    HM = {"days": days, "cells": {s: [cells.get(s, {}).get(d, ["NA", None, None]) for d in days] for s in cells}}

    concept = {s["n"]: s["c"] for s in stores}
    outliers = sorted(
        ({"store": s, "concept": concept.get(s, ""), "rev": v[1], "base": v[2], "st": v[0],
          "ratio": None if (rt := safe_ratio(v[1], v[2])) is None else round(rt, 2)}
         for s, by_day in cells.items() if (v := by_day.get(yday)) and v[0] in ("LOW", "ZERO")),
        key=lambda o: (o["ratio"] if o["ratio"] is not None else 0, o["store"]),
    )
    if outliers:
        findings.append({
            "sev": "YELLOW", "area": "Volume",
            "finding": f"{_plural(len(outliers), 'store')} under 40% of same-weekday average on {_md(yday)}",
            "detail": _list([f"{o['store']} {o['ratio']:.2f}" for o in outliers]),
            "hint": "Could be real (weather, events, a closed day). Confirm bookings and ops data exist before treating it as a pipeline issue.",
        })

    total = next((x for x in r["unknown"] if x["store"] is None), {})
    unk_pct = safe_ratio(total.get("unknown_rev") or 0, total.get("revel_rev"))
    unk_pct = 0.0 if unk_pct is None else unk_pct * 100
    unk_stores = sorted(({"store": x["store"], "rev": round(x["unknown_rev"] or 0, 2), "qty": x["qty"]}
                         for x in r["unknown"] if x["store"] is not None), key=lambda x: -x["rev"])
    unk_s = "RED" if unk_pct >= 5 else "YELLOW" if unk_pct >= 1 else "GREEN"
    if unk_s != "GREEN":
        findings.append({
            "sev": unk_s, "area": "Unattributed revenue",
            "finding": f"{unk_pct:.1f}% of Revel revenue in the last 7 days landed in the unknown bucket",
            "detail": _list([f"{x['store']} {_money(x['rev'])}" for x in unk_stores]),
            "hint": "Products missing from dim_sales_products. Add the new product IDs to the product mapping.",
        })
    UNKNOWN = {"pct": round(unk_pct, 2), "stores": unk_stores, "s": unk_s}
    return {"NET": NET, "NET_S": net_s, "RECON": RECON, "HM": HM, "OUTLIERS": outliers, "UNKNOWN": UNKNOWN}


def build_reviews(r: dict, findings: list) -> dict:
    """Checks 10, 10b, 11, 11b."""
    REV: dict = {p: {"MISSING": [], "MISMATCH": [], "NODATA": [], "STALE": {}} for p in PLATFORM_LABEL}
    for x in r["coverage"]:
        st = x["status"]
        if st == "STALE":
            REV[x["platform"]]["STALE"][x["store"]] = x["days_since"]
        elif st != "OK":
            REV[x["platform"]][st].append(x["store"])
    for d in REV.values():
        for k in ("MISSING", "MISMATCH", "NODATA"):
            d[k].sort()

    def by_plat(k):
        return {p: len(REV[p][k]) for p in REV if REV[p][k]}

    def pairs(k):
        return [f"{s} ({PLATFORM_LABEL[p]})" for p in REV for s in (REV[p][k] if k != "STALE" else [])]

    missing = by_plat("MISSING")
    MAPPROB = [[x["source"], x["sheet_ticker"], x["dim_ticker"] or "(none)", x["platform_id"] or "(blank)", x["problem"]]
               for x in r["map_problems"]]
    blank_n = sum(1 for m in MAPPROB if m[4] == "Blank platform ID")
    if missing:
        findings.append({
            "sev": "RED", "area": "Review mapping",
            "finding": f"{sum(missing.values())} store/platform pairs have no usable mapping row "
                       f"({', '.join(f'{PLATFORM_LABEL[p]} {n}' for p, n in missing.items())})",
            "detail": _list(pairs("MISSING")),
            "hint": f"The mapping sheet has rows for several of these with a blank platform ID ({blank_n} blank rows in total). "
                    "Fill in the IDs; reviews appear after the next production run." if blank_n else
                    "Add a row per store and platform to the review mapping sheet; reviews appear after the next production run.",
        })

    case_rows = [m for m in MAPPROB if m[4] == "Case mismatch"]
    UNMAPPED_ROWS = r["unmapped_reviews"]
    by_dx = {}
    for x in UNMAPPED_ROWS:
        by_dx[x["dx"]] = by_dx.get(x["dx"], 0) + x["reviews"]
    if case_rows:
        sheet_t = sorted({m[1] for m in case_rows})
        dim_t = sorted({m[2] for m in case_rows})
        explained = by_dx.get("MISMATCH", 0)
        findings.append({
            "sev": "RED", "area": "Review mapping",
            "finding": f"{_plural(len(case_rows), 'mapping row')} {'has' if len(case_rows) == 1 else 'have'} a ticker that differs from the store dimension only by case",
            "detail": f"{', '.join(sheet_t)} vs {', '.join(dim_t)}",
            "hint": "These rows exist but never join, so those stores look unmapped. Fix the ticker casing in the sheet."
                    + (f" This explains {_plural(explained, 'unmapped review')} in the last 30 days." if explained else ""),
        })
    other = [m for m in MAPPROB if m[4] == "No matching ticker"]
    if other:
        findings.append({
            "sev": "YELLOW", "area": "Review mapping",
            "finding": f"{_plural(len(other), 'mapping row')} {'uses' if len(other) == 1 else 'use'} a ticker that isn't in the store dimension at all",
            "detail": _list([f"{m[0]} {m[1]}" for m in other]),
            "hint": "Typo, a retired store, or a store that isn't in the store sheet yet.",
        })
    nodata = pairs("NODATA")
    if nodata:
        findings.append({
            "sev": "YELLOW", "area": "Review mapping",
            "finding": f"{_plural(len(nodata), 'pair')} {'is' if len(nodata) == 1 else 'are'} mapped but {'has' if len(nodata) == 1 else 'have'} never received a review",
            "detail": _list(nodata),
            "hint": "Mapping is fine; verify the platform ID points at the right listing, or whether the listing is new.",
        })
    stale = sorted(((d, s, p) for p in REV for s, d in REV[p]["STALE"].items()), reverse=True)
    if stale:
        sp = by_plat("STALE")
        short = {"tripadvisor": "TA", "yelp": "Yelp", "google": "Google"}
        findings.append({
            "sev": "YELLOW", "area": "Review feed",
            "finding": f"{len(stale)} mapped pairs have had no reviews in 30 days despite a prior pace of 3+ per 30 days "
                       f"({', '.join(f'{PLATFORM_LABEL[p]} {n}' for p, n in sp.items())})",
            "detail": _list([f"{s} {short[p]} {d}d" for d, s, p in stale], 5),
            "hint": "Mapping is OK for these, so look at the feed or scraper rather than the sheet.",
        })

    src = {x["source"]: x for x in r["unmapped_src"] if x["kind"] == "review"}
    UNMAPPED = {}
    for p in PLATFORM_LABEL:
        rows = [{"brand": x["brand"], "name": x["name"], "id": x["id"], "url": x["url"], "reviews": x["reviews"],
                 "latest": x["latest"], "ticker": x["ticker"], "dx": x["dx"]}
                for x in UNMAPPED_ROWS if x["platform"] == p]
        UNMAPPED[p] = {"rows": rows, "n": (src.get(p) or {}).get("unmapped_30d", 0),
                       "total": (src.get(p) or {}).get("total_30d", 0),
                       "empty": f"No {PLATFORM_LABEL[p]} reviews landed without a store."}
    if by_dx.get("STALE"):
        stale_rows = [x for x in UNMAPPED_ROWS if x["dx"] == "STALE"]
        findings.append({
            "sev": "YELLOW", "area": "Unmapped rows",
            "finding": f"{_plural(by_dx['STALE'], 'review')} in fct_stores_all_reviews {'has' if by_dx['STALE'] == 1 else 'have'} no store, "
                       "although the platform review table has one",
            "detail": _list([f"{x['name']} ({PLATFORM_LABEL[x['platform']]}, {x['reviews']})" for x in stale_rows]),
            "hint": "The mapping is already fixed; fct_stores_all_reviews isn't re-keying older rows (likely an incremental model). A full refresh of that model clears these.",
        })
    if by_dx.get("NOTIN") or by_dx.get("MATCH"):
        rows = [x for x in UNMAPPED_ROWS if x["dx"] in ("NOTIN", "MATCH")]
        findings.append({
            "sev": "YELLOW", "area": "Unmapped rows",
            "finding": f"{_plural(sum(x['reviews'] for x in rows), 'review')} in 30 days came from listings that don't map to a store",
            "detail": _list([f"{x['name']} ({PLATFORM_LABEL[x['platform']]}, {'not in sheet' if x['dx'] == 'NOTIN' else 'in sheet'})" for x in rows]),
            "hint": "Not in the sheet: add a mapping row. In the sheet with a matching ticker: check the downstream join.",
        })

    gaps = {p: {"map": len(REV[p]["MISSING"]) + len(REV[p]["MISMATCH"]) + len(REV[p]["NODATA"]),
                "stale": len(REV[p]["STALE"])} for p in REV}
    cov_s = "RED" if any(REV[p]["MISSING"] or REV[p]["MISMATCH"] for p in REV) else \
        "YELLOW" if any(REV[p]["NODATA"] or REV[p]["STALE"] for p in REV) else "GREEN"
    return {"REV": REV, "UNMAPPED": UNMAPPED, "MAPPROB": MAPPROB, "COV_S": cov_s, "COV_GAPS": gaps}


def build_nps(r: dict, findings: list, stores: list[dict]) -> dict:
    """Checks 11 (nps rows), 11c, 12a, 12b, 13."""
    today = date.fromisoformat(r["meta"]["today"])
    srcs = [x for x in r["unmapped_src"] if x["kind"] == "nps"]
    unmapped_total = sum(x["unmapped_30d"] for x in srcs)
    rows = r["unmapped_nps"]
    null_loc = sum(x["responses"] for x in rows if x["loc"] == "(null)" and x["gbgs_loc"] == "(null)")
    if unmapped_total:
        named = [x for x in rows if not (x["loc"] == "(null)" and x["gbgs_loc"] == "(null)")]
        by_src = ", ".join(f"{x['source']} {x['unmapped_30d']}" for x in srcs if x["unmapped_30d"])
        findings.append({
            "sev": "YELLOW", "area": "Unmapped rows",
            "finding": f"{_plural(unmapped_total, 'NPS response')} in 30 days {'has' if unmapped_total == 1 else 'have'} no store ({by_src})"
                       + (f"; {null_loc} of them have no location captured at all" if null_loc else ""),
            "detail": _list([f"{x['responses']} say {x['loc'] if x['loc'] != '(null)' else x['gbgs_loc']}" for x in named]) or "All have no location",
            "hint": "A missing location field is a survey-capture problem, not a store mapping. Named locations usually mean a new store or a ticker casing issue in the mapping.",
        })

    v = (r["nps_valid"] or [{}])[0]
    valid_bad = {k: v.get(k, 0) for k in ("duplicate_ids", "null_rating", "out_of_range") if v.get(k)}
    if valid_bad:
        findings.append({
            "sev": "RED" if valid_bad.get("duplicate_ids") else "YELLOW", "area": "NPS validity",
            "finding": f"NPS responses (30d) fail validity checks: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in valid_bad.items()),
            "detail": f"{v.get('rows_checked', 0):,} responses checked",
            "hint": "Duplicates usually mean a re-sync of the survey source; out-of-range ratings mean a scale change upstream.",
        })

    none = r["nps_none"]
    no_nps = [x for x in none if x["bookings_14d"] > 0 and x["nps_30d"] == 0]
    no_bookings = [x for x in none if x["bookings_14d"] == 0]
    if no_nps:
        findings.append({
            "sev": "YELLOW", "area": "NPS coverage",
            "finding": f"{_plural(len(no_nps), 'store')} with bookings in 14 days {'has' if len(no_nps) == 1 else 'have'} no NPS responses in 30 days",
            "detail": _list([x["store"] for x in no_nps]),
            "hint": "The survey feed for the store stopped or never started. New stores often need adding to the survey location list.",
        })
    if no_bookings:
        findings.append({
            "sev": "YELLOW", "area": "Store setup",
            "finding": f"{_plural(len(no_bookings), 'active store')} {'has' if len(no_bookings) == 1 else 'have'} no bookings arriving in the last 14 days",
            "detail": _list([x["store"] for x in no_bookings]),
            "hint": "A store that is open but has no bookings usually has a seller or store key that changed. Check the store sheet's seller IDs.",
        })

    # 12a, zero-filled, with the trailing 4-week average computed after the fill
    this_week = today - timedelta(days=today.weekday())
    weeks = [(this_week - timedelta(weeks=i)).isoformat() for i in range(9, -1, -1)]
    counts: dict[str, dict] = {}
    for x in r["nps_weekly"]:
        counts.setdefault(x["store"], {})[x["week_start"]] = x["responses"]
    WK = {}
    for s in stores:
        series = [counts.get(s["n"], {}).get(w, 0) for w in weeks]
        cells = []
        for i, n in enumerate(series):
            prior = series[max(0, i - 4):i]
            avg = sum(prior) / len(prior) if len(prior) == 4 else None
            if i == len(series) - 1:
                st = "NA"  # current week is partial: shown, never flagged
            elif avg is None or avg < 1:
                st = "NA"
            elif n == 0:
                st = "ZERO"
            elif n / avg < 0.4:
                st = "LOW"
            else:
                st = "OK"
            cells.append([st, n, None if avg is None else round(avg, 1)])
        WK[s["n"]] = cells
    last_full = [WK[s][-2] for s in WK]
    NPS_WEEKS = {"weeks": weeks, "cells": WK,
                 "zero": sum(1 for c in last_full if c[0] == "ZERO"), "low": sum(1 for c in last_full if c[0] == "LOW")}

    return {"NPS_UNMAPPED": rows, "NPS_SRC": srcs, "NPS_UNMAPPED_N": unmapped_total, "NPS_NULL_LOC": null_loc,
            "NPS_NONE": no_nps, "NO_BOOKINGS": no_bookings, "NPS_VALID": v, "NPS_WEEKS": NPS_WEEKS}


def build_integrity(r: dict, findings: list, sellers: list[dict]) -> dict:
    """Checks 8, 8b, 9."""
    b = (r["bookings_int"] or [{}])[0]
    orphans = r["orphans"]
    seller_ids = {s["x_seller_id"] for s in sellers}
    if b.get("duplicate_ids") or b.get("null_store"):
        findings.append({
            "sev": "RED", "area": "Integrity",
            "finding": f"Bookings (30d) have {b.get('duplicate_ids', 0)} duplicate IDs and {b.get('null_store', 0)} rows with no store at all",
            "detail": f"{b.get('rows_checked', 0):,} rows checked",
            "hint": "Duplicates usually mean a join fan-out in the bookings model; null stores mean the store key lookup failed. Check the latest model change.",
        })
    if b.get("orphan_store_rows"):
        new_sellers = [o for o in orphans if o["x_seller_id"] in seller_ids]
        findings.append({
            "sev": "YELLOW", "area": "Integrity",
            "finding": f"{_plural(b['orphan_store_rows'], 'booking row')} (30d) point to a store key that is not in dim_stores_master",
            "detail": f"{_plural(len(orphans), 'distinct key')}" + (": the new Xola seller below" if new_sellers and len(new_sellers) == len(orphans) else ""),
            "hint": "Same cause as the unmatched Xola seller: clears once the seller ID is in the store sheet." if new_sellers else
                    "A store key the dimension doesn't know. Usually a new store or seller that isn't in the store sheet yet.",
        })
    if b.get("refund_exceeds_paid"):
        findings.append({
            "sev": "YELLOW", "area": "Integrity",
            "finding": f"{_plural(b['refund_exceeds_paid'], 'booking row')} (30d) {'has' if b['refund_exceeds_paid'] == 1 else 'have'} a refund larger than the amount paid",
            "detail": "Spot-check a few rows",
            "hint": "Refund netting logic, or partial payments with full refunds. Spot-check a few.",
        })
    if b.get("null_amount_paid"):
        findings.append({
            "sev": "YELLOW", "area": "Integrity",
            "finding": f"{_plural(b['null_amount_paid'], 'booking row')} (30d) {'has' if b['null_amount_paid'] == 1 else 'have'} no amount paid",
            "detail": "Spot-check a few rows", "hint": "Usually a payment record that didn't join. Check the payments source.",
        })
    a = (r["agg_int"] or [{}])[0]
    if a.get("duplicate_store_days") or a.get("null_revenue_rows_7d"):
        findings.append({
            "sev": "RED" if a.get("duplicate_store_days") else "YELLOW", "area": "Integrity",
            "finding": f"agg_stores_day has {a.get('duplicate_store_days', 0)} duplicate store-days and "
                       f"{a.get('null_revenue_rows_7d', 0)} null-revenue rows in the last 7 days",
            "detail": "Duplicates double-count every dashboard built on the aggregate",
            "hint": "A duplicated dimension row (often the store sheet) fanning out the aggregate. Check dim_stores_master for repeated IDs.",
        })
    book_s = "RED" if b.get("duplicate_ids") or b.get("null_store") else \
        "YELLOW" if b.get("orphan_store_rows") or b.get("refund_exceeds_paid") or b.get("null_amount_paid") else "GREEN"
    agg_s = "RED" if a.get("duplicate_store_days") else "YELLOW" if a.get("null_revenue_rows_7d") else "GREEN"
    return {"BOOK_INT": {**b, "orphans": orphans, "s": book_s}, "AGG_INT": {**a, "s": agg_s}}


def _suggestions(x: dict) -> dict:
    """Check 16: one strip of suggested values for an unmatched seller's sheet row."""
    vals = x.get("row_values")
    vals = json.loads(vals) if isinstance(vals, str) else (vals or {})
    cols = [{"i": i, "l": _col_letter(i), "h": h, "cur": _cell_text(vals.get(c)) if c else ""}
            for i, (h, c) in enumerate(SHEET_COLUMNS, start=1)]
    by_h = {c["h"]: c for c in cols}
    sug: dict[int, dict] = {}

    def put(header, value, conf, note):
        c = by_h[header]
        if value in (None, "") or c["cur"]:  # never suggest over a value the sheet already has
            return
        sug[c["i"]] = {"v": _cell_text(value), "c": conf, "n": note}

    city, name, concept = x["city"], x["seller_name"], x["concept"]
    n, ticker, st = x.get("next_no"), x.get("ticker"), x.get("st")
    brand = "Great Big Game Show" if concept == "GBGS" else "The Escape Game"
    for h in ("i3 Merchant Name_Revel", "i3 Merchant Name_Xola"):
        put(h, name, "confirm", "Xola seller name. The i3 and Revel merchant names don't always match it, so check.")
    for h, where in (("Hubspot Name", "HubSpot"), ("Paycor Name", "Paycor"), ("Marketing Name", None),
                     ("_ADP Name", "ADP"), ("_Shopify Market", None)):
        put(h, city, "confirm", "The city name, which is what recent rows use." + (f" Check against {where}." if where else ""))
    put("City", city, "xola", "City on the Xola seller record.")
    put("_Xola Store Name", name, "xola", "Name on the Xola seller record.")
    put("_Concept", concept, "xola", f"The seller name starts with {brand}.")
    put("_State", st, "xola", f"State on the Xola seller record ({x['state_name']}), as the sheet abbreviates it.")
    if x.get("street") and st:
        put("_Store Address", f"{x['street']}, {city}, {st} {x.get('zip') or ''}".strip(), "xola",
            "Xola address, written the way the sheet writes addresses.")
    put("_gbgs_xola_seller_id" if concept == "GBGS" else "xola_seller_id", x["seller_id"], "xola",
        "The Xola seller ID, the one that is missing from the sheet.")
    if concept == "GBGS":
        put("xola_seller_id", x["seller_id"], "pattern", "GBGS rows usually repeat their seller ID here too.")
    if n:
        put("_Store Order", n, "pattern", f"The next number after the highest {concept} store ({n - 1}).")
        put("Store Number", n, "pattern", "Same number as _Store Order.")
        lo = (n - 1) // 5 * 5 + 1
        put("_Every 5", f"({lo}-{lo + 4})", "pattern", f"Stores {lo} to {lo + 4} share this bucket.")
    if ticker:
        note = f"{ticker} already has rows in the staffing budget sheet and no store uses it yet."
        put("Ticker (FIN)", ticker, "confirm", note)
        put("Ticker (OPS)", ticker, "confirm", "Same as Ticker (FIN) in every recent row.")
        if n:
            put("_TEG Ticker", f"{ticker} ({n})", "pattern", "Ticker followed by the store number.")
            put("NetSuite Name", f"{n:04d} {ticker}", "pattern", "Four-digit store number plus ticker.")
    if x.get("neighbour"):
        nb = x["neighbour"]
        for h, key in (("_Zone", "zone"), ("_Zone 2026", "zone_2026"), ("Region", "region")):
            put(h, x.get(key), "confirm", f"{nb}, the most recently opened {concept} store in {st}, has this.")
        put("_Region Leader", x.get("leader"), "confirm", f"Leader for that zone, per the {nb} row.")
        put("_Area", x.get("area"), "confirm", f"{nb}, in the same state, has this.")
    tiers = x.get("tiers")
    tiers = json.loads(tiers) if isinstance(tiers, str) else (tiers or [])
    if len(tiers) == 1:
        put("_Tier 2025", tiers[0], "pattern", f"The three most recent {concept} stores ({x['tier_from']}) are all tier {tiers[0]}.")
    opened = by_h["_Date Opened"]["cur"]
    try:
        opened_d = datetime.strptime(opened, "%m/%d/%Y").date() if opened else None
    except ValueError:
        opened_d = None
    if opened_d and opened_d > date.today():
        put("_Store Status", x.get("unopened_status"), "pattern", "What rows with a future opening date use.")
        put("_Store Year", x.get("unopened_year"), "pattern", "What rows with a future opening date use.")
    if opened_d:
        put("_Vintage", opened_d.year, "pattern", f"Opening year in the sheet ({opened}).")
    if concept == "TEG" and x.get("twin_xola_store_id"):
        put("_gbgs_redirect_store_id", x["twin_xola_store_id"], "confirm",
            f"TEG rows with a GBGS twin hold the twin's xola_store_id ({x['twin_name']}). Only if the two share a building.")

    cur_notes = {}
    if opened:
        cur_notes[by_h["_Date Opened"]["i"]] = ("Check this is the real opening date (12/31 is the sheet's placeholder). "
                                               "It also drives _First Comp, _Vintage and _Latest Slot.")
    needs = {
        by_h["_longitude"]["i"]: "Left blank on purpose. Xola's coordinates are city-level (about 4 km off), so use the Google Places lookup.",
        by_h["_latitude"]["i"]: "Left blank on purpose. Xola's coordinates are city-level (about 4 km off), so use the Google Places lookup.",
        by_h["_GM"]["i"]: "Not derivable from the data.",
        by_h["_ADP Location"]["i"]: "Not derivable from the data.",
        by_h["_google_places_id"]["i"]: "Needs a Google Places lookup.",
    }
    still = [c["h"] for c in cols if not c["cur"] and c["i"] not in sug]
    return {
        "seller_id": x["seller_id"], "store": x.get("row_name") or city,
        "row": (x["row_line"] + SHEET_ROW_OFFSET) if x.get("row_line") is not None else None,
        "cols": cols, "sug": {str(k): v for k, v in sug.items()}, "curNotes": {str(k): v for k, v in cur_notes.items()},
        "needsNotes": {str(k): v for k, v in needs.items()},
        "stillNeeds": _list(still, 12),
    }


def build_store_setup(r: dict, findings: list) -> dict:
    """Checks 15a, 15b, 15c, 16."""
    sellers = r["sellers"]
    for s in sellers:
        s["s"] = "RED" if s["booking_rows"] else "YELLOW"
    sg = [_suggestions(x) for x in r["suggest"]]
    sg_by_id = {x["seller_id"]: x for x in sg}
    taking = [s for s in sellers if s["booking_rows"]]
    if taking:
        s0 = taking[0]
        sgx = sg_by_id.get(s0["x_seller_id"])
        findings.append({
            "sev": "RED", "area": "Store setup",
            "finding": f"{_plural(len(taking), 'Xola seller')} {'is' if len(taking) == 1 else 'are'} taking bookings but "
                       f"{'is' if len(taking) == 1 else 'are'}n't in the store sheet: " + _list([s["seller_name"] for s in taking], 3),
            "detail": f"x_seller_id {s0['x_seller_id']}, {s0['booking_rows']} booking rows ({_money(s0['paid'])}), first arrival {_md(s0['first_arrival'])}",
            "hint": (f"Paste the ID into xola_seller_id on the {sgx['store']} row (row {sgx['row']}) of the store sheet. "
                     f"The Integrity tab has suggested values for {len(sgx['sug']) - 1} other columns you can copy.")
                    if sgx and sgx["row"] else
                    "Paste the ID into xola_seller_id on the matching row of the store sheet, then fill in ticker, concept, status and address.",
        })
    waiting = [s for s in sellers if not s["booking_rows"]]
    if waiting:
        findings.append({
            "sev": "YELLOW", "area": "Store setup",
            "finding": f"{_plural(len(waiting), 'Xola seller')} {'exists' if len(waiting) == 1 else 'exist'} in Xola but not in the store sheet (no bookings yet)",
            "detail": _list([s["seller_name"] for s in waiting]),
            "hint": "Add the seller ID to the store sheet before the store starts taking bookings.",
        })
    ph = [x for x in r["placeholders"] if x["k"] == "placeholder"]
    ph_booking = [x for x in ph if x["booking_rows"]]
    unknown = [x for x in r["placeholders"] if x["k"] == "unknown_seller"]
    if ph_booking:
        findings.append({
            "sev": "YELLOW", "area": "Store setup",
            "finding": f"{_plural(len(ph_booking), 'placeholder row')} in the store sheet {'is' if len(ph_booking) == 1 else 'are'} already taking bookings",
            "detail": _list([f"{x['name']} (missing {x['missing']})" for x in ph_booking]),
            "hint": "Fill in the missing concept, ticker or seller ID so the store's bookings attribute correctly.",
        })
    if unknown:
        findings.append({
            "sev": "YELLOW", "area": "Store setup",
            "finding": f"{_plural(len(unknown), 'seller ID')} on recent bookings {'is' if len(unknown) == 1 else 'are'} unknown to both the store sheet and Xola",
            "detail": _list([f"{x['name']} ({x['booking_rows']} rows)" for x in unknown]),
            "hint": "Normally empty. Check whether the Xola seller table sync is behind.",
        })
    return {"SELLERS": sellers, "SG": sg[0] if sg else None, "SG_ALL": [x["store"] for x in sg],
            "PLACEHOLDER": {"total": len(ph), "with_bookings": ph_booking, "unknown_sellers": unknown}}


def build_targets(r: dict, findings: list, stores: list[dict]) -> dict:
    """Checks 14 and 14c."""
    today = date.fromisoformat(r["meta"]["today"])
    tm = date.fromisoformat(r["meta"]["target_month"])
    TG: dict[str, dict] = {}
    for x in r["targets"]:
        TG.setdefault(x["store"], {})[x["k"]] = "L" if x["in_target"] else "M" if x["in_prior"] else "N"
    concept = {s["n"]: s["c"] for s in stores}

    def applies(t, store):
        return t["appl"] == "BOTH" or t["appl"] == concept.get(store)

    days_to = (tm - today).days
    sev_incomplete = "RED" if days_to <= 7 else "YELLOW"
    cards = []
    for t in TARGET_TYPES:
        app = [s for s in TG if applies(t, s)]
        ok = sum(1 for s in app if TG[s].get(t["k"]) == "L")
        cards.append({"k": t["k"], "label": t["label"], "app": len(app), "ok": ok,
                      "state": "GREEN" if app and ok == len(app) else "GREY" if ok == 0 else "YELLOW"})
    done = [c for c in cards if c["state"] == "GREEN"]
    none = [c for c in cards if c["state"] == "GREY"]
    partial = [c for c in cards if c["state"] == "YELLOW"]
    month = f"{tm:%b}"
    if len(done) < len(cards):
        when = f"{month} 1 is {_plural(days_to, 'day')} away" if days_to > 0 else f"{tm:%B} has started"
        findings.append({
            "sev": sev_incomplete, "area": f"Targets ({month})",
            "finding": (f"{len(none)} of {len(cards)} target types have no {tm:%B} targets for any store: "
                        + ", ".join(c["label"].lower().replace(" target", "") for c in none)) if none else
                       f"{len(partial)} of {len(cards)} target types are only partly loaded for {tm:%B}",
            "detail": "Loaded so far: " + (", ".join(f"{c['label'].lower()} {c['ok']}/{c['app']}" for c in partial) or "nothing"),
            "hint": f"Load these before {month} 1 so dashboards have targets on day 1. {when}; turns red 7 days before the month starts.",
        })
    ORPHAN_T = [[x["ticker"], x["types"]] for x in r["target_orphans"]]
    return {"TTYPES": TARGET_TYPES, "TG": TG, "ORPHAN_T": ORPHAN_T,
            "TARGET": {"month": tm.isoformat(), "label": f"{tm:%B %Y}", "short": month, "days_to": days_to,
                       "done": len(done), "partial": len(partial), "none": len(none),
                       "s": "GREEN" if len(done) == len(cards) else sev_incomplete}}


def build_upkeep(r: dict, findings: list) -> dict:
    """Checks 17a, 17b, 18, 19, 19b and guest care."""
    today = date.fromisoformat(r["meta"]["today"])
    issues, next_blank = [], []
    for x in r["slots"]:
        tick, col, sim = x["ticker"], x["sheet_column"], x["similar_col"]
        info = x["team_info_slots"]
        if not col and sim:
            sheet, fix, kind = f"Column is {sim}, ticker is {tick}", "Make the header and the ticker match, then fill in this month.", "named"
        elif not col:
            sheet, kind = f"No column ({tick})", "missing"
            fix = f"Add a {tick} column to the Historical slots sheet" + (f" with {info} from {today:%B %Y}." if info else ".")
        elif not x["sheet_this_month"]:
            sheet, fix, kind = f"Column {col}, {today:%B} blank", f"Fill in {today:%B} for {col}.", "blank"
        elif not x["agg_slots"]:
            sheet, kind = f"Column {col} has {_cell_text(x['sheet_this_month'])}", "unpicked"
            fix = (f"The sheet has values but the aggregate shows 0. The header ({col}) and ticker ({tick}) differ; "
                   "check how the model maps columns to stores.")
        else:
            next_blank.append(x)
            continue
        issues.append({"store": x["store"], "agg": _cell_text(x["agg_slots"]) or "0", "info": _cell_text(info) or "—",
                       "sheet": sheet, "fix": fix, "kind": kind})
    if issues:
        miss = [i for i in issues if i["kind"] in ("missing", "named")]
        unpicked = [i for i in issues if i["kind"] == "unpicked"]
        blank = [i for i in issues if i["kind"] == "blank"]
        parts = []
        if miss:
            parts.append(f"{len(miss)} {'is' if len(miss) == 1 else 'are'} missing or named differently in the Historical slots sheet")
        if blank:
            parts.append(f"{len(blank)} {'has' if len(blank) == 1 else 'have'} this month blank")
        if unpicked:
            parts.append(f"{len(unpicked)} {'has' if len(unpicked) == 1 else 'have'} values the aggregate is not picking up")
        findings.append({
            "sev": "RED", "area": "Slots",
            "finding": f"{_plural(len(issues), 'open store')} show 0 slots in the aggregate: " + ", ".join(parts),
            "detail": _list([f"{i['sheet']} ({i['store']})" for i in issues]),
            "hint": "Add missing columns and line the other headers up with the tickers."
                    + (" The sheet already has values for some of them, so also check how the model maps columns to stores." if unpicked else ""),
        })
    if next_blank:
        findings.append({
            "sev": "YELLOW", "area": "Slots",
            "finding": f"{_plural(len(next_blank), 'store')} {'has' if len(next_blank) == 1 else 'have'} next month blank in the Historical slots sheet",
            "detail": _list([x["store"] for x in next_blank]),
            "hint": "Fill in next month's slots before the month starts.",
        })
    mism = [[x["store"], x["team_info"], x["sheet"]] for x in r["slot_mismatch"]]
    if mism:
        findings.append({
            "sev": "YELLOW", "area": "Slots",
            "finding": f"{_plural(len(mism), 'store')} {'has' if len(mism) == 1 else 'have'} different slots in Store Team Info and the Historical slots sheet",
            "detail": _list([f"{m[0]} {_cell_text(m[1])} vs {_cell_text(m[2])}" for m in mism]),
            "hint": "One sheet was updated and the other was not. Decide which is right and update the other.",
        })

    cov, clean = [], []
    for x in r["sheet_cov"]:
        missing = x["missing"]
        missing = json.loads(missing) if isinstance(missing, str) else (missing or [])
        if missing:
            cov.append({"label": x["sheet"], "missing": missing})
        else:
            clean.append(x["sheet"])
    cov_stores = sorted({s for c in cov for s in c["missing"]})
    if cov_stores:
        worst = sorted(cov_stores, key=lambda s: -sum(s in c["missing"] for c in cov))
        findings.append({
            "sev": "YELLOW", "area": "Sheet coverage",
            "finding": f"{_plural(len(cov_stores), 'store')} {'is' if len(cov_stores) == 1 else 'are'} missing a column in at least one per-store sheet "
                       f"({', '.join(c['label'] for c in cov)})",
            "detail": _list([f"{s} ({sum(s in c['missing'] for c in cov)})" for s in worst], 6),
            "hint": "A new store needs a column in each of these sheets. The checklist is on the Sheet upkeep tab.",
        })

    # monthly sheets: where should each one be by now?
    this_m = today.replace(day=1)
    last_m = (this_m - timedelta(days=1)).replace(day=1)
    before_m = (last_m - timedelta(days=1)).replace(day=1)
    rows = {x["sheet"]: x for x in r["monthly"] if not x["sheet"].startswith("GM score")}
    gm = [x for x in r["monthly"] if x["sheet"].startswith("GM score")]
    if gm:
        latest = max(x["latest_month"] for x in gm if x["latest_month"]) if any(x["latest_month"] for x in gm) else None
        ns = sorted({x["stores_filled"] for x in gm if x["latest_month"] == latest and x["stores_filled"]})
        rows["GM scores (four tabs)"] = {"latest_month": latest,
                                         "stores_filled": f"{ns[0]} to {ns[-1]}" if len(ns) > 1 else (ns[0] if ns else 0)}
    MONTHLY = []
    for sheet, kind in MONTHLY_SHEETS.items():
        x = rows.get(sheet) or {"latest_month": None, "stores_filled": 0}
        latest = date.fromisoformat(x["latest_month"]) if x["latest_month"] else None
        reach = {"actuals": last_m if today.day > 15 else before_m, "current": this_m, "prior": last_m,
                 "budget": date(today.year, 12, 1), "exception": None}[kind]
        if kind == "exception":
            s, note = "INFO", "Exceptions only."
        elif latest and latest >= reach:
            s = "GREEN"
            note = {"budget": "Full year loaded.", "current": f"{(this_m + timedelta(days=32)):%B} is needed before the 1st.",
                    "prior": f"{this_m:%B} is due early next month."}.get(kind, "Current.")
        else:
            s = "YELLOW"
            prev = x.get("prior_filled")
            note = (f"Last filled in {latest:%B} ({_plural(int(x['stores_filled'] or 0), 'store')}), nothing since."
                    if latest else "No values found.")
        MONTHLY.append({"sheet": sheet, "latest": _mon(x["latest_month"]) if x["latest_month"] else "None",
                        "n": _cell_text(x["stores_filled"]), "reach": "exceptions only" if kind == "exception" else f"{reach:%b %Y}",
                        "s": s, "note": note})
    yday = today - timedelta(days=1)
    for g in r["guest_care"]:
        latest = date.fromisoformat(g["latest"]) if g["latest"] else None
        behind = (yday - latest).days if latest else None
        s = "YELLOW" if behind is None or behind > 3 else "GREEN"
        note = (f"No {today.year} rows" + (" (rows carry another year's dates)" if not g["rows_this_year"] else " with revenue") + "."
                if latest is None else f"{_plural(behind, 'day')} behind." if behind > 0 else "Current.")
        MONTHLY.append({"sheet": f"Guest care revenue, {g['concept']}", "latest": f"{latest:%b} {latest.day}, {latest.year}" if latest else f"None for {today.year}",
                        "n": "daily" if latest else "0", "reach": f"{yday:%b} {yday.day}, {yday.year}", "s": s, "note": note})
    behind = [m for m in MONTHLY if m["s"] == "YELLOW"]
    gc = [m for m in behind if m["sheet"].startswith("Guest care")]
    sheets = [m for m in behind if not m["sheet"].startswith("Guest care")]
    if sheets:
        findings.append({
            "sev": "YELLOW", "area": "Monthly sheets",
            "finding": f"{_plural(len(sheets), 'monthly sheet')} {'is' if len(sheets) == 1 else 'are'} behind: "
                       + _list([f"{m['sheet']} (latest {m['latest']})" for m in sheets]),
            "detail": " ".join(m["note"] for m in sheets[:3]),
            "hint": "Load the missing months, or confirm a sheet is meant to be empty.",
        })
    if gc:
        findings.append({
            "sev": "YELLOW", "area": "Guest care sheets",
            "finding": "Guest care data is behind: " + _list([
                f"{m['sheet'].split(', ')[1]} has no {today.year} data" if m["latest"].startswith("None")
                else f"{m['sheet'].split(', ')[1]} stops at {m['latest'].rsplit(',', 1)[0]}" for m in gc]),
            "detail": " ".join(f"{m['sheet'].split(', ')[1]}: {m['note']}" for m in gc),
            "hint": "Fill in the missing days, and check the dates on any rows that carry the wrong year.",
        })
    slot_s = "RED" if issues else "YELLOW" if mism or next_blank else "GREEN"
    return {"SLOT_ISSUES": issues, "SLOT_MISMATCH": mism, "SLOT_NEXT_BLANK": len(next_blank),
            "SHEET_COV": cov, "SHEET_COV_CLEAN": clean, "MONTHLY": MONTHLY,
            "UPKEEP_S": _worst(slot_s, "YELLOW" if cov else "GREEN", "YELLOW" if behind else "GREEN"), "SLOT_S": slot_s}


def _stores(r: dict) -> list[dict]:
    """The 90-ish scope stores, TEG first then GBGS, from Check 10's rows."""
    seen = {}
    for x in r["coverage"]:
        seen[x["store"]] = x["concept"]
    return sorted(({"n": n, "c": c} for n, c in seen.items()), key=lambda s: (s["c"] != "TEG", s["n"].lower()))


def _summary(p: dict, findings: list) -> dict:
    """Hero text and the category strip, from the same results as the tabs."""
    red = sum(1 for f in findings if f["sev"] == "RED")
    yellow = sum(1 for f in findings if f["sev"] == "YELLOW")
    tables = p["TABLES"]
    late = [t for t in tables if t["s"] != "GREEN"]
    oldest = max(tables, key=lambda t: t["hrs"]) if tables else None
    n = len(findings)
    h1 = ("Everything is on time." if not late else f"{_WORDS[min(len(late), 20)]} {'table is' if len(late) == 1 else 'tables are'} late.") + \
         ("<br>Nothing needs a look." if n == 0 else f"<br>{_WORDS[n] if n <= 20 else n} {'thing needs' if n == 1 else 'things need'} a look.")
    build = p["last_build"][11:16] if p["last_build"] else "—"
    reds = [f for f in findings if f["sev"] == "RED"]
    red_areas = list(dict.fromkeys(f["area"].lower() for f in reds))
    para = f"The last build finished at {build} CT. "
    if not red and not yellow:
        para += "Nothing is red or yellow."
    else:
        para += (f"{_WORDS[red] if red <= 20 else red} {'issue is' if red == 1 else 'issues are'} red and "
                 f"{_WORDS[yellow].lower() if yellow <= 20 else yellow} {'is' if yellow == 1 else 'are'} yellow"
                 + (", including " + _list(red_areas, 3) + "." if red_areas else "."))

    rev = p["REV"]
    gaps = sum(v["map"] + v["stale"] for v in p["COV_GAPS"].values())
    map_n = sum(v["map"] for v in p["COV_GAPS"].values())
    stale_n = sum(v["stale"] for v in p["COV_GAPS"].values())
    stale_plats = [PLATFORM_LABEL[k] for k in rev if rev[k]["STALE"]]
    integ = [("unmatched seller", "RED" if any(s["booking_rows"] for s in p["SELLERS"]) else "YELLOW" if p["SELLERS"] else "GREEN"),
             ("unmapped NPS", "YELLOW" if p["NPS_UNMAPPED_N"] else "GREEN"),
             ("orphan keys", "YELLOW" if p["BOOK_INT"].get("orphan_store_rows") else "GREEN"),
             ("refunds", "YELLOW" if p["BOOK_INT"].get("refund_exceeds_paid") else "GREEN"),
             ("bookings duplicates", "RED" if p["BOOK_INT"]["s"] == "RED" else "GREEN"),
             ("aggregate", p["AGG_INT"]["s"])]
    open_ = [k for k, s in integ if s != "GREEN"]
    T = p["TARGET"]
    slot0 = len(p["SLOT_ISSUES"])
    cov_n = len({s for c in p["SHEET_COV"] for s in c["missing"]})
    behind = sum(1 for m in p["MONTHLY"] if m["s"] == "YELLOW")
    R = p["RECON"]
    fresh_s = _worst(*(t["s"] for t in tables), *(x["s"] for x in p["LAG"]))
    cats = [
        {"name": "Freshness", "s": fresh_s, "v": "All current" if not late else f"{len(late)} late",
         "d": f"Oldest table refreshed {oldest['hrs']}h ago" if oldest else ""},
        {"name": "Coverage", "s": p["COV_S"], "v": _plural(gaps, "gap"),
         "d": f"{map_n} mapping problems, {stale_n} feed stale" + (f" ({' + '.join(stale_plats)})" if stale_plats else "")},
        {"name": "Volume", "s": _worst("YELLOW" if p["OUTLIERS"] else "GREEN", p["NET_S"]),
         "v": f"{len(p['OUTLIERS'])} stores low", "d": "<40% of same-weekday average yesterday"},
        {"name": "Integrity", "s": _worst(*(s for _, s in integ)), "v": _plural(len(open_), "check") + " open",
         "d": (lambda t: t[:1].upper() + t[1:])(", ".join(open_[:4])) if open_ else "Nothing flagged"},
        {"name": f"Targets ({T['short']})", "s": T["s"], "v": f"{T['done']} / {len(TARGET_TYPES)} complete",
         "d": f"{T['partial']} partly loaded, {T['none']} not loaded. "
              + (f"{T['short']} 1 is {T['days_to']} days away" if T["days_to"] > 0 else f"{T['label']} has started")},
        {"name": "Sheet upkeep", "s": p["UPKEEP_S"], "v": f"{slot0} stores, 0 slots" if slot0 else f"{cov_n} stores missing",
         "d": f"{cov_n} stores missing from sheets, {behind} monthly sheets behind"},
        {"name": "Reconciliation", "s": R["s"], "v": f"{R['last']:.3f}" if R["last"] is not None else "—",
         "d": (f"{'Within' if abs(R['drift']) <= 0.05 else 'Off by'} {abs(R['drift']) * 100:.1f} pts of 28-day avg ({R['last_trail']:.3f})"
               if R["drift"] is not None else "No overlapping days")},
    ]
    status = "RED" if red else "YELLOW" if yellow else "GREEN"
    return {"status": status, "red": red, "yellow": yellow, "h1": h1, "para": para, "cats": cats,
            "fresh": sum(1 for t in tables if t["s"] == "GREEN"), "build": build}


# Section → (queries it needs, builder). The dashboard runs every query once,
# concurrently, then every builder; /checks/{section} runs just one section.
SECTIONS = {
    "pipeline":    ["freshness", "lag"],
    "revenue":     ["network", "recon", "heatmap", "unknown", "coverage"],
    "reviews":     ["coverage", "map_problems", "unmapped_src", "unmapped_reviews"],
    "nps":         ["unmapped_src", "unmapped_nps", "nps_weekly", "nps_valid", "nps_none", "coverage"],
    "store_setup": ["sellers", "placeholders", "suggest"],
    "integrity":   ["bookings_int", "orphans", "agg_int", "sellers"],
    "targets":     ["targets", "target_orphans", "coverage"],
    "upkeep":      ["slots", "slot_mismatch", "sheet_cov", "monthly", "guest_care"],
}


def _build(r: dict, sections: list[str]) -> dict:
    findings: list[dict] = []
    p: dict = {}
    stores = _stores(r) if "coverage" in r else []
    if "pipeline" in sections:
        p.update(build_pipeline(r, findings))
    if "store_setup" in sections:
        p.update(build_store_setup(r, findings))
    if "upkeep" in sections:
        p.update(build_upkeep(r, findings))
    if "reviews" in sections:
        p.update(build_reviews(r, findings))
    if "targets" in sections:
        p.update(build_targets(r, findings, stores))
    if "nps" in sections:
        p.update(build_nps(r, findings, stores))
    if "revenue" in sections:
        p.update(build_revenue(r, findings, stores))
    if "integrity" in sections:
        p.update(build_integrity(r, findings, r.get("sellers", [])))
    # stable sort: severity first, then the order the builders added them
    findings.sort(key=lambda f: SEV_RANK[f["sev"]])
    p["ATTN"] = findings
    p["STORES"] = stores
    return p


def get_dashboard(target_month: Optional[str] = None) -> dict:
    """Every check, shaped for the page. The default (next month's targets)
    is what api/refresh.py caches."""
    t0 = time.time()
    names = sorted({q for qs in SECTIONS.values() for q in qs})
    r = run_queries(names, target_month)
    p = _build(r, list(SECTIONS))
    p["SUMMARY"] = _summary(p, p["ATTN"])
    m = r["meta"]
    p["META"] = {"generated_at": m["now_ct"], "today": m["today"], "target_month": m["target_month"],
                 "store_count": len(p["STORES"]), "query_seconds": r["_timings"], "total_seconds": round(time.time() - t0, 1)}
    return p


def get_default_dashboard() -> dict:
    return get_dashboard(None)


def get_section(section: str, target_month: Optional[str] = None) -> dict:
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail=f"Unknown section {section!r}; one of {', '.join(SECTIONS)}")
    r = run_queries(SECTIONS[section], target_month)
    p = _build(r, [section])
    p["META"] = {"generated_at": r["meta"]["now_ct"], "today": r["meta"]["today"],
                 "target_month": r["meta"]["target_month"], "query_seconds": r["_timings"]}
    return p


# ---------------------------------------------------------------------------
# Auth — every /api/* route requires a verified Cognito id_token.
#
# The CloudFront Function only gates the page (and only checks that an
# id_token cookie exists), lets /api/* through, and the Lambda Function URL is
# authorization_type NONE. So without this check anyone with the URL could
# read the whole payload. The page sends the token as
# `Authorization: Bearer <id_token>` (CloudFront forwards that header on
# /api/*). Same approach as auto-payroll-recon and nps-tracker.
#
# Config (set by Terraform on the Lambda):
#   COGNITO_USER_POOL_ID  the shared TEG pool (Google Workspace SSO)
#   COGNITO_CLIENT_ID     optional; when set, the token audience must match.
#                         Issuer + signature already pin tokens to the TEG pool.
#
# Fails closed: inside Lambda, a missing pool id is a 503, never an open door.
# Outside Lambda with no pool configured (local dev), auth is skipped.
# ---------------------------------------------------------------------------

COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID")
_IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
_jwks_client: Optional[jwt.PyJWKClient] = None


def _issuer() -> str:
    region = COGNITO_USER_POOL_ID.split("_", 1)[0]
    return f"https://cognito-idp.{region}.amazonaws.com/{COGNITO_USER_POOL_ID}"


def _require_user(authorization: Optional[str] = Header(None)) -> dict:
    global _jwks_client
    if not COGNITO_USER_POOL_ID:
        if _IN_LAMBDA:
            raise HTTPException(status_code=503, detail="Auth is not configured on this deployment")
        return {"email": "local-dev"}
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in required")
    token = authorization[7:].strip()
    try:
        if _jwks_client is None:
            _jwks_client = jwt.PyJWKClient(_issuer() + "/.well-known/jwks.json", cache_keys=True)
        key = _jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], issuer=_issuer(),
            audience=COGNITO_CLIENT_ID, options={"verify_aud": bool(COGNITO_CLIENT_ID), "require": ["exp", "iat"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Sign-in expired or invalid")
    if claims.get("token_use") != "id":
        raise HTTPException(status_code=401, detail="Sign-in expired or invalid")
    return claims


# ---------------------------------------------------------------------------
# Cached payload — read from S3 here, behind auth, instead of the page
# fetching /cache/data-health.json straight through CloudFront (that path is
# outside the auth gate). CACHE_BUCKET is unset in local dev → always live.
# ---------------------------------------------------------------------------

CACHE_BUCKET = os.environ.get("CACHE_BUCKET")
CACHE_KEY = "cache/data-health.json"  # what api/refresh.py writes


def _cached_payload() -> Optional[bytes]:
    if not CACHE_BUCKET:
        return None
    try:
        obj = boto3.client("s3", region_name=os.environ.get("AWS_REGION_NAME", "us-east-2")) \
            .get_object(Bucket=CACHE_BUCKET, Key=CACHE_KEY)
        return obj["Body"].read()
    except Exception as exc:
        print(f"Cache read failed, running live: {exc}")
        return None


# Shared 5-minute result cache, per Lambda container. Every request now
# carries its own id_token, which is part of CloudFront's /api/* cache key, so
# CloudFront no longer shares cached answers between users. This takes that
# role back, and it's safe: it only runs inside routes, after the auth check.
# Mostly matters for a non-default Targets month (a full ~28-query live run)
# and saves the S3 read on the default view. Only successful results are
# cached; None (an S3 miss) is not.
MEMO_TTL = 300
_memo_cache: dict[tuple, tuple[float, object]] = {}
_memo_lock = threading.Lock()


def _memo(key: tuple, fn, *args):
    now = time.monotonic()
    with _memo_lock:
        hit = _memo_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = fn(*args)
    if value is not None:
        with _memo_lock:
            for k in [k for k, (exp, _) in _memo_cache.items() if exp <= now]:
                del _memo_cache[k]
            _memo_cache[key] = (now + MEMO_TTL, value)
    return value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

api = APIRouter(dependencies=[Depends(_require_user)])


def _guard(fn, *args):
    try:
        return fn(*args)
    except HTTPException:
        raise
    except snowflake.connector.errors.DatabaseError as exc:
        raise HTTPException(status_code=503, detail=f"Snowflake connection failed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Query failed: {exc}")


@api.get("/api/data-health/dashboard")
def dashboard_route(target_month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
                    live: bool = Query(False)):
    # The default view is what the refresh Lambda pre-bakes; a non-default
    # Targets month (or ?live=1, or a cache miss) runs the checks live.
    if target_month is None and not live:
        body = _memo(("s3",), _cached_payload)
        if body is not None:
            # Short: CloudFront's /api/* TTL would otherwise hold it for an hour.
            return Response(content=body, media_type="application/json",
                            headers={"Cache-Control": "max-age=300"})
    if live:  # an explicit ?live=1 always runs fresh
        return _guard(get_dashboard, target_month)
    return _guard(_memo, ("dashboard", target_month), get_dashboard, target_month)


@api.get("/api/data-health/checks/{section}")
def section_route(section: str, target_month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$")):
    return _guard(_memo, ("section", section, target_month), get_section, section, target_month)


app.include_router(api)


# ---------------------------------------------------------------------------
# Lambda + dev entrypoints — reusable as-is
# ---------------------------------------------------------------------------

handler = Mangum(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
