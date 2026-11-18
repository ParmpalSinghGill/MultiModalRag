# Offline plan checks. Run: terraform init -backend=false && terraform test
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/mock/1", dns_name = "internal.elb.amazonaws.com" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/1" }
  }
  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/mock/1/2" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
}

variables {
  state_bucket = "state-bucket"
}

override_data {
  target = data.terraform_remote_state.foundation
  values = {
    outputs = {
      prefix                      = "mfrag-prod"
      enable_nat_gateway          = true
      enable_redis                = false
      domain_name                 = "mfrag-prod-search"
      opensearch_parameter        = "/mfrag-prod/runtime/opensearch_endpoint"
      opensearch_log_group_arn    = "arn:aws:logs:us-east-1:123456789012:log-group:os"
      opensearch_index            = "multimodal_rag_finance"
      opensearch_client_role_arns = ["arn:aws:iam::123456789012:role/a"]
      kms_key_arn                 = "arn:aws:kms:us-east-1:123456789012:key/k"
      documents_bucket            = "docs"
      vpc_id                      = "vpc-1"
      public_subnet_ids           = ["subnet-a", "subnet-b"]
      private_subnet_ids          = ["subnet-c", "subnet-d"]
      task_subnet_ids             = ["subnet-c", "subnet-d"]
      assign_public_ip            = false
      private_route_table_id      = "rtb-1"
      security_group_ids          = { vpc_link = "sg-1", alb = "sg-2", api = "sg-3", worker = "sg-4", opensearch = "sg-5", redis = "sg-6", ops = "sg-7" }
      ecs_cluster_arn             = "arn:aws:ecs:us-east-1:123456789012:cluster/c"
      ecs_cluster_name            = "c"
      ecs_execution_role_arn      = "arn:aws:iam::123456789012:role/e"
      api_task_role_arn           = "arn:aws:iam::123456789012:role/t"
      api_image                   = "repo/api:v1"
      api_log_group               = "/ecs/api"
      redis_secret_arn            = "arn:aws:secretsmanager:us-east-1:123456789012:secret:s"
      http_api_id                 = "api1"
    }
  }
}

run "runtime_defaults" {
  command = apply

  assert {
    condition     = aws_ecs_service.api.desired_count == 0
    error_message = "The API must start at zero tasks until search data is restored."
  }
  assert {
    condition     = length(aws_nat_gateway.main) == 1 && length(aws_elasticache_replication_group.redis) == 0
    error_message = "Default runtime has one NAT Gateway and no Redis."
  }
  assert {
    condition     = startswith(aws_opensearch_domain.main.engine_version, "OpenSearch_2.")
    error_message = "The nmslib mapping requires OpenSearch 2.x."
  }
  assert {
    condition     = aws_lb.api.internal
    error_message = "The ALB must not be internet-facing."
  }
}

run "reject_opensearch_3" {
  command = plan
  variables {
    opensearch_engine_version = "OpenSearch_3.1"
  }
  expect_failures = [var.opensearch_engine_version]
}
