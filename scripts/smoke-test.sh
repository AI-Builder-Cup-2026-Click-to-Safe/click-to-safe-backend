#!/usr/bin/env bash
set -euo pipefail
: "${QUERY_URL:?Set QUERY_URL}"
# This proves the deployed revision can serve requests without incurring a Gemini call.
# Never enable shell tracing here: access tokens must not be printed.
response=$(curl --fail-with-body --silent --show-error --max-time 180 \
  --retry 3 --retry-delay 10 --retry-all-errors \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H 'Content-Type: application/json' \
  -d '{"class_method":"health","input":{}}' "$QUERY_URL")
printf '%s' "$response" | python3 -c '
import json, sys
result = json.load(sys.stdin)
assert result.get("output", {}).get("status") == "ok", "Unexpected health response"
print("Deployed Agent Runtime health check passed")
'
