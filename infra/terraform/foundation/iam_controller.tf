# Role for ragctl. It can operate the platform and manage only runtime resources.
# It cannot change foundation resources or delete protected data.

locals {
  arn_region = "${local.partition}:%s:${var.region}:${local.account_id}"
}

data "aws_iam_policy_document" "controller_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = length(var.controller_trusted_principal_arns) > 0 ? var.controller_trusted_principal_arns : ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "controller" {
  name                 = "${local.prefix}-controller"
  assume_role_policy   = data.aws_iam_policy_document.controller_assume.json
  max_session_duration = 14400
}

data "aws_iam_policy_document" "controller_operations" {
  statement {
    sid       = "ReadConfiguration"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.config.arn]
  }
  statement {
    sid       = "ControlState"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.control.arn]
  }
  statement {
    sid       = "ReconcileDocuments"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.documents.arn]
  }
  statement {
    sid       = "PipeOperations"
    actions   = ["pipes:DescribePipe", "pipes:StartPipe", "pipes:StopPipe"]
    resources = [aws_pipes_pipe.ingest.arn]
  }
  statement {
    sid       = "DrainExecutions"
    actions   = ["states:ListExecutions"]
    resources = [aws_sfn_state_machine.ingest.arn]
  }
  statement {
    actions   = ["states:DescribeExecution", "states:StopExecution"]
    resources = [local.execution_arn_glob]
  }
  statement {
    sid       = "QueueOperations"
    actions   = ["sqs:GetQueueAttributes", "sqs:SendMessage"]
    resources = [aws_sqs_queue.ingest.arn]
  }
  statement {
    actions   = ["sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.dlq.arn]
  }
  statement {
    sid       = "InvokeOperationsFunction"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.opensearch_ops.arn]
  }
  statement {
    sid       = "Manifests"
    actions   = ["s3:GetObject", "s3:GetObjectAttributes", "s3:PutObject"]
    resources = ["${local.backups_arn}/${local.manifest_prefix}/*"]
  }
  statement {
    sid       = "VerifySnapshotFiles"
    actions   = ["s3:GetObject"]
    resources = ["${local.backups_arn}/${local.snapshot_base_path}/*"]
  }
  statement {
    # Unconditional so that reads of missing manifests return 404, not 403.
    actions   = ["s3:ListBucket"]
    resources = [local.backups_arn]
  }
  statement {
    sid       = "ListRawDocuments"
    actions   = ["s3:ListBucket"]
    resources = [local.documents_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["raw/*"]
    }
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [local.kms_arn]
  }
  statement {
    sid       = "TerraformState"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:${local.partition}:s3:::${var.state_bucket}/runtime/*"]
  }
  statement {
    sid       = "ReadFoundationState"
    actions   = ["s3:GetObject"]
    resources = ["arn:${local.partition}:s3:::${var.state_bucket}/foundation/terraform.tfstate"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:${local.partition}:s3:::${var.state_bucket}"]
  }
  statement {
    sid       = "Preflight"
    actions   = ["ecr:DescribeImages"]
    resources = [aws_ecr_repository.api.arn, aws_ecr_repository.worker.arn]
  }
  statement {
    actions   = ["iam:GetRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/aws-service-role/opensearchservice.amazonaws.com/*"]
  }
}

data "aws_iam_policy_document" "controller_runtime" {
  statement {
    sid = "ReadOnlyDiscovery"
    actions = [
      "ec2:Describe*", "elasticloadbalancing:Describe*", "es:Describe*", "es:List*", "es:GetCompatibleVersions",
      "elasticache:Describe*", "elasticache:ListTagsForResource", "ecs:Describe*", "ecs:List*",
      "apigateway:GET", "application-autoscaling:Describe*", "cloudwatch:DescribeAlarms",
      "ssm:DescribeParameters", "sts:GetCallerIdentity", "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "OpenSearchDomain"
    actions   = ["es:CreateDomain", "es:DeleteDomain", "es:UpdateDomainConfig", "es:AddTags", "es:RemoveTags"]
    resources = [local.domain_arn]
  }
  statement {
    sid       = "EncryptManagedServices"
    actions   = ["kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant"]
    resources = [local.kms_arn]
    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "kms:ViaService"
      values   = ["es.${var.region}.amazonaws.com", "elasticache.${var.region}.amazonaws.com"]
    }
  }
  statement {
    sid = "NatAndRouting"
    actions = [
      "ec2:AllocateAddress", "ec2:ReleaseAddress", "ec2:CreateNatGateway", "ec2:DeleteNatGateway",
      "ec2:CreateTags", "ec2:DeleteTags", "ec2:CreateRoute", "ec2:DeleteRoute", "ec2:AssociateAddress",
      "ec2:DisassociateAddress",
    ]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:Region"
      values   = [var.region]
    }
  }
  statement {
    sid       = "ManagedServiceNetworkInterfaces"
    actions   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:ModifyNetworkInterfaceAttribute"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:Region"
      values   = [var.region]
    }
  }
  statement {
    sid     = "LoadBalancer"
    actions = ["elasticloadbalancing:*"]
    resources = [
      "arn:${format(local.arn_region, "elasticloadbalancing")}:loadbalancer/app/${local.prefix}-*",
      "arn:${format(local.arn_region, "elasticloadbalancing")}:listener/app/${local.prefix}-*",
      "arn:${format(local.arn_region, "elasticloadbalancing")}:listener-rule/app/${local.prefix}-*",
      "arn:${format(local.arn_region, "elasticloadbalancing")}:targetgroup/${local.prefix}-*",
    ]
  }
  statement {
    sid = "ApiService"
    actions = [
      "ecs:CreateService", "ecs:UpdateService", "ecs:DeleteService", "ecs:TagResource", "ecs:UntagResource",
    ]
    resources = [
      "arn:${format(local.arn_region, "ecs")}:service/${aws_ecs_cluster.main.name}/${local.prefix}-api",
      "arn:${format(local.arn_region, "ecs")}:task-definition/${local.prefix}-api:*",
    ]
  }
  statement {
    # ECS does not support resource-level permissions for these actions.
    sid       = "ApiTaskDefinition"
    actions   = ["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition"]
    resources = ["*"]
  }
  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_execution.arn, aws_iam_role.api_task.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid       = "ApiAutoscaling"
    actions   = ["application-autoscaling:RegisterScalableTarget", "application-autoscaling:DeregisterScalableTarget", "application-autoscaling:PutScalingPolicy", "application-autoscaling:DeleteScalingPolicy", "application-autoscaling:TagResource"]
    resources = ["*"]
  }
  statement {
    sid       = "ApiAutoscalingAlarms"
    actions   = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms"]
    resources = ["arn:${format(local.arn_region, "cloudwatch")}:alarm:TargetTracking-service/${aws_ecs_cluster.main.name}/${local.prefix}-api-*"]
  }
  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values = [
        "elasticloadbalancing.amazonaws.com", "ecs.application-autoscaling.amazonaws.com",
        "elasticache.amazonaws.com", "ecs.amazonaws.com", "ops.apigateway.amazonaws.com",
      ]
    }
  }
  statement {
    sid     = "ApiRoutes"
    actions = ["apigateway:POST", "apigateway:PATCH", "apigateway:DELETE", "apigateway:TagResource", "apigateway:UntagResource"]
    resources = [
      "arn:${local.partition}:apigateway:${var.region}::/apis/${aws_apigatewayv2_api.main.id}/routes",
      "arn:${local.partition}:apigateway:${var.region}::/apis/${aws_apigatewayv2_api.main.id}/routes/*",
      "arn:${local.partition}:apigateway:${var.region}::/apis/${aws_apigatewayv2_api.main.id}/integrations",
      "arn:${local.partition}:apigateway:${var.region}::/apis/${aws_apigatewayv2_api.main.id}/integrations/*",
      "arn:${local.partition}:apigateway:${var.region}::/vpclinks",
      "arn:${local.partition}:apigateway:${var.region}::/vpclinks/*",
      "arn:${local.partition}:apigateway:${var.region}::/tags/*",
    ]
  }
  statement {
    sid       = "RuntimeParameters"
    actions   = ["ssm:PutParameter", "ssm:DeleteParameter", "ssm:GetParameter", "ssm:GetParameters", "ssm:AddTagsToResource", "ssm:ListTagsForResource", "ssm:RemoveTagsFromResource"]
    resources = ["arn:${format(local.arn_region, "ssm")}:parameter${local.runtime_parameter_path}/*"]
  }
  statement {
    sid       = "RedisAuthToken"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.redis_auth.arn]
  }
  statement {
    sid = "RedisCache"
    actions = [
      "elasticache:CreateReplicationGroup", "elasticache:DeleteReplicationGroup", "elasticache:ModifyReplicationGroup",
      "elasticache:CreateCacheCluster", "elasticache:DeleteCacheCluster", "elasticache:CreateCacheSubnetGroup",
      "elasticache:DeleteCacheSubnetGroup", "elasticache:ModifyCacheSubnetGroup", "elasticache:AddTagsToResource",
      "elasticache:RemoveTagsFromResource",
    ]
    resources = [
      "arn:${format(local.arn_region, "elasticache")}:replicationgroup:${local.prefix}-*",
      "arn:${format(local.arn_region, "elasticache")}:cluster:${local.prefix}-*",
      "arn:${format(local.arn_region, "elasticache")}:subnetgroup:${local.prefix}-*",
      "arn:${format(local.arn_region, "elasticache")}:parametergroup:*",
      "arn:${format(local.arn_region, "elasticache")}:securitygroup:*",
    ]
  }
  statement {
    sid       = "PublishDomainLogs"
    actions   = ["logs:ListTagsForResource"]
    resources = ["${local.log_arn_base}:*"]
  }
}

resource "aws_iam_policy" "controller_operations" {
  name   = "${local.prefix}-controller-operations"
  policy = data.aws_iam_policy_document.controller_operations.json
}

resource "aws_iam_policy" "controller_runtime" {
  name   = "${local.prefix}-controller-runtime"
  policy = data.aws_iam_policy_document.controller_runtime.json
}

resource "aws_iam_role_policy_attachment" "controller_operations" {
  role       = aws_iam_role.controller.name
  policy_arn = aws_iam_policy.controller_operations.arn
}

resource "aws_iam_role_policy_attachment" "controller_runtime" {
  role       = aws_iam_role.controller.name
  policy_arn = aws_iam_policy.controller_runtime.arn
}
