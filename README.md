# Click-to-Safe — backend

Dashcam clip in, a citation-backed traffic-violation report out.
**Three Gemini agents review the footage; the Judge's whole job is to say no.**

Built end-to-end on Google Cloud, serverless at every tier.

## How a review runs

![Architecture: the app uploads a clip, three Gemini agents on Agent Runtime
review it against the statute via RAG Engine, and the driver reviews the draft
before sending it to the authority](docs/architecture.png)

The loop runs **at most 3 rounds** (`MAX_FOLLOW_UP_ROUNDS = 2` plus the first
pass), so the Judge can send a draft back for more evidence but the agents can
never argue forever.

## The stack

![The stack: Gemini 3.8 Flash, Agent Platform, ADK and Agent Runtime for the
agents; RAG Engine, Vector Search, Cloud Storage and Firestore for grounding and
data; Cloud Run, Secret Manager, IAM, Logging and Artifact Registry for the
platform; Python, ffmpeg, Terraform and GitHub Actions as the open-source
layer](docs/stack.png)

## API at a glance

| Endpoint | What it does |
|---|---|
| `POST /v1/uploads` | Hand back a signed URL; the clip goes straight to Cloud Storage |
| `POST /v1/review` | Run the three agents over the clip and return a verdict |
| `GET /v1/reviews` | Page through finished reviews |
| `GET /v1/reviews/{id}` | One review, with signed playback URLs |
| `PATCH /v1/reviews/{id}/reporting` | Mark a case sent |
| `GET /v1/impact` | Counters for the impact screen |

Video never passes through the API: the app `PUT`s it straight to Cloud Storage
with a signed URL, and only the `gs://` URI is sent for review. Playback URLs
are signed through IAM, so the bucket is never public.

