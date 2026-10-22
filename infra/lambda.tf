locals {
  lambda_zip = "${path.module}/../lambda.zip"
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name_prefix}-api"
  role          = aws_iam_role.lambda.arn

  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  runtime          = "python3.12"
  handler          = "api.main.handler"

  # Live /api/data-health/dashboard runs the same ~28 checks as the refresh
  # (only hit for a non-default ?tmonth= or a cache miss). Matches the /api
  # origin's origin_read_timeout in cloudfront.tf — CloudFront's max without
  # a quota increase.
  timeout     = 60
  memory_size = 512

  environment {
    variables = {
      SNOWFLAKE_SECRET_ARN = data.aws_secretsmanager_secret.snowflake.arn
      AWS_REGION_NAME      = var.aws_region
    }
  }
}

# Auth is enforced entirely at the CloudFront layer (see cloudfront.tf's
# CloudFront Function) — this Function URL is intentionally open.
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE"
}
