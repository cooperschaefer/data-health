# Shared TEG service-account credentials (tableau_svc / prod_ro, RSA key-pair).
# Referenced, not managed: the secret is owned centrally and rotated there, so
# this repo keeps no Snowflake credential of its own.
data "aws_secretsmanager_secret" "snowflake" {
  name = var.snowflake_secret_name
}
