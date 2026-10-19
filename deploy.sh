#!/usr/bin/env bash
# Deploy this dashboard to AWS
# Usage: ./deploy.sh
#
# Needs no Snowflake credentials: the Lambda reads the shared tableau_svc
# service-account secret at runtime (see infra/secrets.tf).
set -euo pipefail

# TODO: set this once per project — everything else derives from it.
# Must match infra/variables.tf's project_slug and infra/main.tf's
# backend "s3" { key = "<slug>.state" } (that one line can't be
# parameterized — see the TODO in infra/main.tf).
PROJECT_SLUG="<slug>"

ROOT="$(cd "$(dirname "$0")" && pwd)"
INFRA="$ROOT/infra"
ZIP="$ROOT/lambda.zip"
SITE_BUCKET="teg-${PROJECT_SLUG}"
CLOUDFRONT_COMMENT="TEG ${PROJECT_SLUG} Dashboard"

echo "==> Building dashboard.html..."
python "$ROOT/build_dashboard.py"

echo "==> Building Lambda zip..."
rm -f "$ZIP"

PKGDIR="$ROOT/.lambda_pkg"
rm -rf "$PKGDIR"
pip install \
  --quiet \
  --target "$PKGDIR" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r "$ROOT/requirements.txt"

cp -r "$ROOT/api" "$PKGDIR/api"

cd "$PKGDIR"
zip -r "$ZIP" . -x "*.pyc" -x "*/__pycache__/*" > /dev/null
cd "$ROOT"
rm -rf "$PKGDIR"
echo "    lambda.zip: $(du -sh "$ZIP" | cut -f1)"

echo "==> Running Terraform..."
cd "$INFRA"
terraform init -input=false
terraform apply -input=false -auto-approve \
  -var="project_slug=${PROJECT_SLUG}"

echo "==> Uploading dashboard.html to S3..."
aws s3 cp "$ROOT/dashboard.html" "s3://$SITE_BUCKET/dashboard.html" \
  --content-type "text/html" \
  --cache-control "max-age=300"

echo "==> Invoking refresh Lambda (seed the initial cache)..."
aws lambda invoke --function-name "tf-${PROJECT_SLUG}-refresh" /dev/null > /dev/null

echo "==> Invalidating CloudFront cache..."
DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${CLOUDFRONT_COMMENT}'].Id" \
  --output text)
aws cloudfront create-invalidation \
  --distribution-id "$DIST_ID" \
  --paths "/*" > /dev/null

echo ""
echo "Done!"
cd "$INFRA" && terraform output dashboard_url

echo ""
echo "NOTE: if this was the first deploy, copy the cognito_client_id output"
echo "above into COGNITO_CLIENT at the top of build_dashboard.py, then rerun"
echo "./deploy.sh so dashboard.html is rebuilt with the real client id."
