#!/usr/bin/env bash
# ==============================================================================
# MultiFormatRag: Backend Shutdown Script
#
# Stops OpenSearch and Redis Docker containers gracefully.
#
# Usage:
#   ./stop_backend.sh
# ==============================================================================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "============================================================================"
echo "🛑 STOPPING MULTIFORMAT-RAG BACKEND SERVICES (OPENSEARCH + REDIS)"
echo "============================================================================"

if [ -f "docker-compose.yml" ]; then
    docker compose down
else
    docker stop multiformat-rag-opensearch multiformat-rag-dashboards multiformat-rag-redis multiformat-rag-redis-commander 2>/dev/null || true
fi

echo "[✓] Backend services stopped successfully."
echo "============================================================================"
