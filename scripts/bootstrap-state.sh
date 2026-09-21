#!/usr/bin/env bash
# Run with an authenticated project administrator, or the authorized CI deployer.
set -euo pipefail
: "${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
: "${GCP_REGION:=us-central1}"
: "${TF_STATE_BUCKET:=${GCP_PROJECT_ID}-prototype-tfstate}"

# Terraform reads project metadata during plan, before its API resources can be applied.
gcloud services enable serviceusage.googleapis.com storage.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --project="$GCP_PROJECT_ID" --quiet

# A list error must fail instead of being mistaken for an absent bucket.
existing=$(gcloud storage buckets list --project="$GCP_PROJECT_ID" \
  --filter="name=${TF_STATE_BUCKET}" --format='value(name)')
if [[ -z "$existing" ]]; then
  gcloud storage buckets create "gs://${TF_STATE_BUCKET}" \
    --project="$GCP_PROJECT_ID" --location="$GCP_REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets update "gs://${TF_STATE_BUCKET}" \
  --versioning --uniform-bucket-level-access --public-access-prevention
