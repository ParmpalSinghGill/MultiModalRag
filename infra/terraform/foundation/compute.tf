# All of these resources are free while idle, so they stay in place when the
# platform is stopped. Lambda, Step Functions and the pipe bill only per use.

locals {
  lambda_root = "${path.module}/../../lambda"

  task_subnet_ids  = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
  assign_public_ip = !var.enable_nat_gateway

  worker_environment = [
    { name = "AWS_REGION", value = var.region },
    { name = "DOCUMENTS_BUCKET", value = aws_s3_bucket.documents.bucket },
    { name = "OPENSEARCH_INDEX", value = var.opensearch_index },
  ]
  worker_log_configuration = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.ecs_worker.name
      awslogs-region        = var.region
      awslogs-stream-prefix = "worker"
    }
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.prefix
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

resource "aws_ecs_task_definition" "parse" {
  family                   = "${local.prefix}-parse"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn
  ephemeral_storage {
    size_in_gib = 50
  }
  container_definitions = jsonencode([{
    name             = "worker"
    image            = "${aws_ecr_repository.worker.repository_url}:${var.image_tag}"
    essential        = true
    stopTimeout      = 120
    environment      = local.worker_environment
    logConfiguration = local.worker_log_configuration
  }])
}

resource "aws_ecs_task_definition" "index" {
  family                   = "${local.prefix}-index"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 8192
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn
  ephemeral_storage {
    size_in_gib = 50
  }
  container_definitions = jsonencode([{
    name             = "worker"
    image            = "${aws_ecr_repository.worker.repository_url}:${var.image_tag}"
    essential        = true
    stopTimeout      = 120
    environment      = local.worker_environment
    logConfiguration = local.worker_log_configuration
    # Published by the runtime stack, so this task can only start while it exists.
    secrets = [{ name = "OPENSEARCH_ENDPOINT", valueFrom = local.opensearch_param_arn }]
  }])
}

# --------------------------------- Lambda ---------------------------------
data "archive_file" "pipeline_tasks" {
  type        = "zip"
  source_file = "${local.lambda_root}/pipeline_tasks/handler.py"
  output_path = "${path.module}/.build/pipeline_tasks.zip"
}

data "archive_file" "control_api" {
  type        = "zip"
  source_file = "${local.lambda_root}/control_api/handler.py"
  output_path = "${path.module}/.build/control_api.zip"
}

data "archive_file" "opensearch_ops" {
  type        = "zip"
  output_path = "${path.module}/.build/opensearch_ops.zip"
  source {
    content  = file("${local.lambda_root}/opensearch_ops/handler.py")
    filename = "handler.py"
  }
  source {
    content  = file("${path.module}/../../../data_ingestion/opensearch_schema.py")
    filename = "opensearch_schema.py"
  }
}

resource "aws_lambda_function" "pipeline_tasks" {
  function_name    = "${local.prefix}-pipeline-tasks"
  role             = aws_iam_role.pipeline_lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.pipeline_tasks.output_path
  source_code_hash = data.archive_file.pipeline_tasks.output_base64sha256
  timeout          = 30
  memory_size      = 256
  environment {
    variables = {
      DOCUMENTS_TABLE     = aws_dynamodb_table.documents.name
      CONTROL_TABLE       = aws_dynamodb_table.control.name
      MAX_CONCURRENT_JOBS = tostring(var.max_concurrent_jobs)
      DOCUMENT_SUFFIXES   = jsonencode(var.document_suffixes)
    }
  }
  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function" "control_api" {
  function_name    = "${local.prefix}-control-api"
  role             = aws_iam_role.control_api_lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.control_api.output_path
  source_code_hash = data.archive_file.control_api.output_base64sha256
  timeout          = 10
  memory_size      = 256
  environment {
    variables = {
      DOCUMENTS_BUCKET  = aws_s3_bucket.documents.bucket
      DOCUMENTS_TABLE   = aws_dynamodb_table.documents.name
      CONTROL_TABLE     = aws_dynamodb_table.control.name
      DOCUMENT_SUFFIXES = jsonencode(var.document_suffixes)
    }
  }
  depends_on = [aws_cloudwatch_log_group.lambda]
}

