terraform {
  required_version = ">= 1.5, < 2.0"
  backend "gcs" {}
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.3.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "8.3.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
provider "google-beta" {
  project = var.project_id
  region  = var.region
}

variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}

data "google_project" "current" { project_id = var.project_id }

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com", "artifactregistry.googleapis.com",
    "storage.googleapis.com", "iam.googleapis.com", "logging.googleapis.com"
  ])
  service            = each.value
  disable_on_destroy = false
}

# Generating the Vertex AI service identity also creates the Reasoning Engine service agent.
resource "google_project_service_identity" "vertex" {
  provider   = google-beta
  project    = var.project_id
  service    = "aiplatform.googleapis.com"
  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "agents" {
  location      = var.region
  repository_id = "prototype-agents"
  format        = "DOCKER"
  description   = "Prototype Agent Runtime container images"
  depends_on    = [google_project_service.apis]
  lifecycle { prevent_destroy = true }
}

resource "google_artifact_registry_repository_iam_member" "runtime_image_reader" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.agents.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  depends_on = [google_project_service_identity.vertex]
}

resource "google_service_account" "runtime" {
  account_id   = "prototype-agent-runtime"
  display_name = "Prototype video agent runtime"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "runtime_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = google_service_account.runtime.member
}

# The Cloud Storage JSON API bills requests to the caller's project, so the
# runtime identity needs project-level service usage even for bucket-scoped reads.
resource "google_project_iam_member" "runtime_service_usage" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = google_service_account.runtime.member
}

resource "google_storage_bucket" "videos" {
  name                        = "${var.project_id}-prototype-videos"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  depends_on                  = [google_project_service.apis]
  lifecycle { prevent_destroy = true }
}

resource "google_storage_bucket_iam_member" "runtime_video_access" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])
  bucket   = google_storage_bucket.videos.name
  role     = each.value
  member   = google_service_account.runtime.member
}

resource "google_storage_bucket_iam_member" "vertex_video_reader" {
  bucket = google_storage_bucket.videos.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_project_service_identity.vertex.email}"
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agents.repository_id}/video-agent"
}
output "runtime_service_account" { value = google_service_account.runtime.email }
output "video_bucket" { value = google_storage_bucket.videos.name }
