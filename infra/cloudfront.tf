resource "aws_cloudfront_distribution" "dashboard" {
  enabled             = true
  default_root_object = "dashboard.html"
  comment             = local.cloudfront_comment

  # S3 origin for static dashboard HTML and pre-baked cache JSON
  origin {
    domain_name              = aws_s3_bucket.dashboard.bucket_regional_domain_name
    origin_id                = "s3-dashboard"
    origin_access_control_id = aws_cloudfront_origin_access_control.dashboard.id
  }

  # Lambda origin for live API
  origin {
    domain_name = trimsuffix(replace(aws_lambda_function_url.api.function_url, "https://", ""), "/")
    origin_id   = "lambda-api"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # Default behavior: serve dashboard.html from S3, enforce Cognito auth
  default_cache_behavior {
    target_origin_id       = "s3-dashboard"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 300
    max_ttl     = 3600

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.auth.arn
    }
  }

  # /api/* behavior: proxy to Lambda. TTL here is a tradeoff — bump it if
  # this dashboard is filter-heavy (many distinct querystrings = mostly
  # cache misses anyway) or drop it if the API is cheap and freshness
  # matters more than Lambda invocation count.
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "lambda-api"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = true
      headers      = ["Authorization"]
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 3600
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# Key-value store holds the Cognito client id so the auth function can read it
# at runtime instead of embedding it. This breaks what would otherwise be a
# Terraform dependency cycle: distribution -> function -> client -> distribution.
# The KVS depends only on the cognito client (never on the distribution), so the
# graph stays acyclic while everything remains dynamic (no hardcoded ids).
resource "aws_cloudfront_key_value_store" "auth" {
  name    = "${local.name_prefix}-auth-kvs"
  comment = "Cognito client id for the auth CloudFront function"
}

resource "aws_cloudfrontkeyvaluestore_key" "client_id" {
  key_value_store_arn = aws_cloudfront_key_value_store.auth.arn
  key                  = "client_id"
  value                = aws_cognito_user_pool_client.main.id
}

# CloudFront function: redirect unauthenticated users to Cognito hosted login.
#
# Two runtime lookups avoid referencing the distribution and so avoid a cycle:
#   - client_id      ← read from the key-value store (above)
#   - redirect_uri   ← derived from the incoming request's Host header, which is
#                       the CloudFront domain serving this request
resource "aws_cloudfront_function" "auth" {
  name        = "${local.name_prefix}-auth"
  runtime     = "cloudfront-js-2.0"
  comment     = "Redirect unauthenticated users to Cognito hosted login"
  publish     = true
  key_value_store_associations = [aws_cloudfront_key_value_store.auth.arn]

  code = <<-EOF
    import cf from 'cloudfront';

    const COGNITO_URL = 'https://${local.cognito_domain_prefix}.auth.${var.aws_region}.amazoncognito.com';
    const kvs = cf.kvs();

    async function handler(event) {
      const request = event.request;
      const cookies  = request.cookies || {};

      // Allow /api/*, /health, and /cache/* through unauthenticated — the
      // page's own JS calls these directly via fetch, which can't follow a
      // 302-to-login redirect the way a top-level navigation can. Forgetting
      // /cache/* here breaks the pre-baked-cache fetch on first paint.
      if (request.uri.startsWith('/api/') || request.uri === '/health' || request.uri.startsWith('/cache/')) {
        return request;
      }

      // Allow the Cognito callback (?code=...) through so the page can load
      // and exchange the code for tokens. Without this the function would
      // bounce the callback back to /login before any JS runs — an endless
      // loop (the cookie can only be set by JS that never gets to execute).
      const qs = request.querystring || {};
      if (qs.code) {
        return request;
      }

      // Check for id_token cookie set after Cognito redirect
      if (!cookies['id_token']) {
        const clientId = await kvs.get('client_id');
        const host     = request.headers.host.value;
        return {
          statusCode: 302,
          statusDescription: 'Found',
          headers: {
            location: {
              value: COGNITO_URL + '/login?client_id=' + clientId +
                     '&response_type=code&scope=openid+email+profile' +
                     '&redirect_uri=https://' + host
            }
          }
        };
      }

      return request;
    }
  EOF
}
