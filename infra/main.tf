terraform {
  backend "s3" {
    bucket = "teg-terraform-states-bucket"
    # Terraform's `backend` block can't interpolate variables (it's evaluated
    # before any variable is resolved), so this literal is set by hand — keep
    # it in sync with project_slug in variables.tf and deploy.sh.
    key    = "data-health.state"
    region = "us-east-2"
  }
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Environment = "production"
      Project     = "datalake"
    }
  }
}

# us-east-1 provider required for CloudFront ACM certs
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = {
      Environment = "production"
      Project     = "datalake"
    }
  }
}

data "aws_caller_identity" "current" {}

# Derived names, shared across every other .tf file — this is the one place
# that turns on the "just set project_slug" workflow. Add derived locals
# here rather than repeating "tf-${var.project_slug}-..." inline everywhere.
locals {
  name_prefix        = "tf-${var.project_slug}"
  bucket_name        = "teg-${var.project_slug}"
  cloudfront_comment = "TEG ${var.project_slug} Dashboard"
}
