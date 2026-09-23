#!/usr/bin/env bash
# Upload a local dashcam clip and print the violation report as JSON.
#
#   ./scripts/try-violations.sh path/to/clip.mp4            # deployed runtime
#   TARGET=local ./scripts/try-violations.sh path/to/clip.mp4  # local server on :8080
#
# The deployed runtime is IAM-authenticated, so you need gcloud credentials
# with access to the project. The project comes from $PROJECT_ID, falling back
# to the active gcloud configuration.
set -euo pipefail

VIDEO=${1:?usage: try-violations.sh <video.mp4>}
PROJECT_ID=${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}
[[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || {
  echo "Set PROJECT_ID, or select one with: gcloud config set project PROJECT_ID" >&2
  exit 1
}
REGION=${REGION:-us-central1}
BUCKET=${BUCKET:-${PROJECT_ID}-prototype-videos}
MIN_CONFIDENCE=${MIN_CONFIDENCE:-0.5}
TARGET=${TARGET:-remote}

[[ -f "$VIDEO" ]] || { echo "No such file: $VIDEO" >&2; exit 1; }

OBJECT="uploads/$(date +%s)-$(basename "$VIDEO" | tr -cd '[:alnum:].-')"
VIDEO_URI="gs://${BUCKET}/${OBJECT}"

echo "Uploading to ${VIDEO_URI} ..." >&2
gcloud storage cp "$VIDEO" "$VIDEO_URI" \
  --content-type=video/mp4 --project="$PROJECT_ID" >&2

REQUEST=$(VIDEO_URI="$VIDEO_URI" MIN_CONFIDENCE="$MIN_CONFIDENCE" python3 -c '
import json, os
print(json.dumps({
    "video_uri": os.environ["VIDEO_URI"],
    "mime_type": "video/mp4",
    "min_confidence": float(os.environ["MIN_CONFIDENCE"]),
}))')

if [[ "$TARGET" == "local" ]]; then
  echo "Reviewing via local server ..." >&2
  curl --fail-with-body --silent --max-time 300 \
    -X POST http://127.0.0.1:8080/v1/violations \
    -H 'Content-Type: application/json' -d "$REQUEST"
else
  # Older gcloud releases have no `ai reasoning-engines` group, so list over REST.
  if [[ -z "${ENGINE_ID:-}" ]]; then
    ENGINE_ID=$(curl --silent --max-time 60 \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines" \
      | python3 -c '
import json, sys
engines = json.load(sys.stdin).get("reasoningEngines", [])
print(engines[0]["name"].rsplit("/", 1)[-1] if engines else "")')
  fi
  [[ -n "$ENGINE_ID" ]] || { echo "Set ENGINE_ID to your reasoning engine id." >&2; exit 1; }

  RE="projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines/${ENGINE_ID}"
  echo "Reviewing via ${RE} ..." >&2
  ENVELOPE=$(REQUEST="$REQUEST" python3 -c '
import json, os
print(json.dumps({"class_method": "review_violations",
                  "input": json.loads(os.environ["REQUEST"])}))')

  curl --fail-with-body --silent --max-time 300 \
    -X POST "https://${REGION}-aiplatform.googleapis.com/v1/${RE}:query" \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H 'Content-Type: application/json' -d "$ENVELOPE"
fi | python3 -m json.tool
