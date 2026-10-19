output "dashboard_url" {
  description = "Public URL for the dashboard"
  value       = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
}

output "api_url" {
  description = "Lambda function URL for the API"
  value       = aws_lambda_function_url.api.function_url
}

output "cognito_login_url" {
  description = "Cognito hosted login UI"
  value       = "https://${local.cognito_domain_prefix}.auth.${var.aws_region}.amazoncognito.com/login?client_id=${aws_cognito_user_pool_client.main.id}&response_type=code&scope=openid+email+profile&redirect_uri=https://${aws_cloudfront_distribution.dashboard.domain_name}"
}

output "cognito_client_id" {
  description = "Cognito app client id — copy into COGNITO_CLIENT at the top of build_dashboard.py after the first apply (see the two-pass deploy note in CLAUDE.md)"
  value       = aws_cognito_user_pool_client.main.id
}
