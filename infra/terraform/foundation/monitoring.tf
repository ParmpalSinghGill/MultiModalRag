resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = toset(["pipeline-tasks", "control-api", "opensearch-ops"])
  name              = "/aws/lambda/${local.prefix}-${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "ecs_worker" {
  name              = "/ecs/${local.prefix}/worker"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "ecs_api" {
  name              = "/ecs/${local.prefix}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# The vendedlogs prefix is the AWS-recommended path for Step Functions delivery.
resource "aws_cloudwatch_log_group" "state_machine" {
  name              = "/aws/vendedlogs/states/${local.state_machine_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "pipe" {
  name              = "/aws/vendedlogs/pipes/${local.prefix}-ingest"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/vendedlogs/apigateway/${local.prefix}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "opensearch" {
  name              = "/aws/opensearch/${local.domain_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

data "aws_iam_policy_document" "opensearch_logs" {
  statement {
    actions   = ["logs:PutLogEvents", "logs:CreateLogStream"]
    resources = ["${aws_cloudwatch_log_group.opensearch.arn}:*"]
    principals {
      type        = "Service"
      identifiers = ["es.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "opensearch" {
  policy_name     = "${local.prefix}-opensearch"
  policy_document = data.aws_iam_policy_document.opensearch_logs.json
}

# ------------------------------- Notifications -----------------------------
resource "aws_sns_topic" "alerts" {
  name              = "${local.prefix}-alerts"
  kms_master_key_id = aws_kms_key.main.id
}

resource "aws_sns_topic_subscription" "alerts" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.prefix}-ingest-dlq-not-empty"
  alarm_description   = "Documents failed ingestion and need review."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "queue_age" {
  alarm_name          = "${local.prefix}-ingest-queue-near-expiry"
  alarm_description   = "Queued messages are 10 days old and expire at 14 days. Start the platform; ragctl start also rediscovers unprocessed S3 objects."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.ingest.name }
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 864000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "workflow_failures" {
  alarm_name          = "${local.prefix}-ingestion-workflow-failures"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.ingest.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_budgets_budget" "monthly" {
  name         = "${local.prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = length(var.alert_emails) > 0 ? [
      { threshold = 80, type = "ACTUAL" },
      { threshold = 100, type = "ACTUAL" },
      { threshold = 100, type = "FORECASTED" },
    ] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = var.alert_emails
    }
  }
}

# -------------------------------- CloudTrail -------------------------------
resource "aws_s3_bucket" "audit" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = "${local.prefix}-audit-${local.account_id}"
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  count                   = var.enable_cloudtrail ? 1 : 0
  bucket                  = aws_s3_bucket.audit[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.audit[0].id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  count  = var.enable_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.audit[0].id
  rule {
    id     = "archive-then-expire"
    status = "Enabled"
    filter {}
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = 365
    }
  }
}

data "aws_iam_policy_document" "audit_bucket" {
  count = var.enable_cloudtrail ? 1 : 0
  statement {
    sid       = "CloudTrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.audit[0].arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:cloudtrail:${var.region}:${local.account_id}:trail/${local.prefix}-audit"]
    }
  }
  statement {
    sid       = "CloudTrailWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit[0].arn}/AWSLogs/${local.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:cloudtrail:${var.region}:${local.account_id}:trail/${local.prefix}-audit"]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit[0].arn, "${aws_s3_bucket.audit[0].arn}/*"]
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
}

resource "aws_s3_bucket_policy" "audit" {
  count      = var.enable_cloudtrail ? 1 : 0
  bucket     = aws_s3_bucket.audit[0].id
  policy     = data.aws_iam_policy_document.audit_bucket[0].json
  depends_on = [aws_s3_bucket_public_access_block.audit]
}

resource "aws_cloudtrail" "main" {
  count                         = var.enable_cloudtrail ? 1 : 0
  name                          = "${local.prefix}-audit"
  s3_bucket_name                = aws_s3_bucket.audit[0].id
  kms_key_id                    = aws_kms_key.main.arn
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true
  depends_on                    = [aws_s3_bucket_policy.audit]
}
