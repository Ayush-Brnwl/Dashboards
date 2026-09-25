-- GitHub license collector operational queries.
-- Replace the project/dataset only if they differ from the current DEV setup.

-- 1. Recent executions. The date predicate supports require_partition_filter.
SELECT
  snapshot_date,
  snapshot_run_id,
  status,
  collection_started_at,
  collection_completed_at,
  api_total_seats_consumed,
  api_total_seats_purchased,
  api_user_record_count,
  loaded_user_record_count,
  pages_fetched,
  pagination_complete,
  duplicate_identity_count,
  missing_github_login_count,
  unresolved_identity_count,
  failure_stage,
  error_message,
  collector_version
FROM `db-dev-l4to-mp-github.github_governance.github_license_snapshot_runs`
WHERE snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
ORDER BY collection_started_at DESC;

-- 2. Validate one run after a manual full execution.
-- Supply query parameters @snapshot_date (DATE) and @snapshot_run_id (STRING).
SELECT
  COUNT(*) AS row_count,
  COUNT(DISTINCT license_identity_key) AS distinct_identity_count,
  COUNT(*) - COUNT(DISTINCT license_identity_key) AS duplicate_identity_count,
  COUNTIF(identity_key_type = 'UNRESOLVED') AS unresolved_identity_count,
  COUNTIF(github_login_normalized IS NULL) AS missing_github_login_count,
  MIN(captured_at) AS min_captured_at,
  MAX(captured_at) AS max_captured_at
FROM `db-dev-l4to-mp-github.github_governance.github_license_user_snapshot`
WHERE snapshot_date = @snapshot_date
  AND snapshot_run_id = @snapshot_run_id;

-- 3. Confirm the current view exposes only the latest COMPLETE run.
SELECT
  snapshot_date,
  snapshot_run_id,
  COUNT(*) AS current_licensed_identities
FROM `db-dev-l4to-mp-github.github_governance.github_license_users_current`
GROUP BY snapshot_date, snapshot_run_id;

-- 4. Inspect a snapshot held for review before any controlled promotion.
SELECT
  snapshot_date,
  snapshot_run_id,
  status,
  error_message AS review_reason,
  api_user_record_count,
  loaded_user_record_count,
  duplicate_identity_count,
  unresolved_identity_count
FROM `db-dev-l4to-mp-github.github_governance.github_license_snapshot_runs`
WHERE snapshot_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
  AND status IN ('REVIEW_REQUIRED', 'REJECTED')
ORDER BY collection_started_at DESC;

-- 5. Optional, change-controlled approval after evidence review.
-- Do not run this automatically. Preserve error_message as the review evidence.
-- Supply @snapshot_date and @snapshot_run_id.
UPDATE `db-dev-l4to-mp-github.github_governance.github_license_snapshot_runs`
SET status = 'COMPLETE',
    updated_at = CURRENT_TIMESTAMP()
WHERE snapshot_date = @snapshot_date
  AND snapshot_run_id = @snapshot_run_id
  AND status = 'REVIEW_REQUIRED';

