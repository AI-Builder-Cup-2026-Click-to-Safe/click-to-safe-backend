#!/usr/bin/env bash
# Prove the public endpoint is reachable, and that the shared token is what
# separates a served request from a rejected one.
#
# Never enable shell tracing here: the token must not reach the build log.
set -euo pipefail
: "${PUBLIC_API_URL:?Set PUBLIC_API_URL}"
: "${PUBLIC_API_TOKEN:?Set PUBLIC_API_TOKEN}"

fail() { echo "$1" >&2; exit 1; }

# A cold Cloud Run instance answers only after it starts, so allow for a retry.
# /health, not /healthz: Google's front end answers the latter itself.
status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 60 \
  --retry 5 --retry-delay 5 --retry-all-errors "${PUBLIC_API_URL}/health")
[[ "$status" == "200" ]] || fail "Health check returned ${status}, expected 200"

status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 60 \
  -X POST "${PUBLIC_API_URL}/v1/review" \
  -H 'Content-Type: application/json' -d '{"video_uri":"gs://absent/uploads/none"}')
[[ "$status" == "401" ]] || fail "An unauthenticated review returned ${status}, expected 401"

status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 60 \
  -X POST "${PUBLIC_API_URL}/v1/review" \
  -H 'Authorization: Bearer not-the-token' \
  -H 'Content-Type: application/json' -d '{"video_uri":"gs://absent/uploads/none"}')
[[ "$status" == "401" ]] || fail "A wrong token returned ${status}, expected 401"

# 422 means the request passed authentication and was rejected on its body,
# which is exactly what proves the token was accepted.
status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 60 \
  -X POST "${PUBLIC_API_URL}/v1/review" \
  -H "Authorization: Bearer ${PUBLIC_API_TOKEN}" \
  -H 'Content-Type: application/json' -d '{}')
[[ "$status" == "422" ]] || fail "An authenticated request returned ${status}, expected 422"

echo "Public API smoke test passed: health served, bad tokens rejected, good token accepted"
