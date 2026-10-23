variable "project_slug" {
  description = "Short kebab-case project name, e.g. \"same-store-sales\". Drives every resource name via local.name_prefix/bucket_name/etc in main.tf — set this and nothing else needs renaming."
  type        = string
  default     = "data-health" # deploy.sh also passes this explicitly
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

variable "shared_cognito_pool_id" {
  description = "Shared TEG Cognito user pool (Google Workspace SSO) that all dashboards federate through"
  type        = string
  default     = "us-east-2_IKqlKW1Uu"
}

variable "shared_cognito_domain" {
  description = "Hosted-UI domain of the shared TEG Cognito pool"
  type        = string
  default     = "teg-dashboards.auth.us-east-2.amazoncognito.com"
}
