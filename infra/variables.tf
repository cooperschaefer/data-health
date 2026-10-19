variable "project_slug" {
  description = "Short kebab-case project name, e.g. \"same-store-sales\". Drives every resource name via local.name_prefix/bucket_name/etc in main.tf — set this and nothing else needs renaming."
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-2"
}

variable "snowflake_secret_name" {
  description = "Shared Secrets Manager secret holding the tableau_svc Snowflake key-pair credentials"
  type        = string
  default     = "tf-teg-production-snowflake-tableau_svc-creds"
}

variable "cognito_domain_prefix" {
  description = "Unique prefix for the Cognito hosted UI domain. Cognito domains are globally unique across ALL AWS accounts, not just yours — leave empty to default to \"teg-${project_slug}\", and only override if that collides."
  type        = string
  default     = ""
}
