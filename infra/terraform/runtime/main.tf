# Disposable, hourly-billed infrastructure. ragctl applies and destroys this stack.
# It holds no durable data: OpenSearch is restored from verified S3 snapshots.

terraform {
  required_version = ">= 1.10"

  backend "s3" {
    key = "runtime/terraform.tfstate"
  }

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

variable "state_bucket" {
  type = string
}

variable "opensearch_engine_version" {
  description = "Stay on 2.x: the existing index uses nmslib, which blocks new indexes on 3.x."
  type        = string
  default     = "OpenSearch_2.19"
  validation {
    condition     = startswith(var.opensearch_engine_version, "OpenSearch_2.")
    error_message = "The index mapping uses the nmslib engine and requires OpenSearch 2.x."
  }
}

variable "opensearch_instance_type" {
  description = "t3 is the low-cost default. AWS does not recommend t3 for production workloads."
  type        = string
  default     = "t3.small.search"
}

variable "opensearch_instance_count" {
  type    = number
  default = 1
}

variable "opensearch_volume_gb" {
  type    = number
  default = 20
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "api_cpu" {
  type    = number
  default = 1024
}

variable "api_memory" {
  type    = number
  default = 3072
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "api_max_count" {
  type    = number
  default = 3
}

variable "bedrock_model_id" {
  type    = string
  default = "meta.llama3-8b-instruct-v1:0"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Stack       = "runtime"
    }
  }
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "foundation/terraform.tfstate"
    region = var.region
  }
}

locals {
  f      = data.terraform_remote_state.foundation.outputs
  prefix = local.f.prefix
  sg     = local.f.security_group_ids

  opensearch_subnets = slice(local.f.private_subnet_ids, 0, var.opensearch_instance_count > 1 ? 2 : 1)
}

# ------------------------------- NAT Gateway -------------------------------
resource "aws_eip" "nat" {
  count  = local.f.enable_nat_gateway ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${local.prefix}-nat" }
}

resource "aws_nat_gateway" "main" {
  count         = local.f.enable_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = local.f.public_subnet_ids[0]
  tags          = { Name = local.prefix }
}

resource "aws_route" "private_default" {
  count                  = local.f.enable_nat_gateway ? 1 : 0
  route_table_id         = local.f.private_route_table_id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[0].id
}

