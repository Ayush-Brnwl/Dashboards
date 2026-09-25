variable "project_id" {
  description = "GCP project containing the Cloud Run Job and BigQuery dataset."
  type        = string
}

variable "region" {
  description = "Cloud Run and Cloud Scheduler region."
  type        = string
}

variable "image_uri" {
  description = "Immutable collector image URI. Prefer an Artifact Registry digest, not a mutable tag."
  type        = string
}

variable "job_name" {
  description = "Cloud Run Job name."
  type        = string
  default     = "github-license-collector"
}

variable "collector_service_account_id" {
  description = "Account ID for the dedicated runtime service account."
  type        = string
  default     = "github-license-collector"
}

variable "scheduler_service_account_id" {
  description = "Account ID for the scheduler-only service account."
  type        = string
  default     = "github-license-scheduler"
}

variable "bigquery_dataset_id" {
  description = "Existing Terraform-managed BigQuery dataset ID."
  type        = string
}

variable "bigquery_snapshot_table_id" {
  description = "Existing snapshot table ID. The runtime never creates it."
  type        = string
  default     = "github_license_user_snapshot"
}

variable "bigquery_run_table_id" {
  description = "Existing snapshot-runs table ID. The runtime never creates it."
  type        = string
  default     = "github_license_snapshot_runs"
}

variable "bigquery_location" {
  description = "BigQuery job location, for example EU or europe-west3."
  type        = string
}

variable "archive_bucket_name" {
  description = "Existing Terraform-managed GCS bucket for immutable raw and canonical snapshots."
  type        = string
}

variable "archive_prefix" {
  description = "Object prefix in the archive bucket."
  type        = string
  default     = "github-license-snapshots"
}

variable "github_enterprise_slug" {
  description = "GitHub Enterprise slug used in /enterprises/{slug}/consumed-licenses."
  type        = string
}

variable "github_app_id" {
  description = "GitHub App ID. This is not the client ID."
  type        = string
}

variable "github_app_installation_id" {
  description = "Enterprise installation ID for the GitHub App."
  type        = number
}

variable "github_private_key_secret_id" {
  description = "Existing Secret Manager secret ID containing only the GitHub App PEM private key."
  type        = string
}

variable "github_private_key_secret_version" {
  description = "Secret version injected into the job. Pin a numeric version after testing."
  type        = string
  default     = "latest"
}

variable "github_api_url" {
  description = "GitHub REST API base URL. Change for GHE.com data residency."
  type        = string
  default     = "https://api.github.com"
}

variable "github_api_version" {
  description = "Pinned GitHub REST API version."
  type        = string
  default     = "2026-03-10"
}

variable "collector_version" {
  description = "Immutable application version written into every snapshot."
  type        = string
}

variable "run_mode" {
  description = "validate calls GitHub without writing; full archives and loads data."
  type        = string
  default     = "validate"

  validation {
    condition     = contains(["validate", "full"], var.run_mode)
    error_message = "run_mode must be validate or full."
  }
}

variable "count_delta_review_percent" {
  description = "Hold a snapshot for review when user count changes by more than this percentage."
  type        = number
  default     = 20
}

variable "schedule" {
  description = "Cloud Scheduler cron expression."
  type        = string
  default     = "0 2 * * *"
}

variable "schedule_time_zone" {
  description = "IANA time zone for the schedule."
  type        = string
  default     = "Etc/UTC"
}

variable "scheduler_paused" {
  description = "Keep true during initial manual validation."
  type        = bool
  default     = true
}

variable "task_timeout" {
  description = "Maximum duration for one collector task."
  type        = string
  default     = "1800s"
}

variable "max_retries" {
  description = "Cloud Run task retries. The collector is idempotent for one execution ID."
  type        = number
  default     = 1
}

variable "cpu" {
  description = "CPU limit."
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory limit. Raw pages and normalized records are held in memory."
  type        = string
  default     = "1Gi"
}

variable "vpc_connector" {
  description = "Optional Serverless VPC Access connector resource name."
  type        = string
  default     = null
  nullable    = true
}

variable "vpc_egress" {
  description = "Cloud Run VPC egress mode when a connector is configured."
  type        = string
  default     = "PRIVATE_RANGES_ONLY"

  validation {
    condition = contains(
      ["PRIVATE_RANGES_ONLY", "ALL_TRAFFIC"],
      var.vpc_egress
    )
    error_message = "vpc_egress must be PRIVATE_RANGES_ONLY or ALL_TRAFFIC."
  }
}

variable "proxy_environment" {
  description = "Optional non-secret proxy environment variables, e.g. HTTPS_PROXY and NO_PROXY."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for key in keys(var.proxy_environment) : contains(
        [
          "HTTP_PROXY",
          "HTTPS_PROXY",
          "NO_PROXY",
          "http_proxy",
          "https_proxy",
          "no_proxy",
        ],
        key,
      )
    ])
    error_message = "proxy_environment may contain only HTTP_PROXY, HTTPS_PROXY, NO_PROXY, and their lowercase equivalents."
  }
}

variable "deletion_protection" {
  description = "Protect the Cloud Run Job from accidental deletion."
  type        = bool
  default     = true
}

variable "manage_project_services" {
  description = "Whether this module enables required Google APIs. Set false when APIs are centrally managed."
  type        = bool
  default     = false
}
