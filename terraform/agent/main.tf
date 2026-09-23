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
# Reading the footage is the step the rest of the review depends on, so it runs
# on a stronger model than the text agents.
variable "vision_model" {
  type    = string
  default = "gemini-3.8-flash"
}
variable "model_region" {
  type    = string
  default = "global"
}
# RAG Engine is not open to new projects in every region, so the statute corpus
# may live apart from the runtime; retrieval is a plain HTTPS call either way.
variable "law_corpus" {
  type        = string
  default     = ""
  description = "Full resource name of the statute RAG corpus, or empty to run without retrieval."
  validation {
    condition     = var.law_corpus == "" || can(regex("^projects/[^/]+/locations/[^/]+/ragCorpora/[0-9]+$", var.law_corpus))
    error_message = "Use a full ragCorpora resource name, or leave it empty."
  }
}

resource "google_vertex_ai_reasoning_engine" "video_agent" {
  display_name = "prototype-video-agent"
  description  = "Gemini video analysis deployed by GitHub Actions and Terraform"
  project      = var.project_id
  region       = var.region

  spec {
    service_account = var.runtime_service_account
    container_spec {
      image_uri = var.image_uri
      port      = 8080
    }
    deployment_spec {
      min_instances         = 0
      max_instances         = 2
      container_concurrency = 3
      resource_limits = {
        cpu    = "1"
        memory = "1Gi"
      }
      dynamic "env" {
        for_each = {
          APP_PROJECT_ID = var.project_id
          MODEL          = var.model
          VISION_MODEL   = var.vision_model
          MODEL_REGION   = var.model_region
          VIDEO_BUCKET   = var.video_bucket
          LAW_CORPUS     = var.law_corpus
          # Agent Runtime reserves GOOGLE_CLOUD_*; the ADK agents are pointed at
          # Vertex from code using APP_PROJECT_ID and MODEL_REGION instead.
        }
        content {
          name  = env.key
          value = env.value
        }
      }
    }
    class_methods = jsonencode([
      { name = "health", api_mode = "" },
      { name = "query", api_mode = "async" },
      { name = "analyze_video", api_mode = "async" },
      { name = "review_violations", api_mode = "async" },
      { name = "review_traffic", api_mode = "async" },
      { name = "stream_query", api_mode = "async_stream" }
    ])
  }
}

output "reasoning_engine_resource_name" { value = google_vertex_ai_reasoning_engine.video_agent.id }
output "query_url" {
  value = "https://${var.region}-aiplatform.googleapis.com/v1/${google_vertex_ai_reasoning_engine.video_agent.id}:query"
}
output "custom_api_base_url" {
  value = "https://${var.region}-aiplatform.googleapis.com/reasoningEngines/v1/${google_vertex_ai_reasoning_engine.video_agent.id}/api"
}
