data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  prefix     = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  domain_name = "${local.prefix}-search"
  domain_arn  = "arn:${local.partition}:es:${var.region}:${local.account_id}:domain/${local.domain_name}"

  state_machine_name = "${local.prefix}-ingestion"
  state_machine_arn  = "arn:${local.partition}:states:${var.region}:${local.account_id}:stateMachine:${local.state_machine_name}"
  execution_arn_glob = "arn:${local.partition}:states:${var.region}:${local.account_id}:execution:${local.state_machine_name}:*"

  runtime_parameter_path = "/${local.prefix}/runtime"
  opensearch_parameter   = "${local.runtime_parameter_path}/opensearch_endpoint"
  opensearch_param_arn   = "arn:${local.partition}:ssm:${var.region}:${local.account_id}:parameter${local.opensearch_parameter}"

  snapshot_repository = "${replace(local.prefix, "-", "_")}_backups"
  snapshot_base_path  = "opensearch-snapshots/${local.prefix}"
  manifest_prefix     = "infrastructure-state/${local.prefix}"

  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# ---------------------------------------------------------------------------
# Published for the controller, so status and stop work without reading state.
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "config" {
  name = "/${local.prefix}/foundation/config"
  type = "String"
  tier = "Intelligent-Tiering"
  value = jsonencode({
    prefix              = local.prefix
    region              = var.region
    state_bucket        = var.state_bucket
    image_tag           = var.image_tag
    enable_redis        = var.enable_redis
    documents_bucket    = aws_s3_bucket.documents.bucket
    backup_bucket       = aws_s3_bucket.backups.bucket
    documents_table     = aws_dynamodb_table.documents.name
    control_table       = aws_dynamodb_table.control.name
    queue_url           = aws_sqs_queue.ingest.url
    dlq_url             = aws_sqs_queue.dlq.url
    pipe_name           = aws_pipes_pipe.ingest.name
    state_machine_arn   = aws_sfn_state_machine.ingest.arn
    ecs_cluster         = aws_ecs_cluster.main.name
    worker_families     = [aws_ecs_task_definition.parse.family, aws_ecs_task_definition.index.family]
    api_service         = "${local.prefix}-api"
    ops_function        = aws_lambda_function.opensearch_ops.function_name
    domain_name         = local.domain_name
    snapshot_repository = local.snapshot_repository
    snapshot_base_path  = local.snapshot_base_path
    manifest_prefix     = local.manifest_prefix
    opensearch_index    = var.opensearch_index
    document_suffixes   = var.document_suffixes
    ecr_repositories    = { api = aws_ecr_repository.api.name, worker = aws_ecr_repository.worker.name }
    api_endpoint        = aws_apigatewayv2_api.main.api_endpoint
  })
}
