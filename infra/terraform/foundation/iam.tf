locals {
  documents_arn = aws_s3_bucket.documents.arn
  backups_arn   = aws_s3_bucket.backups.arn
  kms_arn       = aws_kms_key.main.arn
  log_arn_base  = "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group"
}

data "aws_iam_policy_document" "assume" {
  for_each = {
    ecs        = "ecs-tasks.amazonaws.com"
    lambda     = "lambda.amazonaws.com"
    states     = "states.amazonaws.com"
    pipes      = "pipes.amazonaws.com"
    opensearch = "es.amazonaws.com"
  }
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = [each.value]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# --------------------------------- ECS ------------------------------------
resource "aws_iam_role" "ecs_execution" {
  name               = "${local.prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.assume["ecs"].json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution" {
  statement {
    actions   = ["ssm:GetParameters"]
    resources = [local.opensearch_param_arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.redis_auth.arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [local.kms_arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution" {
  name   = "runtime-configuration"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution.json
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.prefix}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.assume["ecs"].json
}

data "aws_iam_policy_document" "worker_task" {
  statement {
    sid       = "ReadSources"
    actions   = ["s3:GetObject"]
    resources = ["${local.documents_arn}/raw/*", "${local.documents_arn}/processed/*", "${local.documents_arn}/figures/*"]
  }
  statement {
    sid       = "WriteOutputs"
    actions   = ["s3:PutObject"]
    resources = ["${local.documents_arn}/processed/*", "${local.documents_arn}/figures/*"]
  }
  statement {
    # HeadObject returns 404 instead of 403 for a missing key only with this
    # permission. A prefix condition would not apply because HeadObject has no prefix.
    sid       = "DetectMissingObjects"
    actions   = ["s3:ListBucket"]
    resources = [local.documents_arn]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.kms_arn]
  }
  statement {
    sid       = "IndexChunks"
    actions   = ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost", "es:ESHttpPut"]
    resources = ["${local.domain_arn}/*"]
  }
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "ingestion"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task.json
}

resource "aws_iam_role" "api_task" {
  name               = "${local.prefix}-api-task"
  assume_role_policy = data.aws_iam_policy_document.assume["ecs"].json
}

data "aws_iam_policy_document" "api_task" {
  statement {
    sid     = "InvokeApprovedModels"
    actions = ["bedrock:InvokeModel"]
    resources = concat(
      [for id in var.bedrock_model_ids : "arn:${local.partition}:bedrock:*::foundation-model/${id}"],
      ["arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/*"],
    )
  }
  statement {
    sid       = "SearchIndex"
    actions   = ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost"]
    resources = ["${local.domain_arn}/${var.opensearch_index}", "${local.domain_arn}/${var.opensearch_index}/*", "${local.domain_arn}/"]
  }
  statement {
    sid       = "SignFigureLinks"
    actions   = ["s3:GetObject"]
    resources = ["${local.documents_arn}/figures/*"]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [local.kms_arn]
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "query"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

# -------------------------------- Lambda ----------------------------------
resource "aws_iam_role" "pipeline_lambda" {
  name               = "${local.prefix}-pipeline-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume["lambda"].json
}

data "aws_iam_policy_document" "pipeline_lambda" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.documents.arn]
  }
  statement {
    sid       = "ReadIngestionGate"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.control.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["INGESTION"]
    }
  }
  statement {
    actions   = ["states:DescribeExecution"]
    resources = [local.execution_arn_glob]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.kms_arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["pipeline-tasks"].arn}:*"]
  }
}

resource "aws_iam_role_policy" "pipeline_lambda" {
  name   = "pipeline"
  role   = aws_iam_role.pipeline_lambda.id
  policy = data.aws_iam_policy_document.pipeline_lambda.json
}

resource "aws_iam_role" "control_api_lambda" {
  name               = "${local.prefix}-control-api-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume["lambda"].json
}

data "aws_iam_policy_document" "control_api_lambda" {
  statement {
    sid       = "PresignRawUploads"
    actions   = ["s3:PutObject"]
    resources = ["${local.documents_arn}/raw/*"]
  }
  statement {
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.documents.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.control.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["INGESTION", "CONTROLLER"]
    }
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.kms_arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda["control-api"].arn}:*"]
  }
}

