<div align="center">

# Click-to-Safe

**Get dangerous drivers off the road.**
Your dashcam saw it. Gemini writes the report. You hit Send.

[![Tests](https://img.shields.io/badge/tests-198%20passing-1e8e3e)](tests)
[![Gemini](https://img.shields.io/badge/Gemini-3.8%20Flash-1a73e8)](https://cloud.google.com/vertex-ai)
[![Cost](https://img.shields.io/badge/cost-~$0.06%20per%20report-f9ab00)](#why-these-choices)
[![Benchmark](https://img.shields.io/badge/myDashCam-69%25%20caught-d93025)](#benchmark)

</div>

---

Roads kill **1.16 million people a year — one every 27 seconds** (WHO, 2026).
Dashcams catch dangerous drivers every day, and then nothing happens: reporting
one means scrubbing footage, naming the offence, citing the law and typing it
all up. So the clip dies on a memory card.

**The evidence exists. The report doesn't.**

Click-to-Safe turns a dashcam clip into a complete, law-cited report in one tap.

## How it works

![Architecture: the app uploads a clip, three Gemini agents on Agent Runtime
review it against the statute via RAG Engine, and the driver reviews the draft
before sending it to the authority](docs/architecture.png)

Three agents, built with ADK, deliberate over the footage:

| | Agent | Job |
|---|---|---|
| **1** | **Analyst** | Watches the clip. Finds the offence, reads the plate, timestamps it, draws the boxes. |
| **2** | **Lawyer** | Cites the matching section of the Road Transport Act 1987, retrieved via RAG. |
| **3** | **Judge** | Decides what is solid enough to report — and says no when it isn't. |

The Judge can send a draft back for more evidence, but only **twice**
(`MAX_FOLLOW_UP_ROUNDS = 2`), so the agents can never argue forever.

> **Exhibit A — a real 44-second clip.**
> The Lawyer overcharged: reckless driving, s.42(1).
> The Judge said no and made it **s.79(2), ignoring a traffic sign.**
> ≈US$0.06 of AI · 166s end to end.

## Benchmark

Against **myDashCam** (IEEE SPS Malaysia Signal & Image Processing Challenge
2021, 97 labelled violations):

| | Violations caught | |
|---|---|---|
| **Click-to-Safe** | **67** | **69%** |
| Gemini on its own | 51 | 53% |

**16 more violations from the same footage** — before the Lawyer and Judge
decide what is worth reporting. A 30-minute clip takes ~2.5 minutes to analyse.

<sub>Caught = our time span overlaps the labelled violation, matched one-to-one.
At ≥50% overlap: 26 vs 22.</sub>

## Three gates before anyone is fined — two are human

1. **The Judge agent** decides what gets reported. Every cited section must match
   frames in the clip.
2. **You** decide what gets sent. Your name and ID join *after* the AI step —
   Gemini never sees them.
3. **The authority** decides who gets a summons. Click-to-Safe never decides guilt.

## The stack

![The stack: Gemini 3.8 Flash, Agent Platform, ADK and Agent Runtime for the
agents; RAG Engine, Vector Search, Cloud Storage and Firestore for grounding and
data; Cloud Run, Secret Manager, IAM, Logging and Artifact Registry for the
platform; Python, ffmpeg, Terraform and GitHub Actions as the open-source
layer](docs/stack.png)

Built end-to-end on Google Cloud, **serverless at every tier** — nothing is
provisioned or patched, and Cloud Run scales to zero between reviews.

### Why these choices

- **Why Flash, not a bigger model** — it reads video natively, at Flash prices.
  Every review reports its own estimated cost.
- **Why a loop with a hard stop** — LoopAgent lets the Judge send drafts back,
  capped so it cannot argue forever.
- **Why RAG, not fine-tuning** — laws change and differ by country. A new
  country means a new corpus, not a new model.

## API

```http
POST /v1/uploads   → signed URL; the clip goes straight to Cloud Storage
POST /v1/review    → run the three agents, return a verdict
GET  /v1/reviews   → page through finished reviews
GET  /v1/impact    → counters for the impact screen
```

Video never passes through the API: the app `PUT`s it straight to Cloud Storage
with a signed URL, and only the `gs://` URI is sent for review. Playback URLs
are signed through IAM, so the bucket is never public.

**→ [Full API reference](docs/API.md)**

## Deploying it

GitHub Actions → Docker → Artifact Registry → Terraform → **Agent Runtime** and
**Cloud Run**. One image serves both: Agent Runtime for IAM-authenticated backend
callers, and Cloud Run for public callers holding a shared token. The target
project is configured per repository; the runtime region is `us-central1`.

This follows Google's [Terraform agent deployment guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-terraform)
and the [container runtime contract](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract).

## First deployment

1. Create **`GCP_SA_KEY`** in
   [GitHub Actions Secrets](https://github.com/AI-Builder-Cup-2026-Click-to-Safe/click-to-safe-backend/settings/secrets/actions)
   and paste the **entire contents** of the service-account JSON file.
   Never commit the JSON file to the repository.
   Add **`PUBLIC_API_TOKEN`** in the same place: it is the shared token that
   guards the public Cloud Run endpoint, and deployment fails without it.
2. Billing must be enabled on the GCP project, and the service account needs the roles below.
3. Run **Run workflow → main** from
   [Actions → Deploy Agent Runtime](https://github.com/AI-Builder-Cup-2026-Click-to-Safe/click-to-safe-backend/actions/workflows/deploy.yml).
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

Reviews include a signed `thumbnail_url`, also exposed as `case.evidence.thumbnailUrl`.
The server creates a 320-pixel JPEG at the first cited moment (one second for a clean
review), using the annotated video when available and the original otherwise. JPEGs
are reused by source object generation and timestamp; links expire after six hours.
Existing history can be populated with ADC credentials and `APP_PROJECT_ID`:

```bash
uv run python -m app.thumbnail_backfill          # inspect record count, no writes
uv run python -m app.thumbnail_backfill --apply  # create missing images and update URI fields
```

Backfill updates only `thumbnail_uri` in each record and summary, preserving user
edits and reporting/dismissal state. It is safe to rerun; failures report review IDs
and produce a nonzero exit code. Dismissal is a separate boolean, updated with
`PATCH /v1/reviews/{id}/dismissal` and `{"dismissed": true}`; dismissed cases remain
readable in history but do not contribute to Impact aggregates.

- PR: Python lint/format/tests, Terraform fmt/validate, shellcheck, Docker build and a
  container health check.
- Push to `main` or a manual run: the same CI passes → prepare the state bucket → apply
  the foundation infrastructure → push a SHA-tagged image → apply the runtime by
  **image digest** → remote health check → apply the public Cloud Run service from the
  same digest → check that it serves health, rejects a wrong token and accepts the
  right one.
- Terraform providers and Python dependencies are pinned by lockfile, and GitHub Actions
  are pinned by commit SHA.
- Terraform state is stored separately under `prototype/foundation`, `prototype/agent`
  and `prototype/public` in `${PROJECT_ID}-prototype-tfstate`. Versioning and GCS state
  locking are enabled, and apply runs never overlap.
- The Docker context allows only application code and dependency files, so credential
  JSON cannot end up in the image.
- The release image is pushed as a single `linux/amd64` manifest, and the same digest is
  pulled back from the repository to verify that it runs. Build provenance is disabled to
  avoid the attestation index that caused Agent Runtime startup failures.
- Artifact Registry: `prototype-agents`. Video bucket: `${PROJECT_ID}-prototype-videos`.
- The runtime runs between 0 and 2 instances with 1 CPU and 1 GiB of memory each. The
  public Cloud Run service runs between 0 and 4, with 2 CPUs and 2 GiB each and 4
  concurrent requests per instance, since one review occupies an instance for minutes.
  Both scale to zero when idle, so the first request may hit a cold start.
- Videos and images are never deleted automatically. Add a lifecycle policy once a
  retention period is decided.

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
