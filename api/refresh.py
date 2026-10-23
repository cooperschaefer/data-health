"""
Cache refresh Lambda — runs on schedule (see infra/refresh.tf).
Pre-bakes the whole Data Health payload so prod page loads read S3 (via the
authenticated /api/data-health/dashboard route, never /cache/* directly)
instead of running ~28 Snowflake queries per visit.

Caching strategy (CLAUDE.md "Caching strategy"): whole-payload cache. The
page's concept and heatmap-window filters are applied client-side to data
that is already network-wide, so the default payload IS every view. The only
live-query path is a non-default Targets month (?tmonth=YYYY-MM on the page).

Triggered by: EventBridge schedule (see infra/refresh.tf for the cron)
Output:       s3://teg-data-health/cache/data-health.json
              s3://teg-data-health/cache/manifest.json
"""

import json
import boto3
from datetime import datetime, timezone

from api.main import get_default_dashboard

BUCKET = "teg-data-health"  # matches infra/s3.tf (teg-${var.project_slug})
s3 = boto3.client("s3")


def write(key: str, data, max_age: int = 300):
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data, default=str, allow_nan=False),
        ContentType="application/json",
        # Short: a health dashboard that serves an hour-old "everything is
        # fine" defeats the point. The API re-serves it with max-age=300 too.
        CacheControl=f"max-age={max_age}",
    )


def handler(event, context):
    print("Starting Data Health cache refresh...")
    started = datetime.now(timezone.utc)

    try:
        data = get_default_dashboard()
        write("cache/data-health.json", data)
        print(f"Cached: {len(data['ATTN'])} findings, {data['META']['store_count']} stores, "
              f"{data['META']['total_seconds']}s")
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