resource "aws_iam_role_policy" "control_api_lambda" {
  name   = "control-api"
  role   = aws_iam_role.control_api_lambda.id
  policy = data.aws_iam_policy_document.control_api_lambda.json
}

resource "aws_iam_role" "ops_lambda" {
  name               = "${local.prefix}-opensearch-ops"
  assume_role_policy = data.aws_iam_policy_document.assume["lambda"].json
}

resource "aws_iam_role_policy_attachment" "ops_lambda_vpc" {
  role       = aws_iam_role.ops_lambda.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "ops_lambda" {
  statement {
    actions   = ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpDelete"]
    resources = ["${local.domain_arn}/*"]
  }
  statement {
    sid       = "RegisterSnapshotRepository"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.opensearch_snapshot.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["es.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ops_lambda" {
  name   = "opensearch-operations"
  role   = aws_iam_role.ops_lambda.id
  policy = data.aws_iam_policy_document.ops_lambda.json
}

# ------------------------------ OpenSearch --------------------------------
resource "aws_iam_role" "opensearch_snapshot" {
  name               = "${local.prefix}-opensearch-snapshot"
  assume_role_policy = data.aws_iam_policy_document.assume["opensearch"].json
}

data "aws_iam_policy_document" "opensearch_snapshot" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [local.backups_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.snapshot_base_path}", "${local.snapshot_base_path}/*"]
    }
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.backups_arn}/${local.snapshot_base_path}/*"]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [local.kms_arn]
  }
}

resource "aws_iam_role_policy" "opensearch_snapshot" {
  name   = "snapshot-repository"
  role   = aws_iam_role.opensearch_snapshot.id
  policy = data.aws_iam_policy_document.opensearch_snapshot.json
}

resource "aws_iam_service_linked_role" "opensearch" {
  count            = var.create_opensearch_service_linked_role ? 1 : 0
  aws_service_name = "opensearchservice.amazonaws.com"
}

# ------------------------- Step Functions and Pipes ------------------------
resource "aws_iam_role" "state_machine" {
  name               = "${local.prefix}-state-machine"
  assume_role_policy = data.aws_iam_policy_document.assume["states"].json
}

data "aws_iam_policy_document" "state_machine" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.pipeline_tasks.arn]
  }
  statement {
    actions = ["ecs:RunTask"]
    resources = [
      "arn:${local.partition}:ecs:${var.region}:${local.account_id}:task-definition/${local.prefix}-parse:*",
      "arn:${local.partition}:ecs:${var.region}:${local.account_id}:task-definition/${local.prefix}-index:*",
      "arn:${local.partition}:ecs:${var.region}:${local.account_id}:task-definition/${local.prefix}-parse",
      "arn:${local.partition}:ecs:${var.region}:${local.account_id}:task-definition/${local.prefix}-index",
    ]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    actions   = ["ecs:StopTask", "ecs:DescribeTasks"]
    resources = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:task/${aws_ecs_cluster.main.name}/*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_execution.arn, aws_iam_role.worker_task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid       = "EcsSyncIntegration"
    actions   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = ["arn:${local.partition}:events:${var.region}:${local.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.ingest.arn, aws_sqs_queue.dlq.arn]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.kms_arn]
  }
  statement {
    # These log-delivery APIs do not support resource-level permissions.
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "state_machine" {
  name   = "orchestration"
  role   = aws_iam_role.state_machine.id
  policy = data.aws_iam_policy_document.state_machine.json
}

resource "aws_iam_role" "pipe" {
  name               = "${local.prefix}-pipe"
  assume_role_policy = data.aws_iam_policy_document.assume["pipes"].json
}

data "aws_iam_policy_document" "pipe" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.ingest.arn]
  }
  statement {
    actions   = ["states:StartExecution"]
    resources = [local.state_machine_arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [local.kms_arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.pipe.arn}:*"]
  }
}

resource "aws_iam_role_policy" "pipe" {
  name   = "ingest-pipe"
  role   = aws_iam_role.pipe.id
  policy = data.aws_iam_policy_document.pipe.json
}

# --------------------------- API consumer access ---------------------------
resource "aws_iam_policy" "api_consumer" {
  name        = "${local.prefix}-api-consumer"
  description = "Attach to principals allowed to call the platform API with SigV4."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "execute-api:Invoke"
      Resource = "${aws_apigatewayv2_api.main.execution_arn}/*"
    }]
  })
}
