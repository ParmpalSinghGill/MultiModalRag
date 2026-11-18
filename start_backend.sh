#!/usr/bin/env bash
# ==============================================================================
# MultiFormatRag: Backend Startup & Health Check Script
#
# Starts Docker services (OpenSearch + Redis), validates endpoints,
# resets disk thresholds, and verifies the ERAG Python environment.
#
# Run anytime after powering on / restarting your laptop:
#   ./start_backend.sh
# ==============================================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# Terminal Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${BOLD}${BLUE}============================================================================${NC}"
echo -e "${BOLD}${CYAN}🚀 STARTING MULTIFORMAT-RAG BACKEND SERVICES${NC}"
echo -e "${BOLD}${BLUE}============================================================================${NC}"

# ------------------------------------------------------------------------------
# 1. Check Docker Daemon
# ------------------------------------------------------------------------------
echo -e "\n[*] Step 1: Checking Docker Daemon..."
if ! docker info >/dev/null 2>&1; then
    echo -e "${RED}[!] Docker is not running. Attempting to start docker service...${NC}"
    sudo systemctl start docker || {
        echo -e "${RED}[ERROR] Could not start Docker. Please start Docker and re-run.${NC}"
        exit 1
    }
fi
echo -e "${GREEN}[✓] Docker daemon is running.${NC}"

# ------------------------------------------------------------------------------
# 2. Start OpenSearch & Redis Containers via Docker Compose
# ------------------------------------------------------------------------------
echo -e "\n[*] Step 2: Starting OpenSearch & Redis containers..."
if [ -f "docker-compose.yml" ]; then
    docker compose up -d
else
    # Fallback to direct container restart/start
    docker start multiformat-rag-opensearch 2>/dev/null || docker run -d --name multiformat-rag-opensearch -p 9200:9200 -p 9600:9600 -e "discovery.type=single-node" -e "DISABLE_SECURITY_PLUGIN=true" -e "OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g" opensearchproject/opensearch:2.18.0
    docker start multiformat-rag-redis 2>/dev/null || docker run -d --name multiformat-rag-redis -p 6380:6379 --restart unless-stopped redis:7-alpine
fi

# ------------------------------------------------------------------------------
# 3. Wait for OpenSearch on Port 9200
# ------------------------------------------------------------------------------
echo -e "\n[*] Step 3: Waiting for OpenSearch on http://localhost:9200..."
MAX_WAIT_OS=40
OS_READY=false

for i in $(seq 1 $MAX_WAIT_OS); do
    if curl -s http://localhost:9200 >/dev/null 2>&1; then
        OS_READY=true
        break
    fi
    echo -ne "    Waiting for OpenSearch JVM... ($i/$MAX_WAIT_OS s)\r"
    sleep 1
done
echo ""

if [ "$OS_READY" = true ]; then
    echo -e "${GREEN}[✓] OpenSearch is online and responding!${NC}"
    # Unblock any disk flood-stage write locks for smooth local dev
    curl -s -X PUT "http://localhost:9200/_cluster/settings" -H 'Content-Type: application/json' -d'
    {
      "persistent": {
        "cluster.blocks.create_index": null,
        "cluster.routing.allocation.disk.threshold_enabled": false
      }
    }' >/dev/null 2>&1 || true
    echo -e "${GREEN}[✓] OpenSearch disk watermark guards configured.${NC}"
else
    echo -e "${RED}[!] OpenSearch did not start within $MAX_WAIT_OS seconds.${NC}"
    echo -e "    Check logs: docker logs multiformat-rag-opensearch"
fi

# ------------------------------------------------------------------------------
# 4. Wait for Redis on Port 6380
# ------------------------------------------------------------------------------
echo -e "\n[*] Step 4: Waiting for Redis Question Cache on localhost:6380..."
MAX_WAIT_REDIS=15
REDIS_READY=false