# -------------------------------- OpenSearch -------------------------------
data "aws_iam_policy_document" "opensearch_access" {
  statement {
    actions   = ["es:ESHttp*"]
    resources = ["arn:aws:es:${var.region}:${data.aws_caller_identity.current.account_id}:domain/${local.f.domain_name}/*"]
    principals {
      type        = "AWS"
      identifiers = local.f.opensearch_client_role_arns
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_opensearch_domain" "main" {
  domain_name     = local.f.domain_name
  engine_version  = var.opensearch_engine_version
  access_policies = data.aws_iam_policy_document.opensearch_access.json

  cluster_config {
    instance_type          = var.opensearch_instance_type
    instance_count         = var.opensearch_instance_count
    zone_awareness_enabled = var.opensearch_instance_count > 1
    dynamic "zone_awareness_config" {
      for_each = var.opensearch_instance_count > 1 ? [1] : []
      content {
        availability_zone_count = 2
      }
    }
  }

  ebs_options {
    ebs_enabled = true
    volume_type = "gp3"
    volume_size = var.opensearch_volume_gb
  }

  vpc_options {
    subnet_ids         = local.opensearch_subnets
    security_group_ids = [local.sg.opensearch]
  }

  encrypt_at_rest {
    enabled    = true
    kms_key_id = local.f.kms_key_arn
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  log_publishing_options {
    cloudwatch_log_group_arn = local.f.opensearch_log_group_arn
    log_type                 = "ES_APPLICATION_LOGS"
  }

  timeouts {
    create = "60m"
    update = "60m"
    delete = "60m"
  }
}

resource "aws_ssm_parameter" "opensearch_endpoint" {
  name  = local.f.opensearch_parameter
  type  = "String"
  value = aws_opensearch_domain.main.endpoint
}

# ----------------------------------- Redis ---------------------------------
data "aws_secretsmanager_secret_version" "redis_auth" {
  count     = local.f.enable_redis ? 1 : 0
  secret_id = local.f.redis_secret_arn
}

resource "aws_elasticache_subnet_group" "redis" {
  count      = local.f.enable_redis ? 1 : 0
  name       = "${local.prefix}-redis"
  subnet_ids = local.f.private_subnet_ids
}

resource "aws_elasticache_replication_group" "redis" {
  count                      = local.f.enable_redis ? 1 : 0
  replication_group_id       = "${local.prefix}-redis"
  description                = "Question and answer cache. Contents are disposable."
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 1
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.redis[0].name
  security_group_ids         = [local.sg.redis]
  at_rest_encryption_enabled = true
  kms_key_id                 = local.f.kms_key_arn
  transit_encryption_enabled = true
  auth_token                 = data.aws_secretsmanager_secret_version.redis_auth[0].secret_string
  snapshot_retention_limit   = 0
  apply_immediately          = true
}

# ------------------------------ Load balancer ------------------------------
resource "aws_lb" "api" {
  name                       = "${local.prefix}-api"
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [local.sg.alb]
  subnets                    = local.f.private_subnet_ids
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name                 = "${local.prefix}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = local.f.vpc_id
  deregistration_delay = 30
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 4
  }
}

resource "aws_lb_listener" "api" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# -------------------------------- ECS service ------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = local.f.ecs_execution_role_arn
  task_role_arn            = local.f.api_task_role_arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = local.f.api_image
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = [
      { name = "AWS_REGION", value = var.region },
      { name = "DOCUMENTS_BUCKET", value = local.f.documents_bucket },
      { name = "OPENSEARCH_ENDPOINT", value = aws_opensearch_domain.main.endpoint },
      { name = "OPENSEARCH_INDEX", value = local.f.opensearch_index },
      { name = "BEDROCK_MODEL_ID", value = var.bedrock_model_id },
      { name = "REDIS_HOST", value = local.f.enable_redis ? aws_elasticache_replication_group.redis[0].primary_endpoint_address : "" },
    ]
    secrets = local.f.enable_redis ? [{ name = "REDIS_AUTH_TOKEN", valueFrom = local.f.redis_secret_arn }] : []
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = local.f.api_log_group
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

# Created at zero tasks. ragctl scales up only after the search data is restored,
# and desired_count is therefore an operational value Terraform does not manage.
resource "aws_ecs_service" "api" {
  name                              = "${local.prefix}-api"
  cluster                           = local.f.ecs_cluster_arn
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = 0
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 300

  network_configuration {
    subnets          = local.f.task_subnet_ids
    security_groups  = [local.sg.api]
    assign_public_ip = local.f.assign_public_ip
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.api, aws_route.private_default]
}

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${local.f.ecs_cluster_name}/${aws_ecs_service.api.name}"
  min_capacity       = var.api_desired_count
  max_capacity       = var.api_max_count
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.prefix}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  resource_id        = aws_appautoscaling_target.api.resource_id
  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# ------------------------------- API Gateway -------------------------------
resource "aws_apigatewayv2_vpc_link" "api" {
  name               = "${local.prefix}-api"
  security_group_ids = [local.sg.vpc_link]
  subnet_ids         = local.f.private_subnet_ids
}

resource "aws_apigatewayv2_integration" "query" {
  api_id                 = local.f.http_api_id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  integration_uri        = aws_lb_listener.api.arn
  connection_type        = "VPC_LINK"
  connection_id          = aws_apigatewayv2_vpc_link.api.id
  payload_format_version = "1.0"
  timeout_milliseconds   = 29000
}

resource "aws_apigatewayv2_route" "query" {
  api_id             = local.f.http_api_id
  route_key          = "POST /query"
  target             = "integrations/${aws_apigatewayv2_integration.query.id}"
  authorization_type = "AWS_IAM"
}

# ---------------------------------- Outputs --------------------------------
output "opensearch_endpoint" {
  value = aws_opensearch_domain.main.endpoint
}

output "opensearch_engine_version" {
  value = aws_opensearch_domain.main.engine_version
}

output "alb_dns_name" {
  value = aws_lb.api.dns_name
}

output "target_group_arn" {
  value = aws_lb_target_group.api.arn
}

output "api_service_name" {
  value = aws_ecs_service.api.name
}

output "api_desired_count" {
  value = var.api_desired_count
}

output "redis_replication_group_id" {
  value = local.f.enable_redis ? aws_elasticache_replication_group.redis[0].id : null
}

output "nat_gateway_id" {
  value = local.f.enable_nat_gateway ? aws_nat_gateway.main[0].id : null
}
