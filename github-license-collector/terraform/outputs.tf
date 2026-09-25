output "cloud_run_job_name" {
  value       = google_cloud_run_v2_job.collector.name
  description = "Cloud Run Job name."
}

output "collector_service_account_email" {
  value       = google_service_account.collector.email
  description = "Runtime identity."
}

output "scheduler_service_account_email" {
  value       = google_service_account.scheduler.email
  description = "Cloud Scheduler invocation identity."
}

output "scheduler_job_name" {
  value       = google_cloud_scheduler_job.collector.name
  description = "Cloud Scheduler job name."
}

