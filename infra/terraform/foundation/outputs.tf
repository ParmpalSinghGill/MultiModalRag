output "prefix" {
  value = local.prefix
}

output "region" {
  value = var.region
}

output "image_tag" {
  value = var.image_tag
}

output "enable_redis" {
  value = var.enable_redis
}

output "enable_nat_gateway" {
  value = var.enable_nat_gateway
}

output "opensearch_index" {
  value = var.opensearch_index
}

output "domain_name" {
  value = local.domain_name
}

output "opensearch_parameter" {
  value = local.opensearch_parameter
}

output "opensearch_log_group_arn" {
  value = aws_cloudwatch_log_group.opensearch.arn
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "documents_bucket" {
  value = aws_s3_bucket.documents.bucket
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "task_subnet_ids" {
  value = local.task_subnet_ids
}

output "assign_public_ip" {
  value = local.assign_public_ip
}

output "private_route_table_id" {
  value = aws_route_table.private.id
}

output "security_group_ids" {
  value = { for name, group in aws_security_group.this : name => group.id }
}

output "ecs_cluster_arn" {
  value = aws_ecs_cluster.main.arn
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_execution_role_arn" {
  value = aws_iam_role.ecs_execution.arn
}

output "api_task_role_arn" {
  value = aws_iam_role.api_task.arn
}

output "opensearch_client_role_arns" {
  value = [aws_iam_role.worker_task.arn, aws_iam_role.api_task.arn, aws_iam_role.ops_lambda.arn]
}

output "api_image" {
  value = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
}

output "api_log_group" {
  value = aws_cloudwatch_log_group.ecs_api.name
}

output "redis_secret_arn" {
  value = aws_secretsmanager_secret.redis_auth.arn
}

output "http_api_id" {
  value = aws_apigatewayv2_api.main.id
}

output "http_api_endpoint" {
  value = aws_apigatewayv2_api.main.api_endpoint
}

output "controller_role_arn" {
  value = aws_iam_role.controller.arn
}

output "api_consumer_policy_arn" {
  value = aws_iam_policy.api_consumer.arn
}

output "ecr_repository_urls" {
  value = { api = aws_ecr_repository.api.repository_url, worker = aws_ecr_repository.worker.repository_url }
}
