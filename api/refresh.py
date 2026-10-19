"""
Cache refresh Lambda — runs on schedule (see infra/refresh.tf).
Pre-bakes whatever portion of this dashboard's data CAN be pre-baked, so
prod page loads don't have to hit Snowflake on every visit.

Triggered by: EventBridge schedule (see infra/refresh.tf for the cron)
Output:       s3://teg-<slug>/cache/<slug>.json
              s3://teg-<slug>/cache/manifest.json

TODO before this is real:
  1. Decide the caching strategy (see CLAUDE.md "Caching strategy") —
     whole-payload cache (single-parameter dashboards) vs. default-view-only
     cache (filter-heavy dashboards).
  2. Replace get_default_payload() below with a call into whatever function
     you wrote in api/main.py that returns the cacheable payload — don't
     duplicate query logic here, reuse the exact function the live API
     route also calls, so the cache and the live endpoint can never drift.
  3. Update BUCKET below to match infra/s3.tf's bucket name
     (teg-${var.project_slug}).
"""

import json
import boto3
from datetime import datetime, timezone

from api.main import get_default_dashboard  # TODO: rename/implement this in main.py

BUCKET = "teg-<slug>"  # TODO: match infra/s3.tf
s3 = boto3.client("s3")


def write(key: str, data):
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, default=str),
        ContentType="application/json",
        CacheControl="max-age=3600",
    )


def handler(event, context):
    print("Starting cache refresh...")
    started = datetime.now(timezone.utc)

    try:
        data = get_default_dashboard()
        write("cache/<slug>.json", data)  # TODO: match the frontend's fetch path
        error = None
    except Exception as e:
        print(f"ERROR: {e}")
        error = str(e)

    manifest = {
        "refreshed_at": started.isoformat(),
        "error": error,
    }
    write("cache/manifest.json", manifest)

    if error:
        raise RuntimeError(error)

    print("Done.")
    return manifest
