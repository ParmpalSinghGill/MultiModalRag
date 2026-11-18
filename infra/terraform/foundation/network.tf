# VPC parts here have no hourly charge. NAT Gateway and ALB live in the runtime stack.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = local.prefix }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.prefix }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)
  tags              = { Name = "${local.prefix}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
  tags              = { Name = "${local.prefix}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.prefix}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

# The default route is added by the runtime stack and only while it is running.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.prefix}-private" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Gateway endpoints are free and keep S3 and DynamoDB traffic off the NAT Gateway.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.public.id]
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------
locals {
  security_groups = {
    vpc_link   = "API Gateway VPC Link"
    alb        = "Internal application load balancer"
    api        = "FastAPI tasks"
    worker     = "Ingestion tasks"
    opensearch = "OpenSearch domain"
    redis      = "Redis cache"
    ops        = "OpenSearch operations Lambda"
  }

  # source -> destination on a port. Each entry creates matching egress and ingress rules.
  security_group_flows = {
    vpc_link_to_alb      = { from = "vpc_link", to = "alb", port = 80 }
    ops_to_alb           = { from = "ops", to = "alb", port = 80 }
    alb_to_api           = { from = "alb", to = "api", port = 8000 }
    api_to_opensearch    = { from = "api", to = "opensearch", port = 443 }
    worker_to_opensearch = { from = "worker", to = "opensearch", port = 443 }
    ops_to_opensearch    = { from = "ops", to = "opensearch", port = 443 }
    api_to_redis         = { from = "api", to = "redis", port = 6379 }
  }
}

resource "aws_security_group" "this" {
  for_each    = local.security_groups
  name        = "${local.prefix}-${replace(each.key, "_", "-")}"
  description = each.value
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "flow" {
  for_each                     = local.security_group_flows
  security_group_id            = aws_security_group.this[each.value.from].id
  referenced_security_group_id = aws_security_group.this[each.value.to].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_ingress_rule" "flow" {
  for_each                     = local.security_group_flows
  security_group_id            = aws_security_group.this[each.value.to].id
  referenced_security_group_id = aws_security_group.this[each.value.from].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

# Tasks reach AWS APIs over HTTPS. No group allows inbound access from the internet.
resource "aws_vpc_security_group_egress_rule" "https" {
  for_each          = toset(["api", "worker"])
  security_group_id = aws_security_group.this[each.key].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
