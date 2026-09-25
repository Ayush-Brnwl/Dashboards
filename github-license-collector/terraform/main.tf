locals {
  required_services = toset([
    "bigquery.googleapis.com",
    "cloudscheduler.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])

  collector_environment = merge(
    var.proxy_environment,
    {
      GCP_PROJECT_ID               = var.project_id
      BQ_DATASET_ID                = var.bigquery_dataset_id
      BQ_SNAPSHOT_TABLE_ID         = var.bigquery_snapshot_table_id
      BQ_RUN_TABLE_ID              = var.bigquery_run_table_id
      BQ_LOCATION                  = var.bigquery_location
      GCS_BUCKET                   = var.archive_bucket_name
      GCS_PREFIX                   = var.archive_prefix
      GITHUB_API_URL               = var.github_api_url
      GITHUB_API_VERSION           = var.github_api_version
      GITHUB_ENTERPRISE_SLUG       = var.github_enterprise_slug
      GITHUB_APP_ID                = var.github_app_id
      GITHUB_APP_INSTALLATION_ID   = tostring(var.github_app_installation_id)
      COLLECTOR_VERSION            = var.collector_version
      RUN_MODE                     = var.run_mode
      COUNT_DELTA_REVIEW_PERCENT   = tostring(var.count_delta_review_percent)
      HTTP_CONNECT_TIMEOUT_SECONDS = "10"
      HTTP_READ_TIMEOUT_SECONDS    = "60"
      MAX_GITHUB_PAGES             = "10000"
    },
  )
}

resource "google_project_service" "required" {
  for_each = var.manage_project_services ? local.required_services : toset([])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "collector" {
  project      = var.project_id
  account_id   = var.collector_service_account_id
  display_name = "GitHub license snapshot collector"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = var.scheduler_service_account_id
  display_name = "GitHub license collector scheduler"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_custom_role" "bigquery_snapshot_writer" {
  project     = var.project_id
  role_id     = "githubLicenseSnapshotWriter"
  title       = "GitHub License Snapshot Writer"
  description = "Minimum dataset permissions for the GitHub license collector."
  permissions = [
    "bigquery.tables.get",
    "bigquery.tables.getData",
    "bigquery.tables.updateData",
  ]
}

resource "google_project_iam_member" "collector_bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.collector.email}"
}

resource "google_bigquery_dataset_iam_member" "collector_dataset_writer" {
  project    = var.project_id
  dataset_id = var.bigquery_dataset_id
  role       = google_project_iam_custom_role.bigquery_snapshot_writer.name
  member     = "serviceAccount:${google_service_account.collector.email}"
}

resource "google_storage_bucket_iam_member" "collector_archive_creator" {
  bucket = var.archive_bucket_name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.collector.email}"
}

resource "google_storage_bucket_iam_member" "collector_archive_viewer" {
  bucket = var.archive_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.collector.email}"
}

resource "google_secret_manager_secret_iam_member" "collector_private_key" {
  project   = var.project_id
  secret_id = var.github_private_key_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.collector.email}"
}

resource "google_cloud_run_v2_job" "collector" {
  project             = var.project_id
  name                = var.job_name
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.collector.email
      timeout         = var.task_timeout
      max_retries     = var.max_retries

      containers {
        image = var.image_uri

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        dynamic "env" {
          for_each = local.collector_environment
          content {
            name  = env.key
            value = env.value
          }
        }

        env {
          name = "GITHUB_APP_PRIVATE_KEY"
          value_source {
            secret_key_ref {
              secret  = var.github_private_key_secret_id
              version = var.github_private_key_secret_version
            }
          }
        }
      }

      dynamic "vpc_access" {
        for_each = var.vpc_connector == null ? [] : [var.vpc_connector]
        content {
          connector = vpc_access.value
          egress    = var.vpc_egress
        }
      }
    }
  }

  depends_on = [
    google_bigquery_dataset_iam_member.collector_dataset_writer,
    google_project_iam_member.collector_bigquery_job_user,
    google_secret_manager_secret_iam_member.collector_private_key,
    google_storage_bucket_iam_member.collector_archive_creator,
    google_storage_bucket_iam_member.collector_archive_viewer,
  ]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_job.collector.location
  name     = google_cloud_run_v2_job.collector.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "collector" {
  project          = var.project_id
  region           = var.region
  name             = "${var.job_name}-daily"
  description      = "Execute the GitHub license snapshot Cloud Run Job"
  schedule         = var.schedule
  time_zone        = var.schedule_time_zone
  paused           = var.scheduler_paused
  attempt_deadline = "320s"

  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
    max_retry_duration   = "900s"
  }

  http_target {
    http_method = "POST"
    uri = format(
      "https://run.googleapis.com/v2/projects/%s/locations/%s/jobs/%s:run",
      var.project_id,
      google_cloud_run_v2_job.collector.location,
      google_cloud_run_v2_job.collector.name,
    )

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_cloud_run_v2_job_iam_member.scheduler_invoker,
    google_project_service.required,
  ]
}
