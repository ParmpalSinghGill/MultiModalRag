# Creates the remote-state bucket. This small stack keeps its own state locally.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "project" {
  type    = string
  default = "mfrag"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.project, Environment = var.environment, ManagedBy = "terraform", Stack = "bootstrap" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  bucket = "${var.project}-${var.environment}-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket" "state" {
  bucket = local.bucket
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "backend_config" {
  description = "Save as infra/terraform/backend.hcl"
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.state.bucket}"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
