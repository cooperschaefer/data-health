resource "aws_lambda_function" "refresh" {
  function_name = "${local.name_prefix}-refresh"
  role          = aws_iam_role.lambda.arn

  filename         = local.lambda_zip
  source_code_hash = filebase64sha256(local.lambda_zip)
  runtime          = "python3.12"
  handler          = "api.refresh.handler"

  # TODO: tune to how expensive get_default_dashboard() actually is —
  # 300s covers a fleet-wide query comfortably, shrink if this dashboard's
  # cacheable payload is small.
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

# TODO: pick a schedule that matches how often the underlying data
# actually changes. 2am CST = 8am UTC is the convention used elsewhere in
# this project family for a once-daily refresh.
resource "aws_cloudwatch_event_rule" "refresh" {
  name                = "${local.name_prefix}-refresh"
  description         = "Refresh the ${var.project_slug} dashboard cache"
  schedule_expression = "cron(0 8 * * ? *)"

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