resource "aws_lambda_function" "opensearch_ops" {
  function_name    = "${local.prefix}-opensearch-ops"
  role             = aws_iam_role.ops_lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.opensearch_ops.output_path
  source_code_hash = data.archive_file.opensearch_ops.output_base64sha256
  timeout          = 60
  memory_size      = 256
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.this["ops"].id]
  }
  environment {
    variables = {
      SNAPSHOT_REPOSITORY = local.snapshot_repository
      BACKUP_BUCKET       = aws_s3_bucket.backups.bucket
      SNAPSHOT_BASE_PATH  = local.snapshot_base_path
      SNAPSHOT_ROLE_ARN   = aws_iam_role.opensearch_snapshot.arn
      OPENSEARCH_INDEX    = var.opensearch_index
    }
  }
  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.ops_lambda_vpc]
}

# ------------------------- Step Functions and Pipe -------------------------
resource "aws_sfn_state_machine" "ingest" {
  name     = local.state_machine_name
  type     = "STANDARD"
  role_arn = aws_iam_role.state_machine.arn
  definition = templatefile("${path.module}/ingestion.asl.json.tftpl", {
    pipeline_function = aws_lambda_function.pipeline_tasks.arn
    cluster           = aws_ecs_cluster.main.arn
    parse_task        = aws_ecs_task_definition.parse.family
    index_task        = aws_ecs_task_definition.index.family
    capacity_provider = var.worker_use_spot ? "FARGATE_SPOT" : "FARGATE"
    subnets           = jsonencode(local.task_subnet_ids)
    security_groups   = jsonencode([aws_security_group.this["worker"].id])
    assign_public_ip  = local.assign_public_ip ? "ENABLED" : "DISABLED"
    queue_url         = aws_sqs_queue.ingest.url
    dlq_url           = aws_sqs_queue.dlq.url
  })
  logging_configuration {
    include_execution_data = false
    level                  = "ERROR"
    log_destination        = "${aws_cloudwatch_log_group.state_machine.arn}:*"
  }
  depends_on = [aws_iam_role_policy.state_machine]
}

# Terraform owns the pipe's existence and wiring. Its running state is an
# operational switch owned by ragctl, so Terraform ignores desired_state.
resource "aws_pipes_pipe" "ingest" {
  name          = "${local.prefix}-ingest"
  role_arn      = aws_iam_role.pipe.arn
  source        = aws_sqs_queue.ingest.arn
  target        = aws_sfn_state_machine.ingest.arn
  desired_state = "STOPPED"

  source_parameters {
    sqs_queue_parameters {
      batch_size = 1
    }
  }
  target_parameters {
    step_function_state_machine_parameters {
      invocation_type = "FIRE_AND_FORGET"
    }
  }
  log_configuration {
    level = "ERROR"
    cloudwatch_logs_log_destination {
      log_group_arn = aws_cloudwatch_log_group.pipe.arn
    }
  }
  lifecycle {
    ignore_changes = [desired_state]
  }
  depends_on = [aws_iam_role_policy.pipe]
}

# ------------------------------- API Gateway -------------------------------
resource "aws_apigatewayv2_api" "main" {
  name          = local.prefix
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 50
    throttling_rate_limit  = 25
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId = "$context.requestId", time = "$context.requestTime", route = "$context.routeKey",
      status    = "$context.status", latency = "$context.responseLatency", caller = "$context.identity.caller"
    })
  }
}

resource "aws_apigatewayv2_integration" "control" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.control_api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "control" {
  for_each           = toset(["POST /documents", "GET /jobs/{doc_id}", "GET /status", "$default"])
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.control.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "control_api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*"
}
