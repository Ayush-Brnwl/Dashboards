# GitHub Enterprise License Snapshot Collector

This repository contains the production-shaped Cloud Run **Job** that populates
the Terraform-managed GitHub license snapshot tables from GitHub's enterprise
consumed-licenses API.

It is intentionally a batch job, not an HTTP service.

## What it does

For every Cloud Run execution, the collector:

1. Creates a GitHub App JWT in memory.
2. Exchanges it for a short-lived enterprise installation token.
3. Calls `GET /enterprises/{enterprise}/consumed-licenses` with 100 records per
   page and follows all pagination links.
4. Normalizes the API records to the existing
   `github_license_user_snapshot` schema.
5. Writes immutable raw pages, canonical NDJSON, and manifests to Cloud
   Storage.
6. appends the canonical file to the existing BigQuery snapshot table with
   `CREATE_NEVER` and `WRITE_APPEND`.
7. Validates row counts and identities.
8. Marks the run `COMPLETE`, `REVIEW_REQUIRED`, `REJECTED`, or `FAILED` in
   `github_license_snapshot_runs`.

The `github_license_users_current` view should expose only `COMPLETE` runs.
That makes the run status a commit marker: a partial or suspicious load never
becomes the current license inventory.

## Security properties

- The GitHub App requires only **Enterprise administration: Read**.
- The PEM private key is read from Secret Manager at container startup.
- The secret value is not put into Terraform configuration or state.
- Installation tokens, private keys, usernames, emails, and API response bodies
  are never logged.
- The runtime identity cannot create or delete BigQuery tables.
- The BigQuery load uses the destination schema and rejects unknown fields and
  bad records.
- GCS objects are create-only. A retry cannot overwrite evidence.
- BigQuery uses a deterministic load job ID. Retrying one Cloud Run execution
  cannot append the same snapshot twice.
- The container runs as non-root.

The raw archive contains identity and email data. Apply the bank's data
classification, CMEK, retention, access-review, DLP, and audit requirements to
the archive bucket. The bucket location must be compatible with the BigQuery
dataset location.

## Existing BigQuery contract

The application assumes these objects already exist and are managed by
Terraform:

```text
github_license_snapshot_runs
github_license_user_snapshot
github_license_users_current
```

The included Terraform deliberately does **not** create or modify them.

## GitHub App setup

Create an enterprise-owned GitHub App and configure:

| Setting | Value |
|---|---|
| Installation target | The GitHub Enterprise account |
| Enterprise permission | `Enterprise administration: Read` |
| Repository permissions | None |
| Organization permissions | None |
| Webhooks | Not required |

After installation, record these three different values:

- App ID — configured as `github_app_id`
- Enterprise installation ID — configured as
  `github_app_installation_id`
- Generated PEM private key — stored as the value of the Secret Manager secret

Do not use the App client ID in place of the App ID. Do not commit the PEM or
place it in `terraform.tfvars`.

For GHE.com data residency, set `github_api_url` to
`https://api.SUBDOMAIN.ghe.com`.

## Secret creation boundary

Create the Secret Manager **metadata** through the bank's Terraform module, for
example:

```hcl
resource "google_secret_manager_secret" "github_app_private_key" {
  project   = var.project_id
  secret_id = "github-license-collector-private-key"

  replication {
    user_managed {
      replicas {
        location = var.approved_secret_region
        customer_managed_encryption {
          kms_key_name = var.approved_secret_kms_key
        }
      }
    }
  }
}
```

Add the PEM as a secret version using the approved secrets pipeline. If policy
permits a local one-time command, the equivalent is:

```bash
gcloud secrets versions add github-license-collector-private-key \
  --project=PROJECT_ID \
  --data-file=PATH_TO_PRIVATE_KEY_PEM
```

Delete the downloaded PEM from the workstation using the bank-approved secure
process after the version has been verified. Pin the numeric version in
Terraform; use `latest` only for the initial DEV test.

## Build and publish

Use the bank's approved base-image mirror and dependency proxy. A generic build
looks like:

```bash
docker build \
  --build-arg PYTHON_BASE_IMAGE=APPROVED_REGISTRY/python:3.12-slim-bookworm \
  --tag REGION-docker.pkg.dev/PROJECT/REPOSITORY/github-license-collector:COMMIT_SHA \
  .

docker push \
  REGION-docker.pkg.dev/PROJECT/REPOSITORY/github-license-collector:COMMIT_SHA
```

Resolve the pushed image digest and pass the digest URI, not a mutable tag, to
Terraform:

