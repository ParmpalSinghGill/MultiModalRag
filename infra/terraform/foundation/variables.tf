variable "project" {
  type    = string
  default = "mfrag"
}

variable "environment" {
  type    = string
  default = "prod"
  validation {
    condition     = length("${var.project}-${var.environment}-search") <= 28 && can(regex("^[a-z][a-z0-9-]*$", "${var.project}-${var.environment}"))
    error_message = "project-environment must be lowercase and short enough for an OpenSearch domain name (28 characters including -search)."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "state_bucket" {
  description = "Terraform state bucket from the bootstrap stack."
  type        = string
}

variable "image_tag" {
  description = "Immutable ECR image tag used by the API and worker tasks."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "enable_nat_gateway" {
  description = "true: tasks run in private subnets behind a runtime NAT Gateway. false: cheaper, tasks use public IPs in public subnets with no inbound access."
  type        = bool
  default     = true
}

variable "enable_redis" {
  description = "Deploy ElastiCache Redis in the runtime stack."
  type        = bool
  default     = false
}

variable "worker_use_spot" {
  description = "Run ingestion tasks on Fargate Spot. Interrupted tasks are retried safely."
  type        = bool
  default     = true
}

variable "max_concurrent_jobs" {
  type    = number
  default = 2
}

variable "document_suffixes" {
  description = "Lowercase file suffixes under raw/ that start ingestion."
  type        = list(string)
  default     = [".pdf", ".docx", ".pptx", ".xlsx", ".html", ".png", ".jpg", ".jpeg", ".tiff"]
}

variable "opensearch_index" {
  type    = string
  default = "multimodal_rag_finance"
}

variable "bedrock_model_ids" {
  description = "Models from data_generation/bedrock_generator.py the API may invoke."
  type        = list(string)
  default = [
    "meta.llama3-8b-instruct-v1:0",
    "amazon.nova-micro-v1:0",
    "amazon.nova-lite-v1:0",
    "mistral.mistral-7b-instruct-v0:2",
  ]
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "budget_limit_usd" {
  type    = number
  default = 100
}

variable "alert_emails" {
  description = "Recipients for AWS Budgets and CloudWatch alarm notifications."
  type        = list(string)
  default     = []
}

variable "enable_cloudtrail" {
  description = "Create a management-event trail. Disable if the account already has one, because a second copy is billed."
  type        = bool
  default     = true
}

variable "data_admin_principal_arns" {
  description = "Only these IAM principals may delete protected S3 objects. Empty means nobody can without changing the bucket policy."
  type        = list(string)
  default     = []
}

variable "controller_trusted_principal_arns" {
  description = "IAM principals allowed to assume the ragctl controller role."
  type        = list(string)
  default     = []
}

variable "create_opensearch_service_linked_role" {
  description = "Set true only if AWSServiceRoleForAmazonOpenSearchService does not exist in the account."
  type        = bool
  default     = false
}
