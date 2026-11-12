terraform {
  # Pinned for the same reason the base image and ruff are pinned: a minor
  # release of either the CLI or the provider can change behaviour with no
  # commit behind it. ~> 1.16.0 allows 1.16.x only (~> 1.16 would allow 1.17).
  required_version = "~> 1.16.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State is local. For anything with more than one operator, move it to an S3
  # backend with state locking (use_lockfile below, S3's own lock file; no
  # DynamoDB table needed). Two concurrent applies against a local state file
  # will corrupt it.
  #
  # backend "s3" {
  #   bucket       = "..."
  #   key          = "api-gateway/terraform.tfstate"
  #   region       = "eu-north-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}