```text
REGION-docker.pkg.dev/PROJECT/REPOSITORY/github-license-collector@sha256:...
```

The dependency ranges in `requirements.txt` are suitable for a prototype. The
bank CI process should resolve them through the approved package mirror,
produce a fully pinned lock or constraints file, run SCA/container scanning,
and retain the SBOM and image-attestation evidence.

## Terraform deployment

Copy `terraform/terraform.tfvars.example` to the protected environment-specific
configuration location and replace every placeholder. Keep these initial
values:

```hcl
run_mode        = "validate"
scheduler_paused = true
```

Then run the bank's normal Terraform workflow:

```bash
terraform -chdir=terraform init
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

The module creates:

- a dedicated Cloud Run runtime service account;
- a scheduler-only service account;
- a minimal BigQuery dataset writer custom role;
- project-level `roles/bigquery.jobUser` for query/load jobs;
- create/read access to the archive bucket;
- accessor permission on only the GitHub private-key secret;
- one-task, one-at-a-time Cloud Run Job;
- a paused Cloud Scheduler job.

If APIs are centrally enabled, retain `manage_project_services = false`.

If the Hello World resource has the same name and is already managed by the
same Terraform state, replace its image/configuration with this resource. If it
exists outside the state, import it before applying. A Cloud Run **service** and
a Cloud Run **job** are different resource types; leave the test service alone
and create this job if the Hello World deployment was an HTTP service.

## Network path

The job needs outbound HTTPS to:

- `api.github.com`, or the configured GHE.com API host;
- Google APIs used by Secret Manager, Cloud Storage, and BigQuery.

For a VPC-controlled environment, supply `vpc_connector` and the approved
egress mode. If all traffic is routed through the VPC, make sure Cloud NAT or
the enterprise proxy allows the GitHub API destination. If the GitHub
Enterprise IP allow list is enabled, add the approved static egress IP through
the normal security change process. Proxy values can be supplied in
`proxy_environment`; do not put proxy credentials there unless they are
injected from a separate approved secret design.

## Safe rollout

### 1. Validate GitHub authentication and pagination

With `run_mode = "validate"`, execute manually:

```bash
gcloud run jobs execute github-license-collector \
  --project=PROJECT_ID \
  --region=REGION \
  --wait
```

Expected log events:

```text
collector_started
github_extraction_completed
validation_mode_succeeded
```

This mode calls GitHub but does not write GCS or BigQuery.

### 2. Enable a single full write

Change and apply:

```hcl
run_mode = "full"
```

Keep the scheduler paused, execute once manually, and run queries 1–3 in
`sql/operational_queries.sql`.

The desired terminal status is `COMPLETE`. `REVIEW_REQUIRED` means the data was
loaded but remains invisible to the current view. `REJECTED` returns a non-zero
job exit code. `FAILED` represents a technical failure and also returns a
non-zero exit code.

### 3. Enable the schedule

After the first full snapshot reconciles with the legacy script, set:

```hcl
scheduler_paused = false
```

Apply Terraform again. The default schedule is daily at 02:00 UTC.

## Status and recovery model

| Status | Meaning | Current view? | Operator action |
|---|---|---:|---|
| `STARTED` | Run registered | No | Wait or investigate stale run |
| `EXTRACTED` | GitHub data archived | No | Retry same execution or launch a new one |
| `LOADED` | BigQuery append completed | No | Validation should follow |
| `COMPLETE` | All validation passed | Yes | None |
| `REVIEW_REQUIRED` | Load succeeded but a safety threshold fired | No | Review evidence, then controlled approval or rerun |
| `REJECTED` | Empty/incomplete/count-invalid snapshot | No | Correct source/configuration and start a new execution |
| `FAILED` | Technical failure | No | Fix the failure; Cloud Run can retry the same execution safely |

The collector holds a run for review when it finds duplicate/unresolved
identities, a mismatch between API totals and page count, or a user-count
change over `count_delta_review_percent`. The first valid full run has no
previous baseline and is not held only for that reason.

## Tests

From the repository root:

```bash
python -m pip install --requirement requirements-dev.txt
python -m pytest
python -m compileall -q app tests
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
```

Run these in CI with the approved internal Python and Terraform sources.

## Known intentional boundary

`advanced_security_enabled` is written as `NULL` because the consumed-licenses
response does not provide that per-user field. Advanced Security active
committer collection is a separate API and should be implemented as a separate
snapshot flow rather than being inferred in this collector.

