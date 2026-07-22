#!/usr/bin/env bash
set -euo pipefail

target_base="${1:-https://mcp.blocksize.info}"
target_base="${target_base%/}"

check_status() {
  local path="$1"
  local expected="$2"
  local actual
  actual="$(curl -sS -o /dev/null -w '%{http_code}' "${target_base}${path}")"
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL ${path}: expected ${expected}, got ${actual}" >&2
    return 1
  fi
  echo "PASS ${path}: ${actual}"
}

check_contains() {
  local path="$1"
  local expected_text="$2"
  local body
  body="$(curl -fsSL "${target_base}${path}")"
  if [[ "$body" != *"$expected_text"* ]]; then
    echo "FAIL ${path}: missing expected text: ${expected_text}" >&2
    return 1
  fi
  echo "PASS ${path}: contains ${expected_text}"
}

check_status "/health" "200"
check_status "/quickstart/first-price" "200"
check_status "/rwa-market-data" "200"
check_status "/market-data-licensing" "200"
check_status "/signed-oracle-feeds" "200"
check_status "/category-hubs.json" "200"
check_status "/evidence/rwa-coverage-index.html" "200"
check_status "/evidence/oracle-lineage-index.html" "200"
check_status "/pdf/Blocksize_RWA_Coverage_Index.pdf" "200"
check_status "/pdf/Blocksize_Oracle_Lineage_Index.pdf" "200"

check_contains "/quickstart/first-price" "Get a live Blocksize price"
check_contains "/rwa-market-data" "existing"
check_contains "/category-hubs.json" "evidence_indexes"
check_contains "/llms.txt" "RWA Coverage Index"

echo "Agentic marketing hosted smoke checks passed for ${target_base}"
