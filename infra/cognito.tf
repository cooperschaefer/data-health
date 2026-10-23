# Login federates through the shared TEG Cognito pool (Google Workspace SSO)
# rather than owning a pool — no per-project usernames/passwords. We only
# create an app client on that shared pool; the pool, hosted-UI domain, and
# Google identity provider are managed centrally and shared across all TEG
# dashboards (same setup as nps-tracker and the template).
#
# Any TEG Google account can sign in. The API verifies that sign-in itself on
# every /api/* request (api/main.py "Auth"), since CloudFront lets /api/* through.
resource "aws_cognito_user_pool_client" "main" {
  name         = "${local.name_prefix}-client"
  user_pool_id = var.shared_cognito_pool_id

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["Google"]

  callback_urls = ["https://${aws_cloudfront_distribution.dashboard.domain_name}"]
  logout_urls   = ["https://${aws_cloudfront_distribution.dashboard.domain_name}"]

  depends_on = [aws_cloudfront_distribution.dashboard]

  generate_secret = false
}
