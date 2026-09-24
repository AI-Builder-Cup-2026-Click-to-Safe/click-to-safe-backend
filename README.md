# Prototype — Gemini video analysis server

GitHub Actions → Docker → Artifact Registry → Terraform → **Agent Runtime**.
The target project is configured per repository and the runtime region is `us-central1`.

This follows Google's [Terraform agent deployment guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-terraform)
and the [container runtime contract](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract).
Instead of the weather ADK demo, video analysis is implemented with FastAPI and the
Google Gen AI SDK. ADK sessions, memory, and Playground chat are not included.

## First deployment

1. Create **`GCP_SA_KEY`** in
   [GitHub Actions Secrets](https://github.com/Lorem-Ipsum-AI-Builder-Cup-2026/prototype/settings/secrets/actions)
   and paste the **entire contents** of the service-account JSON file.
   Never commit the JSON file to the repository.
   Add **`PUBLIC_API_TOKEN`** in the same place: it is the shared token that
   guards the public Cloud Run endpoint, and deployment fails without it.
2. Billing must be enabled on the GCP project, and the service account needs the roles below.
3. Run **Run workflow → main** from
   [Actions → Deploy Agent Runtime](https://github.com/Lorem-Ipsum-AI-Builder-Cup-2026/prototype/actions/workflows/deploy.yml).
   After that, every push to `main` deploys automatically.
4. Read `query_url`, `custom_api_base_url`, and the agent resource name from the
   successful run's Summary.

Without the key, CI still runs and deployment is skipped as `Deployment pending`.
Adding the key alone does not start a run, so **Run workflow** is required.
This setup uses a GCS Terraform backend, so no HCP Terraform login or token is needed.

### Deployment service-account roles

This pipeline also creates infrastructure with an existing service account, so the
following roles are required **on the target project**. You do not need to grant a role
again if an equivalent permission is already in place.

| Role | Purpose |
| --- | --- |
| `roles/serviceusage.serviceUsageAdmin` | Enable required APIs, create service agents |
| `roles/resourcemanager.projectIamAdmin` | Grant Vertex AI access to the runtime account |
| `roles/iam.serviceAccountAdmin` | Create and read the dedicated runtime service account |
| `roles/iam.serviceAccountUser` | Attach the runtime account to Agent Runtime |
| `roles/artifactregistry.admin` | Create the image repository, push images, set reader IAM |
| `roles/storage.admin` | Create the private video/state buckets, read/write state, set bucket IAM |
| `roles/aiplatform.user` | Create, update, read, and call Agent Runtime after deployment |
| `roles/browser` | Read project metadata such as the project number |

The deployment account and the account the server actually runs as are separate. The
server runs as `prototype-agent-runtime` and may only use Gemini and create/read objects
in its dedicated video bucket. No deployment key is injected into the runtime. For
long-term operation, separate the infrastructure deployer from the application deployer
to reduce the deployer's project-administration rights.

### Optional GitHub Variables

| Name | Default |
| --- | --- |
| `GCP_PROJECT_ID` | the pipeline's built-in project |
| `GCP_REGION` | `us-central1` |
| `GEMINI_MODEL` | `gemini-3.8-flash` |
| `GEMINI_VISION_MODEL` | `gemini-3.8-flash` |
| `GEMINI_MODEL_REGION` | `global` |
| `LAW_CORPUS` | unset; deploys without statute retrieval |

Changing the project or region after the first deployment may require moving or
recreating existing resources.

## Deployment pipeline

- PR: Python lint/format/tests, Terraform fmt/validate, shellcheck, Docker build and a
  container health check.
- Push to `main` or a manual run: the same CI passes → prepare the state bucket → apply
  the foundation infrastructure → push a SHA-tagged image → apply the runtime by
  **image digest** → remote health check.
- Terraform providers and Python dependencies are pinned by lockfile, and GitHub Actions
  are pinned by commit SHA.
- Terraform state is stored separately under `prototype/foundation` and `prototype/agent`
  in `${PROJECT_ID}-prototype-tfstate`. Versioning and GCS state locking are enabled, and
  apply runs never overlap.
- The Docker context allows only application code and dependency files, so credential
  JSON cannot end up in the image.
- The release image is pushed as a single `linux/amd64` manifest, and the same digest is
  pulled back from the repository to verify that it runs. Build provenance is disabled to
  avoid the attestation index that caused Agent Runtime startup failures.
- Artifact Registry: `prototype-agents`. Video bucket: `${PROJECT_ID}-prototype-videos`.
- The runtime runs between 0 and 2 instances with 1 CPU and 1 GiB of memory each. It
  scales to zero when idle, so the first request may hit a cold start.
- Videos and images are never deleted automatically. Add a lifecycle policy once a
  retention period is decided.

## Public API on Cloud Run

The same image is also served from Cloud Run at a public URL, where a fixed token
takes the place of Google IAM. Terraform stores that token in Secret Manager and
mounts it as `API_TOKEN`; `PUBLIC_API=true` is what installs the check, so the
Agent Runtime deployment of the identical image stays on IAM.

Every path except `/healthz` requires the token, which Cloud Run's probes need
before any caller authenticates:

```
Authorization: Bearer <PUBLIC_API_TOKEN>
```

A missing or wrong token returns `401`. If the deployment ever lands without the
token configured, the service returns `503` for everything rather than serving
the API unauthenticated.

Read the URL from the deploy run's Summary (`public_api_url`), then run the whole
flow — upload and review — with no gcloud credentials:

```bash
export PUBLIC_API_URL=https://...run.app
export PUBLIC_API_TOKEN=...
./scripts/public-review.sh dashcam.mp4
```

Or call it directly. The upload slot is issued by the API itself, and the PUT that
follows goes straight to Cloud Storage, which keeps a 100 MiB video clear of Cloud
Run's 32 MiB request limit:

```bash
# 1. Ask for an upload slot, passing the real byte size.
curl -X POST "$PUBLIC_API_URL/v1/uploads" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mime_type":"video/mp4","size_bytes":12345678}'

# 2. PUT the file to the returned upload_url. This request carries no token.
curl -X PUT "$UPLOAD_URL" -H 'Content-Type: video/mp4' --upload-file dashcam.mp4

# 3. Review the video_uri from step 1. Keep the connection open; a review runs
#    three agents and takes minutes.
curl -X POST "$PUBLIC_API_URL/v1/review" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"video_uri":"gs://.../uploads/...","mime_type":"video/mp4"}'
```

To rotate the token, update the `PUBLIC_API_TOKEN` secret and re-run the deploy:
Terraform adds a new Secret Manager version and the revision picks up `latest`.

## Application integration

```text
app → authenticated app backend → issue upload session
app → direct PUT to Cloud Storage
app → app backend → Agent Runtime → Gemini → analysis result → app
```

The Agent Runtime endpoint is a **server-to-server API that requires Google IAM
authentication**. Never ship service-account JSON inside a mobile or web app. The app
backend must verify user identity and video ownership, then call this server with a
Google OAuth access token. This repository provides the server and its CI/CD; per-app
login, an ownership database, and a public API gateway are not included yet. An upload
URL grants write access to that object, so hand it only to the intended user and keep it
out of logs.

Set `BASE_URL` to `custom_api_base_url` from the deployment Summary. For development, run
`gcloud auth login` with an account that has access to this project.

```bash
export BASE_URL='custom_api_base_url from the deployment Summary'
export TOKEN="$(gcloud auth print-access-token)"

# 1. Request an upload URL, passing the real video size in bytes.
curl --fail-with-body -X POST "$BASE_URL/v1/uploads" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mime_type":"video/mp4","size_bytes":123456}'

# 2. Send the raw video straight to the upload_url from the response.
# This request does not need a Google OAuth token.
curl --fail-with-body -X PUT 'upload_url from the response' \
  -H 'Content-Type: video/mp4' --data-binary @video.mp4

# 3. Analyze using the video_uri from the response. Keep the HTTP connection open
#    until it finishes.
curl --fail-with-body --max-time 300 -X POST "$BASE_URL/v1/analyze" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"video_uri":"gs://PROJECT_ID-prototype-videos/uploads/ID-from-response","mime_type":"video/mp4","prompt":"Summarize the main events in this video with timestamps"}'
```

### Checking what is deployed

`scripts/check-runtime.sh` prints the deployed engine, the exact image digest it runs,
its registered class methods and its runtime identity, then calls `health`.

```bash
./scripts/check-runtime.sh                 # uses the active gcloud project
PROJECT_ID=my-project ./scripts/check-runtime.sh   # or name one explicitly
```

```text
Engine   : projects/.../locations/us-central1/reasoningEngines/891188808928722944
Display  : prototype-video-agent
Updated  : 2026-09-23T07:53:29Z
Image    : us-central1-docker.pkg.dev/.../video-agent@sha256:b796c7c7...
Methods  : health, query, analyze_video, review_violations, stream_query
Identity : prototype-agent-runtime@PROJECT_ID.iam.gserviceaccount.com

Calling health ...
{"output": {"status": "ok"}}
```

`Updated` and `Image` tell you whether the running revision is the one you just pushed;
compare the digest with the one in the deployment Summary. A `health` response of `ok`
only proves the container serves traffic — it does not exercise Gemini or Cloud Storage,
so confirm those with an actual video request.

The endpoint requires Google IAM credentials that have `roles/aiplatform.user` on the
project. `gcloud auth login` works only if your own account is granted that role;
otherwise authenticate with a service-account key:

```bash
gcloud auth activate-service-account --key-file=KEY.json --project=PROJECT_ID
```

### Traffic violation review (video → JSON)

`/v1/violations` reviews dashcam footage for violations committed by **other vehicles**
and returns a fixed JSON shape. Decoding is constrained by `response_schema`, so the
response cannot fail to parse. Model-written fields (`description`, `evidence`,
`summary`) are always English, regardless of the language spoken in the video.

```bash
curl --fail-with-body --max-time 300 -X POST "$BASE_URL/v1/violations" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"video_uri":"gs://.../uploads/ID","min_confidence":0.5}'
```

| Field | Meaning |
| --- | --- |
| `min_confidence` | Findings below this certainty are dropped before the response is built (default `0.5`) |
| `focus` | Optional free text to direct attention toward a particular violation |

An example response. `violations` is ordered by when each one occurs, and is an empty
array when nothing is found.

```json
{
  "video_uri": "gs://.../uploads/ID",
  "model": "gemini-3.1-flash-lite",
  "violation_count": 1,
  "violations": [
    {
      "violation_type": "shoulder_driving",
      "description": "A white vehicle drives on the shoulder to bypass congestion.",
      "start_seconds": 18.5,
      "end_seconds": 23.5,
      "vehicle": "white sedan",
      "confidence": 0.95,
      "evidence": "The vehicle moves outside the lane markings to overtake other cars."
    }
  ],
  "summary": "During congestion, a white vehicle was observed driving on the shoulder.",
  "filtered_out": 0
}
```

`violation_type` uses a closed set of values so findings can be aggregated:
`red_light_running`, `illegal_lane_change`, `shoulder_driving`, `signal_violation`,
`illegal_u_turn`, `tailgating`, `speeding`, `center_line_crossing`,
`illegal_parking`, `failure_to_yield`, `other`.

From Agent Runtime, `{"class_method":"review_violations","input":{...}}` returns the same
result.

**This output is an automated review aid, not a legal determination.** `confidence` is
only the model's visual certainty, so a person must review the cited segment before the
result is used for any report or penalty. The model is instructed never to guess license
plates, and `vehicle` holds only a description of appearance.

A helper script runs upload through result in one step.

```bash
./scripts/try-violations.sh video.mp4              # call the deployed runtime
TARGET=local ./scripts/try-violations.sh video.mp4 # call the local server on :8080
```

Response: `{"video_uri":"gs://...", "analysis":"analysis result", "model":"gemini-3.1-flash-lite"}`.
You can also call `query_url` with `{"class_method":"analyze_video","input":{"video_uri":"gs://..."}}`.
`stream_query` provides the runtime NDJSON contract and currently returns a single result
once analysis completes.

The sample limits input to MP4/WebM/QuickTime/MPEG, at most **100 MiB**, and roughly
**250 seconds** of synchronous analysis. Direct browser uploads additionally require
bucket CORS configuration matching the real app origin. Long videos and background
processing need a job queue, a state store, and result polling or push notifications.
The deployment health check verifies that the server runs; actual Gemini model access and
video analysis success are verified by a separate video request.

## Local development

```bash
uv sync --frozen
uv run ruff check .
uv run pytest
export APP_PROJECT_ID="$(gcloud config get-value project)"
export VIDEO_BUCKET="${APP_PROJECT_ID}-prototype-videos"
export MODEL=gemini-3.1-flash-lite
export MODEL_REGION=global
gcloud auth application-default login
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

The health check and the tests run without real GCP/Gemini calls or any key.
The local `/v1/*` API has no authentication of its own, so run the development server on
`127.0.0.1` only. In production, calls go through the Agent Runtime IAM frontend.

## Troubleshooting and rollback

- Missing secret: add `GCP_SA_KEY`, then run the workflow manually.
- `403` or `PERMISSION_DENIED`: check the IAM roles above for the failing step, plus
  organization policies.
- `SERVICE_DISABLED` or billing errors: check target-project billing and API enablement
  permissions.
- `IMAGE_PULL_BACKOFF`: confirm the runtime image reader IAM from Terraform foundation was
  applied.
- Region or model errors: confirm the project's supported Agent Runtime regions and model
  availability, then adjust the Variables.
- Terraform lock: wait for the running deployment to finish. Never force-unlock while a
  run is in progress.
- If a resource with the same name already exists, do not delete it — `terraform import`
  it to the matching foundation address.
- Code rollback: push a revert commit to `main` to deploy a new image and revision.
  For commits that include infrastructure changes, review the delete/replace entries in
  the Terraform plan first.

The video bucket and the image repository are protected by `prevent_destroy`. The state
bucket is created separately by the bootstrap script and is not a Terraform destroy
target. If you need to delete resources for cost cleanup, confirm what data and state
must be kept, then delete explicitly.
