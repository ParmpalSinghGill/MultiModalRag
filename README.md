# 🏛️ MultiFormat RAG: Enterprise Multimodal Document Intelligence

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![OpenSearch 2.18](https://img.shields.io/badge/OpenSearch-2.18-005EB8?logo=opensearch&logoColor=white)](https://opensearch.org/)
[![Redis 7](https://img.shields.io/badge/Redis-7.0-DC382D?logo=redis&logoColor=white)](https://redis.io/)
[![AWS Bedrock](https://img.shields.io/badge/AWS-Bedrock-FF9900?logo=amazonaws&logoColor=white)](https://aws.amazon.com/bedrock/)
[![IBM Docling](https://img.shields.io/badge/Parser-IBM%20Docling-052FAD)](https://github.com/DS4SD/docling)
[![Terraform 1.10+](https://img.shields.io/badge/IaC-Terraform-7B42BC?logo=terraform&logoColor=white)](https://www.terraform.io/)
[![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

> **A production-ready, cost-effective Multimodal Retrieval-Augmented Generation (RAG) platform tailored for complex financial reports, balance sheets, statistical charts, and multi-format documents.**

---

## 🌟 Key Capabilities

- 📊 **Multimodal Document Understanding:** Parses complex layouts, multi-column reports, and financial balance sheets into structured Markdown grids using **IBM Docling**.
- 🖼️ **Cryptographic Asset Deduplication:** Computes SHA-256 image hashes to ensure **zero duplicate figure storage** across AWS S3 buckets while maintaining deterministic HTTPS links.
- 🔍 **Hybrid k-NN Vector & BM25 Search:** OpenSearch 2.18 index utilizing dense 384-d embeddings (`all-MiniLM-L6-v2`) with HNSW cosine similarity alongside BM25 full-text matching.
- ⚡ **Sub-Millisecond Redis Caching:** High-speed in-memory cache delivering **< 5 ms** warm recall for recurring queries, slashing latency and commercial LLM API costs.
- 🤖 **Low-Cost AWS Bedrock Inference:** Factually grounded answer synthesis with page-level citations using **Meta Llama 3 8B** (~$0.0005/query) and **Amazon Nova Micro** (~$0.0001/query).
- ☁️ **Two-Tier Disposable Cloud Architecture:** Fully automated Terraform deployments separating persistent foundation resources from disposable hourly-billed runtimes, managed via the `ragctl` CLI.

---

## 🏗️ Repository Architecture

```text
MultiFormatRag/
├── data_preparation/               # Tier 1: Download & S3 Sync
│   ├── DownloadData.py             # Benchmark question & document discovery
│   ├── upload_s3.py                # Raw document and metadata S3 synchronizer
│   ├── download_unidoc.py          # Standalone CLI downloader tool
│   ├── example_usage.py            # SDK usage examples
│   └── unidoc_downloader/          # Core stream downloader package
│
├── data_ingestion/                 # Tier 2: Ingestion, Chunking & S3 Deduplication
│   ├── ingest_pipeline.py          # Master pipeline: S3 -> Docling -> Hybrid Chunking
│   ├── docling_processor.py        # IBM Docling DocumentConverter + HybridChunker
│   ├── opensearch_indexer.py       # OpenSearch 2.18 k-NN vector store & hybrid search
│   ├── redis_question_cache.py     # Sub-millisecond Redis question/answer cache
│   ├── s3_figure_uploader.py       # SHA-256 deduplicated S3 figure uploader
│   ├── query_engine.py             # Hybrid search query engine with Redis caching
│   ├── browse_documents.py         # Terminal document & chunk inspector
│   ├── inspect_redis.py            # Redis cache CLI inspector
│   ├── raw_documents/              # Downloaded documents + ingestion_manifest.json
│   └── processed_chunks/           # Output semantic chunks (tables, figures, text)
│
├── data_generation/                # Tier 3: AWS Bedrock Inference & Evaluation
│   ├── bedrock_generator.py        # Bedrock Converse API with cost tracking & citations
│   ├── rag_pipeline.py             # Orchestrator (Redis Cache + OpenSearch + Bedrock)
│   ├── evaluate_5_questions.py     # Evaluation runner for benchmark questions
│   ├── ask_cli.py                  # Interactive REPL & CLI inference tool
│   └── evaluation_results_5_questions.json # Full benchmark evaluation report
│
├── infra/                          # Tier 4: AWS Production Cloud Infrastructure
│   ├── terraform/                  # Modular Terraform (Bootstrap, Foundation, Runtime)
│   ├── controller/ragctl           # Runtime management CLI (start, stop, status)
│   ├── lambda/                     # Event handlers & in-VPC OpenSearch proxies
│   └── bin/                        # Build and deployment automation scripts
│
├── services/                       # Containerized microservices (FastAPI & Workers)
├── docker-compose.yml              # Local OpenSearch, Dashboards, Redis, Commander
├── start_backend.sh                # Single-command startup & healthcheck script
├── stop_backend.sh                 # Graceful service shutdown script
├── architecture.md                 # Complete system architecture specification
├── GUI.md                          # Web GUI user guide (Dashboards :5601, Redis :8081)
├── requirements.txt                # Unified dependencies
└── README.md
```

---

## 🚀 Quick Start (Local Docker Setup)

### 1. Prerequisites
- Docker & Docker Compose
- Python 3.10+ (or Conda environment `ERAG`)
- AWS CLI configured with Bedrock access (`rag-dev` profile)

### 2. Environment Configuration
Copy the example environment configuration:
```bash
cp .env.example .env
```

### 3. Start Local Backend Services
Spin up OpenSearch, OpenSearch Dashboards, Redis, and Redis Commander:
```bash
./start_backend.sh
```
*Healthchecks and OpenSearch disk watermark guards are configured automatically.*

Web GUIs will be accessible immediately:
- **OpenSearch Dashboards:** [http://localhost:5601](http://localhost:5601)
- **Redis Commander GUI:** [http://localhost:8081](http://localhost:8081)

---

## 🔬 Running Retrieval & Generative Inference

### 1. Ask Any Question via Interactive CLI
```bash
# Query with specific document filter
conda run -n ERAG python data_generation/ask_cli.py -q "What is the NTA before tax for June 2021?" -d "6002966"

# Launch interactive Q&A REPL loop
conda run -n ERAG python data_generation/ask_cli.py --interactive

# Use ultra-low-cost Amazon Nova Micro
conda run -n ERAG python data_generation/ask_cli.py -m amazon.nova-micro-v1:0 -q "What is the revenue growth rate for 2013 younger founders?"
```

### 2. Run Benchmark Evaluation (5 Questions)
Evaluate the RAG pipeline against ground-truth answers:
```bash
# Evaluate with primary model (Meta Llama 3 8B)
conda run -n ERAG python data_generation/evaluate_5_questions.py

# Evaluate with Amazon Nova Micro (~1.1¢ per 100 queries)
conda run -n ERAG python data_generation/evaluate_5_questions.py --model-id amazon.nova-micro-v1:0
```

---

## 📈 Benchmark Evaluation Summary

| QID | Doc ID | Question Summary | Ground Truth Answer | Bedrock Generated Answer | Provenance | Latency |
|---|---|---|---|---|---|---|
| **0** | `3152577` | Variation in revenue growth for 2013 founder cohorts | Younger: 24.7%, Senior: 6.7% | *"...Younger owners: 24.7% growth rate, Senior owners: 6.7% growth rate..."* | `[Doc 3152577, Page 14]` | **3.39 ms** *(Cached)* |
| **1** | `6002966` | WAM Active NTA before tax (June 2021 vs May 2021) | June: 101.84c, May: 101.65c (+0.19c) | *"...June 2021 (101.84c) vs May 2021 (101.65c) = 0.19c increase..."* | `[Doc 6002966, Page 10]` | **3.35 s** *(Cold)* |
| **2** | `5510314` | Economic concern tied to 'Regulatory Expansion' | Increased regulations impacting global discussions | *"...Regulatory Expansion is an Econ Highlight mentioned in Page 15..."* | `[Doc 5510314, Page 15]` | **1.39 s** *(Cold)* |
| **3** | `4119285` | US federal budget shortfall to GDP shift 1980–2020 | Fluctuated, decreases in 90s/00s, pandemic dip to 25% | *"...According to Ex. 12 [Page 6], the budget deficit swelled to over 25% of GDP..."* | `[Doc 4119285, Page 6]` | **3.60 s** *(Cold)* |
| **4** | `6077605` | Impact of mating patterns on Gini index (1960 vs 2005) | 2005 Gini drops 0.43 to 0.34; 1960 drops slightly 0.34 to 0.33 | *"...In 1960, Gini drops slightly to 0.33... in 2005, Gini drops from 0.43 to 0.34..."* | `[Doc 6077605, Page 4]` | **4.16 s** *(Cold)* |

---

## ☁️ AWS Cloud Deployment & Runtime Controller (`ragctl`)

The `infra/` folder contains Terraform modules split into two operational layers:
1. **Foundation (`infra/terraform/foundation`):** Persistent, low-idle-cost resources (S3, VPC, ECR, IAM, Step Functions, DynamoDB, API Gateway).
2. **Runtime (`infra/terraform/runtime`):** Disposable hourly-billed resources (OpenSearch domain, ECS Fargate services, NAT Gateway, ALB, Redis).

Use `ragctl` to start and stop hourly-billed cloud resources whenever you are not working:
```bash
# Check status of cloud infrastructure
infra/bin/ragctl status

# Start disposable cloud runtime (OpenSearch, Fargate API, ALB)
infra/bin/ragctl start

# Teardown disposable runtime to prevent hourly cloud billing
infra/bin/ragctl stop
```

---

## 📚 Technical Documentation

- 🏛️ **[architecture.md](file:///media/parmpal/Data/Codes/VibeCoding/Rags/MultiFormatRag/architecture.md):** Deep technical specification with Mermaid sequence diagrams, data schemas, and mathematical normalization formulas.
- 🖥️ **[GUI.md](file:///media/parmpal/Data/Codes/VibeCoding/Rags/MultiFormatRag/GUI.md):** Detailed guide for exploring chunks and cache keys via OpenSearch Dashboards and Redis Commander.
- ☁️ **[infra/README.md](file:///media/parmpal/Data/Codes/VibeCoding/Rags/MultiFormatRag/infra/README.md):** AWS deployment guide, Step Functions layout, and `ragctl` controller operations.

---

## 📄 License

This project is licensed under the Apache 2.0 License.
