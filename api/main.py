"""
TEG <PROJECT NAME> — FastAPI backend
TODO: one-line description of what this dashboard shows.

Endpoints:
  GET /health              → liveness check
  GET /api/<slug>/...      → TODO: list your real endpoints here as you add them

Run locally:  python -m uvicorn api.main:app --reload

See CLAUDE.md before writing any of the query functions below — it covers
the Snowflake credential pattern, the caching-strategy decision, and the
testing checklist this file is meant to support.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import date, timedelta
from typing import Optional

import boto3
import snowflake.connector
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

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
        **_auth_kwargs(creds),
    )
    if creds.get("role"):
        kwargs["role"] = creds["role"]
    return snowflake.connector.connect(**kwargs)


# ---------------------------------------------------------------------------
# App — reusable as-is
# ---------------------------------------------------------------------------

# TODO: rename the title to this dashboard's actual name.
app = FastAPI(title="TEG <PROJECT NAME> API", version="1.0.0")

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


# ---------------------------------------------------------------------------
# TODO: your query functions go here.
#
# Pattern to follow (see CLAUDE.md "Backend" section):
#   - One function per report/table, e.g. get_<thing>(...).
#   - Validate the SQL against live Snowflake via the MCP tools BEFORE
#     wiring it in here — don't trust the build notes' SQL sketches as-is.
#   - If there's a Total/IRL/RA-style type switch, compute every variant
#     once in SQL and pick the requested one in Python — don't branch SQL
#     per type.
#   - Grand-total rows: sum the raw numerator/denominator columns first,
#     THEN derive delta/%, never average per-row percentages.
#
# Example shape (delete once you have real endpoints):
#
# def get_example(some_filter: Optional[list[str]]) -> dict:
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT ... FROM TEG_PROD.DW.<TABLE> WHERE ...", {...})
#         rows = cur.fetchall()
#     finally:
#         conn.close()
#     return {"rows": [...]}
#
#
# ---------------------------------------------------------------------------
# TODO: a combined "default view" payload, used both by a convenience route
# and by api/refresh.py's cache-refresh Lambda. Whether you need this at all
# depends on the caching-strategy decision in CLAUDE.md:
#   - single-year-style dashboard -> this covers the whole payload
#   - filter-heavy dashboard      -> this covers just the default filter
#                                    state; everything else queries live
#
# def get_default_dashboard() -> dict:
#     ...


# ---------------------------------------------------------------------------
# TODO: routes. One per query function above, wrapped in the same
# try/except pattern (Snowflake DatabaseError -> 503, anything else -> 500).
# ---------------------------------------------------------------------------

# @app.get("/api/<slug>/example")
# def example_route(some_filter: Optional[str] = None):
#     try:
#         return get_example(some_filter.split(",") if some_filter else None)
#     except snowflake.connector.errors.DatabaseError as exc:
#         raise HTTPException(status_code=503, detail=f"Snowflake connection failed: {exc}")
#     except Exception as exc:
#         raise HTTPException(status_code=500, detail=f"Query failed: {exc}")


# ---------------------------------------------------------------------------
# Lambda + dev entrypoints — reusable as-is
# ---------------------------------------------------------------------------

handler = Mangum(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
