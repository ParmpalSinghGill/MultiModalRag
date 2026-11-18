# AWS infrastructure and runtime controller

This directory deploys MultiFormat RAG to AWS and provides `ragctl`, which shuts down
hourly-billed infrastructure when you are not working and restores it later.

## Layout

| Path | Purpose |
|---|---|
| `terraform/bootstrap` | Terraform state bucket |
| `terraform/foundation` | Persistent, low-idle-cost resources, in their own state |
| `terraform/runtime` | Disposable, hourly-billed resources, in their own state |
| `controller/ragctl` | `start`, `stop` and `status` |
| `lambda/` | Workflow tasks, always-on API routes, in-VPC OpenSearch operations |
| `../services` | FastAPI service, ingestion worker and Dockerfiles |

```text
S3 raw/ -> SQS (+DLQ) -> EventBridge Pipe -> Step Functions Standard
                                               |- Fargate parse: Docling, chunks, figures -> S3
                                               `- Fargate index: embeddings -> OpenSearch
Client (SigV4) -> API Gateway -> Lambda: /documents, /jobs/{doc_id}, /status, 503 fallback
                              `-> VPC Link -> internal ALB -> FastAPI on Fargate -> OpenSearch, Bedrock, Redis
```

| Foundation: always kept | Runtime: deleted by `stop` |
|---|---|
| S3, DynamoDB, SQS and DLQ, KMS, Secrets Manager | OpenSearch domain |
| VPC, subnets, security groups, free S3/DynamoDB endpoints | NAT Gateway and Elastic IP |
| ECR, IAM, ECS cluster, worker task definitions | ALB, target group, listener |
| Step Functions, EventBridge Pipe, Lambda, API Gateway | ECS API service, autoscaling |
| CloudWatch logs and alarms, CloudTrail, Budgets | VPC Link, `/query` route, Redis |

### Who owns what

Terraform alone creates and deletes resources. `ragctl` uses Boto3 only for reads and
operational switches: the pipe's running state, the ECS desired count and the ingestion
gate. Terraform ignores those two resource attributes, so the tools cannot fight.

## First deployment

Requires Terraform 1.10+, AWS CLI, Docker, Python 3.10+ with `boto3`, and administrator
credentials for the bootstrap and foundation stacks.

```bash
# 1. State bucket
terraform -chdir=infra/terraform/bootstrap init
terraform -chdir=infra/terraform/bootstrap apply
terraform -chdir=infra/terraform/bootstrap output -raw backend_config > infra/terraform/backend.hcl

# 2. Foundation. Edit terraform.tfvars first.
cp infra/terraform/foundation/terraform.tfvars.example infra/terraform/foundation/terraform.tfvars
terraform -chdir=infra/terraform/foundation init -backend-config=../backend.hcl
terraform -chdir=infra/terraform/foundation apply

# 3. Images, using the same image_tag as terraform.tfvars
infra/bin/build-images 2026-11-18-1

# 4. Start the runtime
infra/bin/ragctl start

# 5. Copy existing documents. They queue and process automatically.
aws s3 sync s3://<existing-bucket>/raw/ s3://<documents-bucket>/raw/
```

Enable access to your chosen models in Amazon Bedrock before querying. The existing
bucket is not modified. For day-to-day use, point `AWS_PROFILE` at a profile that assumes
the `controller_role_arn` output.

To deploy a new image, build it with a new tag, set `image_tag`, apply the foundation
stack, then run `ragctl stop` and `ragctl start`.

## Daily use

```bash
infra/bin/ragctl status            # add --json for automation
infra/bin/ragctl stop --dry-run
infra/bin/ragctl stop              # asks you to type: stop mfrag-prod
infra/bin/ragctl start --dry-run
infra/bin/ragctl start
```

| Option | Effect |
|---|---|
| `--yes` | Skips typed confirmation for automation |
| `stop --drain-timeout N` | Seconds to wait for active ingestion (default 3600) |
| `stop --requeue-on-timeout` | After the timeout, requeues active work, then stops it |
| `start --ignore-snapshot` | Creates an empty index and re-indexes from S3 |
| `start --skip-reconcile` | Skips the S3 scan for documents without an outcome |

Every command is safe to repeat. After any failure or Ctrl-C, fix the cause and run the
same command: it resumes and checks real AWS state before each step. Logs are written to
`controller/logs/`, and each step is recorded in the DynamoDB control table for 90 days.

## What protects your data

- `stop` deletes OpenSearch only after a snapshot reaches `SUCCESS`, has no failed shards,
  contains every index, has its files in S3, and its manifest passes read-back.
- A domain that never passed `start` verification is not snapshotted, so a partial restore
  cannot replace a good backup.
- `start` refuses a corrupt or incomplete backup before creating billed resources. It
  creates an empty index only if no backup exists or you pass `--ignore-snapshot`.
- Terraform plans are rejected if they touch any resource type outside the runtime list,
  or would replace OpenSearch outside `stop`.
- Bucket policies deny object deletion under `raw/`, `processed/`, `figures/`,
  `infrastructure-state/` and `opensearch-snapshots/`. Only principals you list, and the
  snapshot role inside its own repository, are exempt.
- Stateful foundation resources use `prevent_destroy`; DynamoDB also has deletion
  protection and point-in-time recovery.

Queued messages expire after 14 days, and an alarm fires at 10 days. `start` still recovers
expired work by comparing `raw/` with the job table.

## Cost controls

Check current prices for your region; these items drive the bill.

- While running: OpenSearch, NAT Gateway and its public IP, ALB, the API task, and Redis
  if enabled. Ingestion tasks bill only while processing and use Fargate Spot by default.
- While stopped: mainly the KMS key, one secret, CloudWatch alarms and stored data.
- `enable_nat_gateway = false` removes the NAT Gateway. Tasks then use public IPs, with
  security groups that allow no inbound internet traffic.
- `t3.small.search` is the low-cost default. AWS does not recommend T3 for production, so
  choose a larger type and two or more instances in `runtime/terraform.tfvars` when needed.
- Set `enable_cloudtrail = false` if the account already has a management-event trail.

## API

All routes require IAM SigV4. Attach the `api_consumer_policy_arn` output to callers.

| Route | Available while stopped | Purpose |
|---|---|---|
| `POST /documents` | Returns 503 | Returns a presigned upload URL |
| `GET /jobs/{doc_id}` | Yes | Ingestion status |
| `GET /status` | Yes | Platform state |
| `POST /query` | Returns 503 | RAG answer with signed figure links |

## Tests

```bash
pip install pytest boto3
PYTHONPATH=infra/controller python -m pytest infra/controller/tests

terraform -chdir=infra/terraform/foundation init -backend=false
terraform -chdir=infra/terraform/foundation test
terraform -chdir=infra/terraform/runtime init -backend=false
terraform -chdir=infra/terraform/runtime test
```
