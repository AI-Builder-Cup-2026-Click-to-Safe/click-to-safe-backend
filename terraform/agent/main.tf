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
  default = "gemini-3.1-flash-lite"
}
variable "model_region" {
  type    = string
  default = "global"
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
          GOOGLE_CLOUD_PROJECT = var.project_id
          MODEL                = var.model
          MODEL_REGION         = var.model_region
          VIDEO_BUCKET         = var.video_bucket
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
