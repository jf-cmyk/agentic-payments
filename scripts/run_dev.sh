#!/bin/bash
# =============================================================================
# Blocksize MCP + x402 Development Launch Script
# =============================================================================
#
# Usage:
#   ./scripts/run_dev.sh          # Run MCP server (STDIO mode)
#   ./scripts/run_dev.sh resource  # Run resource server (HTTP mode)
#   ./scripts/run_dev.sh both      # Run both servers
#   ./scripts/run_dev.sh test      # Run tests
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Blocksize Capital MCP + x402 Server         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"

# Check for .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo -e "${YELLOW}Warning: .env file not found. Copying from .env.example...${NC}"
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo -e "${YELLOW}Please edit .env with your API keys before running.${NC}"
    exit 1
fi

MODE="${1:-mcp}"

case "$MODE" in
    mcp)
        echo -e "${GREEN}Starting MCP Server (STDIO mode)...${NC}"
        cd "$PROJECT_DIR"
        python -m src.mcp_server
        ;;

    resource)
        echo -e "${GREEN}Starting Resource Server (HTTP mode) on port 8402...${NC}"
        cd "$PROJECT_DIR"
        uvicorn src.resource_server:app --reload --port 8402 --host 0.0.0.0
        ;;

    both)
        echo -e "${GREEN}Starting both servers...${NC}"
        echo -e "${BLUE}Resource Server on :8402${NC}"
        echo -e "${BLUE}MCP Server on :8403 (streamable-http)${NC}"
        cd "$PROJECT_DIR"

        # Start resource server in background
        uvicorn src.resource_server:app --port 8402 --host 0.0.0.0 &
        RESOURCE_PID=$!

        # Start MCP server in HTTP mode
        MCP_TRANSPORT=streamable-http python -m src.mcp_server &
        MCP_PID=$!

        # Trap to clean up both on exit
        trap "kill $RESOURCE_PID $MCP_PID 2>/dev/null" EXIT

        echo -e "${GREEN}Both servers running. Press Ctrl+C to stop.${NC}"
        wait
        ;;

    test)
        echo -e "${GREEN}Running tests...${NC}"
        cd "$PROJECT_DIR"
        pytest -v tests/
        ;;

    *)
        echo "Usage: $0 {mcp|resource|both|test}"
        exit 1
        ;;
esac
