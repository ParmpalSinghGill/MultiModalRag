# Offline plan checks. Run: terraform init -backend=false && terraform test
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:q", url = "https://sqs.example/q" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = {
      arn        = "arn:aws:lambda:us-east-1:123456789012:function:mock"
      invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/mock/invocations"
    }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/mock" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_apigatewayv2_api" {
    defaults = { execution_arn = "arn:aws:execute-api:us-east-1:123456789012:mock", api_endpoint = "https://mock.example" }
  }
}

variables {
  state_bucket = "state-bucket"
  image_tag    = "v1"
}

run "default_foundation" {
  command = apply

  assert {
    condition     = aws_pipes_pipe.ingest.desired_state == "STOPPED"
    error_message = "A new pipe must stay stopped until the runtime is verified."
  }
  assert {
    condition     = aws_sfn_state_machine.ingest.type == "STANDARD"
    error_message = "Ingestion uses a Standard workflow."
  }
  assert {
    condition     = can(jsondecode(aws_sfn_state_machine.ingest.definition).States.ForEachDocument)
    error_message = "The rendered workflow must be valid JSON."
  }
  assert {
    condition     = jsondecode(aws_ssm_parameter.config.value).prefix == "mfrag-prod"
    error_message = "Controller configuration must be published."
  }
  assert {
    condition     = length(aws_cloudtrail.main) == 1
    error_message = "CloudTrail is enabled by default."
  }
  assert {
    condition     = output.assign_public_ip == false
    error_message = "With NAT enabled, tasks must not receive public IPs."
  }
}

run "no_nat_mode_uses_public_task_subnets" {
  command   = plan
  state_key = "no_nat" # Separate state: protected resources cannot be planned away.
  variables {
    enable_nat_gateway = false
    enable_cloudtrail  = false
  }
  assert {
    condition     = output.assign_public_ip && length(aws_cloudtrail.main) == 0
    error_message = "Without NAT, tasks need public IPs for AWS API access."
  }
}
