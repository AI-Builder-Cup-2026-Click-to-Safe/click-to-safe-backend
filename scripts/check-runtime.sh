#!/usr/bin/env bash
# Report what is deployed and whether it answers.
#
#   ./scripts/check-runtime.sh
#
# Prints the deployed engine, its image and class methods, then calls health.
# Requires gcloud credentials with access to the project.
set -euo pipefail

PROJECT_ID=${PROJECT_ID:-aitech-good-s27493}
REGION=${REGION:-us-central1}
BASE="https://${REGION}-aiplatform.googleapis.com/v1"
TOKEN=$(gcloud auth print-access-token)

echo "Account : $(gcloud config get-value account 2>/dev/null)"
echo "Project : ${PROJECT_ID}"
echo

ENGINES=$(curl --fail-with-body --silent --max-time 60 \
  -H "Authorization: Bearer ${TOKEN}" \
  "${BASE}/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines")

ENGINE_ID=$(ENGINES="$ENGINES" python3 -c '
import json, os, sys
items = json.loads(os.environ["ENGINES"]).get("reasoningEngines", [])
if not items:
    sys.exit("No reasoning engine deployed in this project and region.")
for e in items:
    spec = e["spec"]
    show = [
        ("Engine  ", e["name"]),
        ("Display ", e["displayName"]),
        ("Updated ", e.get("updateTime", "?")),
        ("Image   ", spec["containerSpec"]["imageUri"]),
        ("Methods ", ", ".join(m["name"] for m in spec["classMethods"])),
        ("Identity", spec.get("serviceAccount", "?")),
    ]
    for label, value in show:
        print(label + " : " + str(value), file=sys.stderr)
print(items[0]["name"].rsplit("/", 1)[-1])')

echo
echo "Calling health ..."
RE="projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines/${ENGINE_ID}"
# A cold runtime scales to zero, so the first call can take a while to answer.
curl --fail-with-body --silent --max-time 180 \
  -X POST "${BASE}/${RE}:query" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"class_method":"health","input":{}}' | python3 -m json.tool

echo
echo "Deployed and answering. Run ./scripts/try-violations.sh <video.mp4> to review a clip."
