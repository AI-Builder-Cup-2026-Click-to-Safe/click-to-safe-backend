terraform {
  required_version = ">= 1.5, < 2.0"
  backend "gcs" {}
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.3.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "image_uri" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image_uri))
    error_message = "Deploy an immutable image digest, not a mutable tag."
  }
}
variable "runtime_service_account" { type = string }
variable "video_bucket" { type = string }
variable "model" {
  type    = string
  default = "gemini-3.8-flash"
}
variable "vision_model" {
  type    = string
  default = "gemini-3.8-flash"
}
variable "model_region" {
  type    = string
  default = "global"
}
variable "law_corpus" {
  type        = string
  default     = ""
  description = "Full resource name of the statute RAG corpus, or empty to run without retrieval."
  validation {
    condition     = var.law_corpus == "" || can(regex("^projects/[^/]+/locations/[^/]+/ragCorpora/[0-9]+$", var.law_corpus))
    error_message = "Use a full ragCorpora resource name, or leave it empty."
  }
}
# The shared token every public caller must present. It reaches Terraform from a
# GitHub secret and is held in Secret Manager so the Cloud Run revision does not
# carry it as plain text that anyone with viewer access can read back.
variable "api_token" {
  type      = string
  sensitive = true
  validation {
    condition     = length(var.api_token) >= 16
    error_message = "Use at least 16 characters: this single value is the only thing guarding a public endpoint."
  }
}

resource "google_project_service" "public_apis" {
  for_each = toset([
    "run.googleapis.com", "secretmanager.googleapis.com"
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_secret_manager_secret" "api_token" {
  secret_id = "prototype-public-api-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.public_apis]
}

resource "google_secret_manager_secret_version" "api_token" {
  secret      = google_secret_manager_secret.api_token.id
  secret_data = var.api_token
}

resource "google_secret_manager_secret_iam_member" "runtime_reads_token" {
  secret_id = google_secret_manager_secret.api_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.runtime_service_account}"
}

resource "google_cloud_run_v2_service" "public_api" {
  name     = "prototype-video-api"
  location = var.region
  project  = var.project_id
  # Only Google's front end reaches the container; the token check happens in-app.
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = var.runtime_service_account
    # A review runs three agents over up to three rounds, well past the 5m default.
    timeout = "3600s"
    scaling {
      min_instance_count = 0
      max_instance_count = 4
    }
    containers {
      image = var.image_uri
      ports { container_port = 8080 }
      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        # Release the CPU between requests: a review is spent waiting on Gemini,
        # not computing, so paying for an always-on CPU buys nothing here.
        cpu_idle = true
      }
      env {
        name  = "PUBLIC_API"
        value = "true"
      }
      env {
        name = "API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.api_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name  = "APP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "MODEL"
        value = var.model
      }
      env {
        name  = "VISION_MODEL"
        value = var.vision_model
      }
      env {
        name  = "MODEL_REGION"
        value = var.model_region
      }
      env {
        name  = "VIDEO_BUCKET"
        value = var.video_bucket
      }
      env {
        name  = "LAW_CORPUS"
        value = var.law_corpus
      }
      startup_probe {
        # /healthz is answered by Google's front end and never reaches here.
        http_get { path = "/health" }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12
      }
    }
    # One review occupies an instance for minutes; sharing one would queue them.
    max_instance_request_concurrency = 4
  }

  depends_on = [
    google_project_service.public_apis,
    google_secret_manager_secret_version.api_token,
    google_secret_manager_secret_iam_member.runtime_reads_token,
  ]
}

# Cloud Run itself accepts every caller; the shared token in the app decides.
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = google_cloud_run_v2_service.public_api.location
  name     = google_cloud_run_v2_service.public_api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "public_api_url" { value = google_cloud_run_v2_service.public_api.uri }
output "upload_url" { value = "${google_cloud_run_v2_service.public_api.uri}/v1/uploads" }
output "review_url" { value = "${google_cloud_run_v2_service.public_api.uri}/v1/review" }
