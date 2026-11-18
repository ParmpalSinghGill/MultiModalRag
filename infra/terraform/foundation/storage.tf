# Persistent data. prevent_destroy and the delete-deny bucket policies keep it
# safe from both Terraform mistakes and application or controller credentials.

locals {
  buckets = {
    documents = "${local.prefix}-documents-${local.account_id}"
    backups   = "${local.prefix}-backups-${local.account_id}"
  }
  delete_actions = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
}

resource "aws_s3_bucket" "documents" {
  bucket = local.buckets.documents
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket" "backups" {
  bucket = local.buckets.backups
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket     = aws_s3_bucket.documents.id
  depends_on = [aws_s3_bucket_versioning.documents]
  rule {
    id     = "cleanup-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket     = aws_s3_bucket.backups.id
  depends_on = [aws_s3_bucket_versioning.backups]
  rule {
    id     = "cleanup-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "documents_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid     = "ProtectDocumentData"
    effect  = "Deny"
    actions = local.delete_actions
    resources = [
      "${aws_s3_bucket.documents.arn}/raw/*",
      "${aws_s3_bucket.documents.arn}/processed/*",
      "${aws_s3_bucket.documents.arn}/figures/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    dynamic "condition" {
      for_each = length(var.data_admin_principal_arns) > 0 ? [1] : []
      content {
        test     = "ArnNotLike"
        variable = "aws:PrincipalArn"
        values   = var.data_admin_principal_arns
      }
    }
  }
}

data "aws_iam_policy_document" "backups_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "ProtectInfrastructureState"
    effect    = "Deny"
    actions   = local.delete_actions
    resources = ["${aws_s3_bucket.backups.arn}/infrastructure-state/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    dynamic "condition" {
      for_each = length(var.data_admin_principal_arns) > 0 ? [1] : []
      content {
        test     = "ArnNotLike"
        variable = "aws:PrincipalArn"
        values   = var.data_admin_principal_arns
      }
    }
  }

  # OpenSearch manages its own repository files, so only its role may delete here.
  statement {
    sid       = "ProtectSnapshots"
    effect    = "Deny"
    actions   = local.delete_actions
    resources = ["${aws_s3_bucket.backups.arn}/opensearch-snapshots/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotLike"
      variable = "aws:PrincipalArn"
      values   = concat([aws_iam_role.opensearch_snapshot.arn], var.data_admin_principal_arns)
    }
  }
}

resource "aws_s3_bucket_policy" "documents" {
  bucket     = aws_s3_bucket.documents.id
  policy     = data.aws_iam_policy_document.documents_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.documents]
}

resource "aws_s3_bucket_policy" "backups" {
  bucket     = aws_s3_bucket.backups.id
  policy     = data.aws_iam_policy_document.backups_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.backups]
}

resource "aws_s3_bucket_notification" "documents" {
  bucket = aws_s3_bucket.documents.id

  dynamic "queue" {
    for_each = toset(var.document_suffixes)
    content {
      id            = "ingest${replace(queue.value, ".", "-")}"
      queue_arn     = aws_sqs_queue.ingest.arn
      events        = ["s3:ObjectCreated:*"]
      filter_prefix = "raw/"
      filter_suffix = queue.value
    }
  }

  depends_on = [aws_sqs_queue_policy.ingest]
}

# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "documents" {
  name                        = "${local.prefix}-documents"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "control" {
  name                        = "${local.prefix}-control"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }
  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# SQS
# ---------------------------------------------------------------------------
resource "aws_sqs_queue" "dlq" {
  name                      = "${local.prefix}-ingest-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.main.id
}

resource "aws_sqs_queue" "ingest" {
  name                       = "${local.prefix}-ingest"
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 300
  receive_wait_time_seconds  = 20
  kms_master_key_id          = aws_kms_key.main.id
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

data "aws_iam_policy_document" "ingest_queue" {
  statement {
    sid       = "AllowDocumentBucketEvents"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingest.arn]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.documents.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "ingest" {
  queue_url = aws_sqs_queue.ingest.id
  policy    = data.aws_iam_policy_document.ingest_queue.json
}

# ---------------------------------------------------------------------------
# Container images and application secrets
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "api" {
  name                 = "${local.prefix}/api"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_repository" "worker" {
  name                 = "${local.prefix}/worker"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy     = local.ecr_lifecycle
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name
  policy     = local.ecr_lifecycle
}

locals {
  ecr_lifecycle = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the ten most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

resource "random_password" "redis_auth" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "redis_auth" {
  name                    = "${local.prefix}/redis-auth-token"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id     = aws_secretsmanager_secret.redis_auth.id
  secret_string = random_password.redis_auth.result
}
