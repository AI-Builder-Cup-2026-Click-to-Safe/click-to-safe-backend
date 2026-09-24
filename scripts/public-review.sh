#!/usr/bin/env bash
# Upload a dashcam clip through the public API and print the verdict as JSON.
#
#   PUBLIC_API_URL=https://... PUBLIC_API_TOKEN=... ./scripts/public-review.sh clip.mp4
#
# Nothing here needs gcloud: the API issues the upload capability itself, which
# is the point of the public surface. Never enable shell tracing: the token and
# the upload URL are both credentials.
set -euo pipefail

VIDEO=${1:?usage: public-review.sh <video.mp4>}
: "${PUBLIC_API_URL:?Set PUBLIC_API_URL to the Cloud Run URL}"
: "${PUBLIC_API_TOKEN:?Set PUBLIC_API_TOKEN to the shared token}"
[[ -f "$VIDEO" ]] || { echo "No such file: $VIDEO" >&2; exit 1; }

SIZE=$(wc -c < "$VIDEO" | tr -d ' ')
echo "Requesting an upload slot for ${SIZE} bytes ..." >&2

SLOT=$(curl --fail-with-body --silent --show-error --max-time 120 \
  -X POST "${PUBLIC_API_URL}/v1/uploads" \
  -H "Authorization: Bearer ${PUBLIC_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"mime_type\":\"video/mp4\",\"size_bytes\":${SIZE}}")

UPLOAD_URL=$(SLOT="$SLOT" python3 -c 'import json,os; print(json.loads(os.environ["SLOT"])["upload_url"])')
VIDEO_URI=$(SLOT="$SLOT" python3 -c 'import json,os; print(json.loads(os.environ["SLOT"])["video_uri"])')

echo "Uploading ${VIDEO} ..." >&2
curl --fail-with-body --silent --show-error --max-time 900 \
  -X PUT "$UPLOAD_URL" -H 'Content-Type: video/mp4' --upload-file "$VIDEO" >/dev/null

echo "Reviewing ${VIDEO_URI} ..." >&2
curl --fail-with-body --silent --show-error --max-time 1800 \
  -X POST "${PUBLIC_API_URL}/v1/review" \
  -H "Authorization: Bearer ${PUBLIC_API_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"video_uri\":\"${VIDEO_URI}\",\"mime_type\":\"video/mp4\"}" \
  | python3 -m json.tool