Full reference: [Public API on Cloud Run](#public-api-on-cloud-run).

---

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

## Public API on Cloud Run

The same image is also served from Cloud Run at a public URL, where a fixed token
takes the place of Google IAM. Terraform stores that token in Secret Manager and
mounts it as `API_TOKEN`; `PUBLIC_API=true` is what installs the check, so the
Agent Runtime deployment of the identical image stays on IAM.

Read the base URL from the deploy run's Summary (`public_api_url`). It looks like
`https://prototype-video-api-<hash>-uc.a.run.app`.

### Authentication

Every request carries the shared token, in either form:

```
Authorization: Bearer <PUBLIC_API_TOKEN>
Authorization: <PUBLIC_API_TOKEN>
```

| Situation | Response |
| --- | --- |
| Valid token | the endpoint runs |
| Missing or wrong token | `401` with `WWW-Authenticate: Bearer` |
| Deployed without a token configured | `503` on every path |

The `503` is deliberate: an unset token is a deployment fault, and falling open
would publish the API to anyone. Only `/health` and `/healthz` skip the check,
because Cloud Run probes them before any caller authenticates.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness. No token required. |
| `POST` | `/v1/uploads` | Issue a one-off Cloud Storage upload URL. |
| `POST` | `/v1/review` | Full agent workflow: analyst → lawyer → judge. |
| `POST` | `/v1/violations` | Single-pass violation list, no legal grounding. |
| `POST` | `/v1/analyze` | Free-form description of a clip. |
| `GET` | `/v1/reviews` | Stored reviews, newest first. |
| `GET` | `/v1/reviews/{id}` | One stored review, with its evidence. |
| `DELETE` | `/v1/reviews/{id}` | Remove a stored review. |

Use `/health` rather than `/healthz` from outside: on a Cloud Run domain Google's
front end answers `/healthz` itself and never forwards it to the container.

### The upload-then-review flow

A video never passes through Cloud Run. The API issues an upload URL, the client
PUTs the file straight to Cloud Storage, and the review is asked for by reference.
That keeps a large video clear of Cloud Run's 32 MiB request limit.

```text
client → POST /v1/uploads → upload_url + video_uri
client → PUT upload_url → Cloud Storage
client → POST /v1/review { video_uri } → verdict JSON
```

The script does all three steps, and needs no gcloud credentials:

```bash
export PUBLIC_API_URL=https://prototype-video-api-<hash>-uc.a.run.app
export PUBLIC_API_TOKEN=...
./scripts/public-review.sh dashcam.mp4
```

#### Estimated AI cost

Completed `/v1/review` responses and `/v1/reviews` list and detail records include
`estimated_ai_cost_usd` (number or `null`). It sums Gemini token usage across all
video windows, agent/tool turns and vehicle tracking calls. It is a USD estimate
for the configured Gemini 3.8 Flash model, including cached-input pricing and
reasoning tokens. Standard Vertex global/non-global rates and the introductory
rate through December 2026 are applied; compute, storage, retrieval and
account-specific discounts are excluded. See `app/costs.py` for the rate source.
Unknown models or incomplete usage return `null`, never a misleading partial
amount. Older records are not backfilled. No extra model calls are made to
calculate cost.

#### 1. Request an upload slot

`size_bytes` must be the real byte count, and at most 500 MiB. The clip itself must
be thirty minutes or shorter, which `/v1/review` checks once it can read the header.
`mime_type` is one
of `video/mp4`, `video/webm`, `video/quicktime`, `video/mpeg`.

```bash
curl -X POST "$PUBLIC_API_URL/v1/uploads" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"mime_type":"video/mp4","size_bytes":6933100}'
```

```json
{
  "upload_url": "https://storage.googleapis.com/upload/...",
  "video_uri": "gs://<bucket>/uploads/53cb4d01-...",
  "method": "PUT",
  "mime_type": "video/mp4"
}
```

#### 2. Upload the file

Straight to Cloud Storage. This request carries no token — the URL is itself the
capability, so treat it as a credential and do not log it.

```bash
curl -X PUT "$UPLOAD_URL" \
  -H 'Content-Type: video/mp4' \
  --upload-file dashcam.mp4
```

The `Content-Type` must match the `mime_type` from step 1, or the review returns
`400`.

#### 3. Review the clip

Pass the `video_uri` from step 1. `focus` is optional and is treated as a hint,
never as an instruction.

```bash
curl -X POST "$PUBLIC_API_URL/v1/review" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"video_uri":"gs://<bucket>/uploads/53cb4d01-...","mime_type":"video/mp4"}'
```

`window_minutes` chooses how much video the analyst reads at a time, from 1 to
30, and defaults to 1. A smaller window reads each stretch more closely at the
cost of more requests and a longer review; a window at least as long as the clip
sends the whole video to the model in a single unchunked request.

```bash
# One request over the whole clip, however long it is.
-d '{"video_uri":"gs://...","window_minutes":30}'
```

| Window | Requests for a 30-minute clip | |
| --- | --- | --- |
| 1 minute (default) | 33 | refused: too many to finish in one request |
| 5 minutes | 7 | |
| 30 minutes | 1, unchunked | |

A combination that cannot finish inside a single request is refused with `422`
before anything is spent, rather than timing out twenty minutes later. The
default window suits clips of a few minutes; a longer video needs a larger one.

The response reports what was actually done under `coverage`, including
`window_seconds`.

This runs three agents over up to three rounds and takes minutes, so keep the
connection open. The app gives up at 15 minutes and answers `504`; Cloud Run
would hold the connection for an hour, so the app's limit is the one that
applies. Set your client's timeout above 15 minutes, not below.

### Review response

```json
{
  "video_uri": "gs://<bucket>/uploads/53cb4d01-...",
  "reportable_count": 1,
  "reportable": [
    {
      "vehicle_id": "white_sedan",
      "appearance": "White modern sedan with continuous full-width rear light bar",
      "conduct": "Squeezed past and overtook the ego vehicle on the right ...",
      "start_seconds": 18.0,
      "end_seconds": 22.5,
      "legal_basis": "Road Transport Act 1987 (Act 333), Section 43(1)",
      "penalty": "A fine of not less than four thousand ringgit ...",
      "reasoning": "Forcing an overtaking manoeuvre on the right ...",
      "confidence": 0.95
    }
  ],
  "verdict": "Report the white sedan for careless and inconsiderate driving ...",
  "rounds_used": 3,
  "follow_up_exhausted": false,
  "outstanding_questions": { "video": [], "lawyer": [] },
  "evidence": {
    "timeline": [{ "start_seconds": 18.0, "end_seconds": 22.5, "event": "...", "vehicle_ids": ["..."], "road_context": "..." }],
    "vehicles": [{ "vehicle_id": "white_sedan", "appearance": "...", "is_ego": false, "manoeuvres": [] }],
    "legal_analysis": { "findings": [], "gaps": [], "summary": "..." }
  }
}
```

`reportable` is what the judge decided is worth reporting; an empty list with
`reportable_count: 0` means nothing was. `evidence` carries the working the
verdict rests on: the analyst's timeline and vehicle list, and the lawyer's cited
provisions, each quoted from the retrieved statute.

`rounds_used` counts the judge's passes. When the judge still wanted more detail
as the rounds ran out, `follow_up_exhausted` is `true` and
`outstanding_questions` says what was missing — the verdict then rests on
incomplete information and deserves a closer look.

### The other two endpoints

Both take a `video_uri` from the same upload step and answer in one model call,
so they return in seconds rather than minutes. Neither consults the statute
corpus, so neither cites law: `/v1/review` is the one that does.

`/v1/violations` returns a flat list against a fixed set of categories.
`min_confidence` drops findings the model is less sure of than the threshold,
and `filtered_out` reports how many that removed:

```bash
curl -X POST "$PUBLIC_API_URL/v1/violations" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"video_uri":"gs://<bucket>/uploads/...","min_confidence":0.5}'
```

```json
{
  "video_uri": "gs://<bucket>/uploads/...",
  "model": "gemini-3.8-flash",
  "violation_count": 1,
  "violations": [],
  "summary": "...",
  "filtered_out": 2
}
```

`/v1/analyze` just describes a clip, with `prompt` steering what to look at:

```bash
curl -X POST "$PUBLIC_API_URL/v1/analyze" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"video_uri":"gs://<bucket>/uploads/...","prompt":"Describe the road layout."}'
```

In all three, text spoken or shown inside a video is treated as content to
describe, never as instructions to follow.

### Review history

Every finished review is stored and answered with a `review_id`. The history
lives in Firestore, which runs no instance of its own, so an idle deployment
costs nothing.

A `/v1/review` response now ends with the id its record was stored under:

```json
{ "reportable_count": 1, "verdict": "...", "review_id": "c6ec7e580b504ebda1264812676519f3" }
```

#### List reviews

`limit` defaults to 20 and is capped at 100.

```bash
curl "$PUBLIC_API_URL/v1/reviews?limit=20" -H "Authorization: Bearer $PUBLIC_API_TOKEN"
```

```json
{
  "reviews": [
    {
      "review_id": "c6ec7e580b504ebda1264812676519f3",
      "created_at": "2026-10-04T12:23:09.250782+00:00",
      "video_uri": "gs://<bucket>/uploads/88cf2460-...",
      "reportable_count": 1,
      "verdict": "Report vehicle_silver_sedan to the authorities under Section 43(1) ...",
      "provisions": ["Road Transport Act 1987 (Act 333), Section 43(1)"],
      "duration_seconds": 15.1,
      "rounds_used": 3
    }
  ],
  "next_before": null
}
```

| Field | Meaning |
| --- | --- |
| `review_id` | Pass to `/v1/reviews/{id}` for the full record. |
| `original_video_url` | Signed link to the uploaded clip, playable in a browser. |
| `annotated_video_url` | Signed link to the same clip with the boxes drawn on, or `null`. |
| `created_at` | When the review finished. Doubles as the paging cursor. |
| `reportable_count` | Vehicles the judge decided are worth reporting. |
| `verdict` | The decision in one line, truncated at 500 characters. |
| `provisions` | Every provision cited, so a list is scannable without opening records. |
| `duration_seconds` | Length of the clip, absent when the header gave none. |
| `rounds_used` | Judge passes, up to three. |

A listing returns these summaries, never the evidence — that is what makes a
record large, and reading it to draw a list would be paid for on every row.

#### The annotated video

When the judge reports a vehicle, its position is tracked once a second across
the seconds cited and the boxes are rendered onto a copy of the clip. A stored
review carries both videos as signed links, minted per read because they expire
after six hours:

```json
{
  "original_video_url": "https://storage.googleapis.com/...",
  "annotated_video_url": "https://storage.googleapis.com/...",
  "tracks": [
    {
      "vehicleTrackId": "white_mpv",
      "frames": [{ "atSeconds": 10.0, "x": 0.715, "y": 0.367, "width": 0.285, "height": 0.437 }]
    }
  ]
}
```

`annotated_video_url` is `null` when nothing was reportable, or when the render
did not finish — a case keeps its verdict and its `tracks` either way, and a
player can draw those boxes itself. Box coordinates are fractions of the frame,
so they scale to whatever size the video is displayed at.

#### Page through them

Pass the `next_before` value from one page as `before` on the next. It is `null`
on the last page.

```bash
curl "$PUBLIC_API_URL/v1/reviews?limit=20&before=2026-10-04T12:23:09.250782+00:00" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN"
```

Paging on a timestamp rather than an offset keeps the cost flat: Firestore
charges for every document a query walks, and an offset walks the ones it skips.
The cursor may be passed back exactly as it was handed out — a `+` arriving as a
space is repaired, so percent-encoding it is optional.

#### Read one review

Returns the whole record: the verdict, the analyst's timeline and vehicles, and
the lawyer's citations and gaps — the same shape `/v1/review` answered with.

```bash
curl "$PUBLIC_API_URL/v1/reviews/$REVIEW_ID" -H "Authorization: Bearer $PUBLIC_API_TOKEN"
```

#### Delete one

```bash
curl -X DELETE "$PUBLIC_API_URL/v1/reviews/$REVIEW_ID" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN"
```

```json
{ "review_id": "c6ec7e580b504ebda1264812676519f3", "deleted": true }
```

Both the record and its summary go. A review that was already gone answers
`404`, rather than reporting a delete that did nothing.

Stored reviews keep whatever the analyst read, licence plates included. The
history sits behind the same shared token as everything else, but anything
exported from it carries those plates onward.

#### Impact counters

```bash
curl "$PUBLIC_API_URL/v1/impact?period=week" -H "Authorization: Bearer $PUBLIC_API_TOKEN"
```

```json
{
  "snitchedCount": 1,
  "billedCount": 0,
  "mostSpotted": { "plate": "WA 4978 R", "count": 2 },
  "offenceOccurrences": { "queue_cut": 2, "red_light": 1 },
  "isDemo": false,
  "demoToday": "2026-10-08"
}
```

`period` is `all`, `week` or `today`. Each counter means something different:

| Counter | Counts |
| --- | --- |
| `snitchedCount` | Distinct plates in cases a person confirmed and sent. |
| `billedCount` | Fines an authority issued. Always `0`: nothing is wired to one. |
| `mostSpotted` | The plate seen in the most cases, however many were sent. |
| `offenceOccurrences` | Every occurrence, so an offence twice in one case counts twice. |

A case the model produced is not a report. Only a person moves one to `sent`:

```bash
curl -X PATCH "$PUBLIC_API_URL/v1/reviews/$REVIEW_ID/reporting" \
  -H "Authorization: Bearer $PUBLIC_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reporting_status":"sent"}'
```

### Errors

| Code | Meaning |
| --- | --- |
| `400` | The `video_uri` is outside the configured bucket, or its type does not match the upload. |
| `401` | Missing or wrong token. |
| `404` | The upload never finished. |
| `413` | The stored object exceeds 500 MiB or thirty minutes, or is empty. |
| `422` | The request body is malformed — including a `size_bytes` over 500 MiB — or the model returned nothing usable. |
| `502` | The upstream model call failed. |
| `503` | The service is missing its token or bucket configuration, or review history is unreachable. |
| `504` | The review exceeded 15 minutes. |

### Rotating the token

Update the `PUBLIC_API_TOKEN` secret and re-run the deploy. Terraform adds a new
Secret Manager version and the revision picks up `latest`. Callers using the old
token start getting `401` as soon as the new revision serves traffic.

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

The sample limits input to MP4/WebM/QuickTime/MPEG, at most **500 MiB** and **thirty
minutes**. A clip past a minute is read in overlapping one-minute windows and merged
before the lawyer and judge see it, so a review stays synchronous but takes minutes:
windows are read one at a time, because reading several ranges of one object at once
made the model hang. Direct browser uploads additionally require bucket CORS
configuration matching the real app origin. Going beyond thirty minutes needs a job
queue, a state store, and result polling or push notifications.
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
