resource "aws_lambda_function" "refresh" {
  function_name = "${local.name_prefix}-refresh"
  role          = aws_iam_role.lambda.arn

  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  runtime          = "python3.12"
  handler          = "api.refresh.handler"

  # get_default_dashboard() runs ~28 checks, 8 at a time, plus a possible
  # warehouse resume. Lambda bills actual runtime, so the headroom is free;
  # META.total_seconds in the refresh logs shows the real number.
  timeout     = 300
  memory_size = 512

  tags = {
    Environment = "production"
    Project     = "datalake"
  }

  environment {
    variables = {
      SNOWFLAKE_SECRET_ARN = data.aws_secretsmanager_secret.snowflake.arn
      AWS_REGION_NAME      = var.aws_region
    }
  }
}

# Grant refresh Lambda write access to the dashboard S3 bucket
resource "aws_iam_role_policy" "lambda_s3_cache" {
  name = "${local.name_prefix}-lambda-s3-cache"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.dashboard.arn}/cache/*"
      }
    ]
  })
}

# Main refresh: ~1.5h after each dbt production run starts, so the cache is
# rebuilt after the run finishes (runs average ~1h15m; the extra ~15m is
# buffer for slow runs).
#
#   dbt run start (UTC):  10:05  15:05  18:05  21:05  00:05  03:05
#   cache refresh (UTC):  11:35  16:35  19:35  22:35  01:35  04:35
#
# dbt's schedule is fixed in UTC, and EventBridge rule cron expressions are
# always evaluated in UTC, so this offset holds year-round with no DST
# handling. Don't convert these to Central time — that would drift by an
# hour against dbt twice a year. If dbt's schedule changes, update both rows
# above and the hour list below.
resource "aws_cloudwatch_event_rule" "refresh" {
  name                = "${local.name_prefix}-refresh"
  description         = "Refresh the ${var.project_slug} dashboard cache ~1.5h after each dbt production run (UTC)"
  schedule_expression = "cron(35 1,4,11,16,19,22 * * ? *)"

  tags = {
    Environment = "production"
    Project     = "datalake"
  }
}

resource "aws_cloudwatch_event_target" "refresh" {
  rule      = aws_cloudwatch_event_rule.refresh.name
  target_id = "refresh-lambda"
  arn       = aws_lambda_function.refresh.arn
}

resource "aws_lambda_permission" "refresh_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.refresh.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.refresh.arn
}
