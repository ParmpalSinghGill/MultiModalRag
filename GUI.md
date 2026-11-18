# 🖥️ OpenSearch Dashboards GUI & Document Inspection Guide

This guide explains how to visually inspect, browse, and search all ingested documents, financial tables, and chart images using the **OpenSearch Dashboards Web GUI** and the **Terminal Document Inspector**.

---

## 📌 Quick Summary of Ingested Data in OpenSearch

The ingestion pipeline has already processed and pushed **all 5 documents** into the OpenSearch k-NN index `multimodal_rag_finance`:

- **Index Name:** `multimodal_rag_finance` (Status: `GREEN`, Size: `4.2 MB`)
- **Total Ingested Chunks:** **502 chunks**
  - **Visual Figures / Charts:** 149 chunks with high-res cropped PNGs, OCR text, neural classifier labels, and deterministic S3 URLs.
  - **Structured Financial Tables:** 45 chunks extracted into Markdown tables with page provenance.
  - **Prose & Section Chunks:** 308 chunks budgeted to 220 tokens safe limit.
- **Documents Breakdown:**
  - `3152577.pdf` (24 pages): 49 chunks (19 figures, 2 tables)
  - `6002966.pdf` (10 pages): 116 chunks (31 figures, 10 tables)
  - `5510314.pdf` (18 pages): 183 chunks (71 figures, 0 tables)
  - `4119285.pdf` (10 pages): 73 chunks (23 figures, 0 tables)
  - `6077605.pdf` (14 pages): 81 chunks (5 figures, 5 tables)

---

## 🌐 Option 1: OpenSearch Dashboards Web GUI

OpenSearch Dashboards is running locally in Docker with unauthenticated access enabled.

