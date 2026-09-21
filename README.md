# Prototype — Gemini 영상 분석 서버

GitHub Actions → Docker → Artifact Registry → Terraform → **Agent Runtime**.
프로젝트는 `aitech-good-s27493`, 런타임 리전은 `us-central1`입니다.

Google의 [Terraform 에이전트 배포 가이드](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-terraform?hl=ko)와
[컨테이너 런타임 계약](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract)을 따릅니다.
날씨 ADK 데모 대신 FastAPI + Google Gen AI SDK로 영상 분석을 구현했습니다.
ADK 세션/메모리/Playground 채팅은 포함하지 않습니다.

## 처음 배포하기

1. [GitHub Actions Secrets](https://github.com/Lorem-Ipsum-AI-Builder-Cup-2026/prototype/settings/secrets/actions)에
   **`GCP_SA_KEY`**를 만들고 발급한 서비스 계정 JSON 파일의 **내용 전체**를 넣습니다.
   JSON 파일은 레포에 커밋하지 않습니다.
2. GCP 프로젝트의 결제가 활성화되어 있어야 하며, 해당 서비스 계정에 아래 권한이 있어야 합니다.
3. [Actions → Deploy Agent Runtime](https://github.com/Lorem-Ipsum-AI-Builder-Cup-2026/prototype/actions/workflows/deploy.yml)에서
   **Run workflow → main**을 실행합니다. 이후에는 `main` push마다 자동 배포됩니다.
4. 성공한 실행의 Summary에서 `query_url`, `custom_api_base_url`, 에이전트 리소스 이름을 확인합니다.

키가 없으면 CI는 실행되고 배포는 `Deployment pending`으로 건너뜁니다.
키를 등록하는 것만으로는 실행이 시작되지 않으므로 **Run workflow**가 필요합니다.
이 구성은 GCS Terraform backend를 사용하므로 HCP Terraform 로그인/토큰은 필요하지 않습니다.

### 배포 서비스 계정 권한

이 파이프라인은 기존 서비스 계정으로 인프라도 생성하므로 **대상 프로젝트에서** 다음 역할이 필요합니다.
이미 동등한 권한이 있다면 중복 부여하지 않아도 됩니다.

| 역할 | 용도 |
| --- | --- |
| `roles/serviceusage.serviceUsageAdmin` | 필요한 API 활성화, 서비스 에이전트 생성 |
| `roles/resourcemanager.projectIamAdmin` | 실행 계정에 Vertex AI 사용 권한 설정 |
| `roles/iam.serviceAccountAdmin` | 실행 전용 서비스 계정 생성/조회 |
| `roles/iam.serviceAccountUser` | 실행 계정을 Agent Runtime에 연결 |
| `roles/artifactregistry.admin` | 이미지 저장소 생성, 이미지 push, reader IAM 설정 |
| `roles/storage.admin` | 비공개 영상/상태 버킷 생성, 상태 읽기/쓰기, 버킷 IAM 설정 |
| `roles/aiplatform.user` | Agent Runtime 생성/수정/조회 및 배포 후 호출 |
| `roles/browser` | 프로젝트 번호 등 프로젝트 메타데이터 조회 |

배포용 계정과 실제 서버 실행 계정은 분리됩니다. 서버는 `prototype-agent-runtime` 계정으로
Gemini 사용과 전용 영상 버킷의 객체 생성/읽기만 수행합니다. 런타임에 배포 키를 주입하지 않습니다.
장기 운영 시에는 인프라 관리 배포자와 앱 배포자를 분리해 배포자의 프로젝트 관리 권한을 줄이세요.

### 선택 GitHub Variables

| 이름 | 기본값 |
| --- | --- |
| `GCP_PROJECT_ID` | `aitech-good-s27493` |
| `GCP_REGION` | `us-central1` |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` |
| `GEMINI_MODEL_REGION` | `global` |

첫 배포 후 프로젝트/리전을 변경하면 기존 리소스 이동 또는 재생성이 필요할 수 있습니다.

## 배포 과정

- PR: Python lint/format/tests, Terraform fmt/validate, shellcheck, Docker 빌드와 컨테이너 헬스체크.
- `main` push 또는 수동 실행: 같은 CI 통과 → 상태 버킷 준비 → 기반 인프라 apply →
  SHA 태그 이미지 push → **image digest**로 런타임 apply → 원격 헬스체크.
- Terraform provider와 Python 의존성은 lockfile로 고정하고 GitHub Actions는 commit SHA로 고정합니다.
- Terraform 상태는 `${PROJECT_ID}-prototype-tfstate`의 `prototype/foundation`, `prototype/agent`에
  별도로 저장합니다. 버전 관리와 GCS 상태 잠금이 적용되며 apply 실행은 서로 겹치지 않습니다.
- Docker context는 앱 코드와 의존성 파일만 허용해 인증 JSON이 이미지에 들어가지 않도록 합니다.
- 배포 이미지는 단일 `linux/amd64` manifest로 push하고, 저장소에서 동일 digest를 내려받아 실행을 검증합니다.
  Agent Runtime에서 시작 실패를 일으킨 attestation index를 피하기 위해 build provenance는 비활성화합니다.
- Artifact Registry: `prototype-agents`, 영상 버킷: `${PROJECT_ID}-prototype-videos`.
- 런타임은 최소 0개, 최대 2개 인스턴스, 인스턴스당 CPU 1개/메모리 1 GiB입니다.
  유휴 시 scale-to-zero하며 첫 요청에는 콜드 스타트가 있을 수 있습니다.
- 영상과 이미지는 자동 삭제하지 않습니다. 보존 기간이 정해지면 lifecycle 정책을 추가하세요.

## 앱 연동

```text
앱 → 인증된 앱 백엔드 → 업로드 세션 발급
앱 → Cloud Storage 직접 PUT
앱 → 앱 백엔드 → Agent Runtime → Gemini → 분석 결과 → 앱
```

Agent Runtime 엔드포인트는 **Google IAM 인증이 필요한 서버 간 API**입니다.
모바일/웹 앱에 서비스 계정 JSON을 넣지 마세요. 앱 백엔드가 사용자 인증과 영상 소유권을 확인하고
Google OAuth 액세스 토큰으로 이 서버를 호출해야 합니다. 현재 레포는 이 서버와 CI/CD를 제공하며,
앱별 로그인/소유권 DB/공개 API 게이트웨이는 아직 포함하지 않습니다.
업로드 URL은 해당 객체에 쓸 수 있는 권한이므로 해당 사용자에게만 전달하고 로그에 남기지 마세요.

배포 Summary의 `custom_api_base_url`을 `BASE_URL`에 설정합니다.
개발용으로는 이 프로젝트 접근 권한이 있는 계정으로 `gcloud auth login` 후 실행합니다.

```bash
export BASE_URL='배포 Summary의 custom_api_base_url'
export TOKEN="$(gcloud auth print-access-token)"

# 1. 실제 영상 크기(바이트)를 지정하여 업로드 URL을 받습니다.
curl --fail-with-body -X POST "$BASE_URL/v1/uploads" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mime_type":"video/mp4","size_bytes":123456}'

# 2. 응답의 upload_url로 영상 원본을 직접 전송합니다.
# 이 요청에는 Google OAuth 토큰을 추가할 필요가 없습니다.
curl --fail-with-body -X PUT '응답의 upload_url' \
  -H 'Content-Type: video/mp4' --data-binary @video.mp4

# 3. 응답의 video_uri로 분석합니다. 완료될 때까지 HTTP 연결을 유지합니다.
curl --fail-with-body --max-time 300 -X POST "$BASE_URL/v1/analyze" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"video_uri":"gs://aitech-good-s27493-prototype-videos/uploads/응답의ID","mime_type":"video/mp4","prompt":"영상의 주요 사건과 시간대를 한국어로 정리해줘"}'
```

응답: `{"video_uri":"gs://...", "analysis":"분석 결과", "model":"gemini-3.1-flash-lite"}`.
`query_url`에서도 `{"class_method":"analyze_video","input":{"video_uri":"gs://..."}}`로 호출할 수 있습니다.
`stream_query`는 런타임 NDJSON 계약을 제공하며 현재 분석 완료 후 결과 한 개를 반환합니다.

샘플은 MP4/WebM/QuickTime/MPEG, 최대 **100 MiB**, 약 **250초**의 동기 분석 제한을 둡니다.
브라우저 직접 업로드를 쓰려면 실제 앱 origin에 맞는 버킷 CORS 설정이 추가로 필요합니다.
긴 영상/백그라운드 처리에는 작업 큐, 상태 저장소, 결과 폴링 또는 푸시 알림을 추가해야 합니다.
배포 헬스체크는 서버 동작을 확인하며 실제 Gemini 모델 권한/영상 분석 성공 여부는 별도 영상 요청으로 검증합니다.

## 로컬 개발

```bash
uv sync --frozen
uv run ruff check .
uv run pytest
export APP_PROJECT_ID=aitech-good-s27493
export VIDEO_BUCKET=aitech-good-s27493-prototype-videos
export MODEL=gemini-3.1-flash-lite
export MODEL_REGION=global
gcloud auth application-default login
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

헬스체크와 테스트는 실제 GCP/Gemini 호출이나 키 없이 실행됩니다.
로컬 `/v1/*` API에는 별도 인증이 없으므로 개발 서버는 `127.0.0.1`에서만 사용합니다.
운영에서는 Agent Runtime의 IAM 프런트엔드를 통해 호출합니다.

## 장애 대응 / 롤백

- Secret 누락: `GCP_SA_KEY` 등록 후 워크플로 수동 실행.
- `403` 또는 `PERMISSION_DENIED`: 실패 단계에 해당하는 위 IAM 역할 및 조직 정책 확인.
- `SERVICE_DISABLED` / 결제 오류: 대상 프로젝트 결제 및 API 활성화 권한 확인.
- `IMAGE_PULL_BACKOFF`: Terraform foundation의 runtime image reader IAM 적용 확인.
- 지역/모델 오류: 프로젝트의 Agent Runtime 지원 리전과 모델 이용 가능 여부를 확인한 후 Variables 수정.
- Terraform 잠금: 실행 중인 배포가 끝날 때까지 기다립니다. 실행 중 강제 unlock은 하지 않습니다.
- 기존에 같은 이름의 리소스가 있다면 삭제하지 말고 해당 foundation 주소로 `terraform import`합니다.
- 코드 롤백: 정상 커밋으로 되돌리는 revert 커밋을 `main`에 push하면 새 이미지와 revision이 배포됩니다.
  인프라 변경이 포함된 커밋은 Terraform plan의 삭제/교체 항목을 먼저 검토하세요.

영상 버킷과 이미지 저장소는 `prevent_destroy`로 보호합니다. 상태 버킷은 bootstrap 스크립트가
별도로 생성하며 Terraform destroy 대상이 아닙니다. 비용 정리를 위해 삭제가 필요하면
보관할 데이터와 상태를 먼저 확인한 뒤 명시적으로 삭제하세요.