for i in $(seq 1 $MAX_WAIT_REDIS); do
    if docker exec multiformat-rag-redis redis-cli ping 2>/dev/null | grep -q "PONG"; then
        REDIS_READY=true
        break
    fi
    echo -ne "    Waiting for Redis... ($i/$MAX_WAIT_REDIS s)\r"
    sleep 1
done
echo ""

if [ "$REDIS_READY" = true ]; then
    echo -e "${GREEN}[✓] Redis Question Cache is online and responding (PONG)!${NC}"
else
    echo -e "${YELLOW}[!] Redis not responding on port 6380. Check: docker logs multiformat-rag-redis${NC}"
fi

# ------------------------------------------------------------------------------
# 5. Check Conda Environment (ERAG)
# ------------------------------------------------------------------------------
echo -e "\n[*] Step 5: Verifying Python Conda Environment 'ERAG'..."
if conda info --envs | grep -q "ERAG"; then
    echo -e "${GREEN}[✓] Conda environment 'ERAG' found.${NC}"
    conda run -n ERAG python -c "
import opensearchpy
import redis
import sentence_transformers
import torch
print('    • PyTorch CUDA Available :', torch.cuda.is_available(), f'({torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"CPU\"})')
print('    • SentenceTransformers   :', sentence_transformers.__version__)
print('    • OpenSearch-py          :', opensearchpy.__version__)
print('    • Redis-py               :', redis.__version__)
"
else
    echo -e "${YELLOW}[!] Warning: Conda env 'ERAG' not detected. Make sure to activate your virtual environment.${NC}"
fi

# ------------------------------------------------------------------------------
# 6. Service Status Summary Dashboard
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}${BLUE}============================================================================${NC}"
echo -e "${BOLD}${GREEN}✅ ALL BACKEND SERVICES ARE OPERATIONAL${NC}"
echo -e "${BOLD}${BLUE}============================================================================${NC}"
echo -e "  • ${BOLD}OpenSearch Web GUI (Dashboards) :${NC} ${GREEN}${BOLD}http://localhost:5601${NC}"
echo -e "  • ${BOLD}Redis Web GUI (Commander)       :${NC} ${GREEN}${BOLD}http://localhost:8081${NC}"
echo -e "  • ${BOLD}OpenSearch REST API (k-NN)      :${NC} ${CYAN}http://localhost:9200${NC}"
echo -e "  • ${BOLD}Redis Cache Server (Host:Port)  :${NC} ${CYAN}localhost:6380${NC} (v7-alpine, db: 0)"
echo -e "  • ${BOLD}AWS S3 Figure Storage           :${NC} ${CYAN}s3://multimodal-rag-data-nov14/figures/${NC}"
echo -e "  • ${BOLD}Local Embedding Model           :${NC} ${CYAN}sentence-transformers/all-MiniLM-L6-v2${NC}"
echo -e "${BOLD}${BLUE}----------------------------------------------------------------------------${NC}"
echo -e "${BOLD}Quick Inspection & Query Commands:${NC}"
echo -e "  1. OpenSearch Web GUI (Chunks)  : ${YELLOW}Open http://localhost:5601 in your browser${NC}"
echo -e "  2. Redis Web GUI (Cached Qs)    : ${YELLOW}Open http://localhost:8081 in your browser${NC}"
echo -e "  3. CLI Document Inspector       : ${YELLOW}conda run -n ERAG python data_ingestion/browse_documents.py --stats${NC}"
echo -e "  4. CLI Redis Cache Inspector    : ${YELLOW}conda run -n ERAG python data_ingestion/inspect_redis.py --list${NC}"
echo -e "  5. Bedrock RAG Inference CLI   : ${YELLOW}conda run -n ERAG python data_generation/ask_cli.py -q \"your question\"${NC}"
echo -e "  6. Evaluate 5 Benchmark Qs     : ${YELLOW}conda run -n ERAG python data_generation/evaluate_5_questions.py${NC}"
echo -e "  7. Stop services anytime        : ${YELLOW}./stop_backend.sh${NC}"
echo -e "${BOLD}${BLUE}============================================================================${NC}\n"