### 1. Access the Dashboard
Open your web browser and navigate to:
👉 **[http://localhost:5601](http://localhost:5601)**

*(No username or password is required. The security plugin is disabled for seamless local development.)*

---

### 2. Connect the Index Pattern (First-Time Setup)

To view and search chunks in the GUI, create an Index Pattern once:

1. In the left navigation menu (hamburger icon `☰` at top-left), scroll down to **Management** and click **Stack Management** (or **Index Patterns**).
2. Under **OpenSearch Dashboards**, click **Index Patterns**.
3. Click the blue **Create index pattern** button.
4. In the **Index pattern name** input field, type:
   ```text
   multimodal_rag_finance*
   ```
   *(You will see a green checkmark confirming that `multimodal_rag_finance` matched).*
5. Click **Next step**.
6. For **Time field**, leave it as **`I don't want to use the time filter`**.
7. Click **Create index pattern**.

---

### 3. Browse, Search & Filter in "Discover"

1. In the left navigation menu, click **Discover** (compass icon 🧭).
2. Ensure `multimodal_rag_finance*` is selected in the top-left dropdown.
3. You will see all **502 chunks** listed!

#### Adding Visible Columns:
In the left sidebar under **Available fields**, hover over and click **`+` (Add)** on:
- `document_id` — ID of source document (`3152577`, `6002966`, etc.)
- `page_numbers` — Page where chunk was found
- `chunk_type` — `figure_graph` or `text_or_table`
- `neural_class` — Neural classification (`bar_chart`, `line_chart`, etc.)
- `image_s3_url` — Deterministic AWS S3 image link
- `text` — Text / Markdown table content

#### Useful Search Queries in the Search Bar:
Type any of the following into the top search bar (Lucene or DQL syntax):

| Filter Goal | Query Syntax |
|---|---|
| **View all visual charts / figures** | `has_image: true` |
| **View all financial tables** | `has_table: true` |
| **Filter by specific document** | `document_id: "3152577"` |
| **Filter by chart classification** | `neural_class: "bar_chart"` |
| **Full-text search for a concept** | `text: "budget deficit"` or `"revenue growth"` |
| **Combined search** | `document_id: "6077605" AND has_image: true` |

Click on the **expand arrow `▶`** next to any document to view its full JSON representation, including its 384-dimensional dense vector embedding.

---

### 4. Direct Queries via "Dev Tools" (Console)

Click **Dev Tools** (wrench icon 🛠️) in the left sidebar to run direct OpenSearch REST API calls:

#### 1. Check Total Document Count:
```json
GET multimodal_rag_finance/_count
```

#### 2. Retrieve 5 Visual Figures with Captions & S3 URLs:
```json
GET multimodal_rag_finance/_search
{
  "query": {
    "term": { "has_image": true }
  },
  "size": 5,
  "_source": ["chunk_id", "document_id", "neural_class", "image_s3_url", "caption"]
}
```

#### 3. Run Full-Text BM25 Search:
```json
GET multimodal_rag_finance/_search
{
  "query": {
    "multi_match": {
      "query": "federal budget deficit GDP",
      "fields": ["text^2", "caption^3", "ocr_text"]
    }
  },
  "size": 3
}
```

#### 4. Run Vector k-NN Search:
```json
GET multimodal_rag_finance/_search
{
  "size": 2,
  "query": {
    "knn": {
      "embedding": {
        "vector": [0.0778, 0.0615, -0.0260, "... 384 floats ..."],
        "k": 2
      }
    }
  }
}
```

---

## 💻 Option 2: Interactive Terminal Document Inspector

If you want to quickly inspect documents without opening a browser, use [browse_documents.py](file:///media/parmpal/Data/Codes/VibeCoding/Rags/MultiFormatRag/data_ingestion/browse_documents.py):

### 1. View Index Stats & Document Breakdown:
```bash
conda run -n ERAG python data_ingestion/browse_documents.py --stats
```

### 2. View Visual Charts with Neural Classes & S3 Links:
```bash
conda run -n ERAG python data_ingestion/browse_documents.py --figures --limit 5
```

### 3. View Extracted Financial Tables in Markdown:
```bash
conda run -n ERAG python data_ingestion/browse_documents.py --tables --limit 5
```

### 4. Filter by Specific Document or Page:
```bash
conda run -n ERAG python data_ingestion/browse_documents.py --doc-id 3152577 --page 4
```

---

## ⚡ Option 3: Inspecting Redis Question Cache

You have 3 easy ways to inspect cached questions and answers in Redis:

### 1. Redis Web GUI: Redis Commander
Open your browser and navigate to:
👉 **[http://localhost:8081](http://localhost:8081)**

- Browse the hierarchical tree of Redis keys in `local:0`:
  - `rag:qcache:*` — Individual cached questions with full JSON payloads, retrieval results, and TTL.
  - `rag:qcache:qa:*` — Cached ground-truth QA benchmark pairs.
  - `rag:qcache:stats:*` — Telemetry counters (`hits`, `misses`).
- Click any key to inspect its value, see the countdown timer for its TTL, or delete/edit keys visually!

---

### 2. Interactive Terminal Redis Inspector ([inspect_redis.py](file:///media/parmpal/Data/Codes/VibeCoding/Rags/MultiFormatRag/data_ingestion/inspect_redis.py))

Inspect cached questions, answers, and telemetry directly in your shell:

```bash
# 1. View Cache Hit Rate, Total Queries, and Memory Footprint
conda run -n ERAG python data_ingestion/inspect_redis.py --stats

# 2. List all cached questions with their document filters and remaining TTL
conda run -n ERAG python data_ingestion/inspect_redis.py --list

# 3. Deep inspect a specific cached question (e.g. searching by keyword)
conda run -n ERAG python data_ingestion/inspect_redis.py --view "mating"

# 4. Flush / clear the question cache
conda run -n ERAG python data_ingestion/inspect_redis.py --flush
```

---

### 3. Direct Redis CLI (Docker)

If you prefer using the native `redis-cli`:
```bash
# Open interactive redis-cli inside container
docker exec -it multiformat-rag-redis redis-cli

# Check all question cache keys
KEYS rag:qcache:*

# Check remaining TTL (in seconds) for a key
TTL rag:qcache:2d66ecca82fa44fd

# Check memory usage
INFO memory
```

---

## 🔬 Option 4: Testing RAG Query Engine with Redis Caching

To test what the RAG receives for any question with sub-millisecond Redis caching:

```bash
# Ask any custom question
conda run -n ERAG python data_ingestion/query_engine.py -q "What is the predicted firm exit probability for 30-year-old founders?"

# Inspect all 5 benchmark questions side-by-side with ground-truth answers
conda run -n ERAG python data_ingestion/inspect_5_questions.py

# View Redis cache hit rate and memory stats
conda run -n ERAG python data_ingestion/query_engine.py --stats
```

---

## 🤖 Option 5: AWS Bedrock Generative Inference & Benchmark Evaluation

The **`data_generation/`** module integrates AWS Bedrock foundation models with OpenSearch hybrid retrieval and Redis question caching to generate grounded financial answers.

### 1. Supported Low-Cost Bedrock Models

| Model ID | Name | Input / 1K Tokens | Output / 1K Tokens | Cost per 100 Queries |
|---|---|---|---|---|
| `meta.llama3-8b-instruct-v1:0` | **Meta Llama 3 8B Instruct** *(Default)* | $0.00030 | $0.00060 | ~5.6¢ |
| `amazon.nova-micro-v1:0` | **Amazon Nova Micro** | $0.000035 | $0.00014 | ~1.1¢ |
| `amazon.nova-lite-v1:0` | **Amazon Nova Lite** | $0.000060 | $0.00024 | ~2.2¢ |
| `mistral.mistral-7b-instruct-v0:2` | **Mistral 7B Instruct v0.2** | $0.00015 | $0.00020 | ~2.9¢ |

---

### 2. Run Benchmark Evaluation (5 Questions)

Run the full evaluation pipeline across all 5 benchmark questions from `selected_questions_5.json`:

```bash
# Evaluate with primary model (Meta Llama 3 8B)
conda run -n ERAG python data_generation/evaluate_5_questions.py

# Evaluate with ultra-low-cost Amazon Nova Micro (~1 cent per 100 queries)
conda run -n ERAG python data_generation/evaluate_5_questions.py --model-id amazon.nova-micro-v1:0

# Evaluate a specific question (e.g. Question index 0)
conda run -n ERAG python data_generation/evaluate_5_questions.py --qid 0

# Bypass Redis cache to force fresh Bedrock invocations
conda run -n ERAG python data_generation/evaluate_5_questions.py --bypass-cache
```

The script outputs:
- **Ground Truth Answer vs. Bedrock Generated Answer** side-by-side.
- **Retrieved Chunks & Page Numbers** (`[Doc 3152577, Page 14]`).
- **S3 Figure URLs** for visual charts and graphs.
- **Latency Telemetry** (retrieval time, generation time, total time).
- **Token Counts & Estimated Cost in USD**.
- **Redis Caching Acceleration** demonstrating warm repeat queries returning in `< 4 ms`!
- Saves full report to `data_generation/evaluation_results_5_questions.json`.

---

### 3. Interactive CLI Inference (`ask_cli.py`)

Ask arbitrary questions from your terminal:

```bash
# Ask a specific question filtered to a document
conda run -n ERAG python data_generation/ask_cli.py -q "What is the NTA before tax for June 2021?" -d "6002966"

# Launch interactive Q&A REPL loop
conda run -n ERAG python data_generation/ask_cli.py --interactive

# Use Amazon Nova Micro
conda run -n ERAG python data_generation/ask_cli.py -m amazon.nova-micro-v1:0 -q "What is the revenue growth rate for 2013 younger founders?"
```

---

## 🛠️ Managing Backend Services

Use the automated shell scripts whenever starting or stopping your laptop:

```bash
# Start OpenSearch (9200), Dashboards GUI (5601), and Redis (6380)
./start_backend.sh

# Stop all 4 services gracefully
./stop_backend.sh
```
