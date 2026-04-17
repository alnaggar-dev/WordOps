# Multitenancy DevOps Capabilities - Spec Delta

## ADDED Requirements

### Requirement: Health Check System
The system SHALL provide comprehensive health checking capabilities for monitoring infrastructure status.

#### Scenario: Basic health check
- **WHEN** operator runs `wo multitenancy health`
- **THEN** system returns health status for shared infrastructure, database, sites, disk space, PHP-FPM, and nginx
- **AND** overall status is "healthy", "degraded", or "unhealthy"

#### Scenario: JSON output for automation
- **WHEN** operator runs `wo multitenancy health --json`
- **THEN** system returns health data in machine-readable JSON format
- **AND** includes timestamp, status, and detailed checks object

#### Scenario: Single site health check
- **WHEN** operator runs `wo multitenancy health --site=example.com`
- **THEN** system returns health status for only the specified site
- **AND** includes WordPress accessibility, database connectivity, and cache status

#### Scenario: Unhealthy detection
- **WHEN** critical component (shared infrastructure, database) is unavailable
- **THEN** health check returns status "unhealthy"
- **AND** affected component shows error details

---

### Requirement: Structured Logging
The system SHALL produce structured JSON logs for log aggregation systems.

#### Scenario: JSON log format
- **WHEN** any multitenancy operation is performed
- **THEN** log entry is written to `/var/log/wo/multitenancy.json`
- **AND** entry includes timestamp, level, event name, and contextual data

#### Scenario: Operation timing
- **WHEN** operation completes
- **THEN** log entry includes `duration_ms` field with operation duration

#### Scenario: Correlation tracking
- **WHEN** multi-step operation is performed
- **THEN** all related log entries share the same `correlation_id`

#### Scenario: Log rotation
- **WHEN** the structured log file exceeds the configured rotation threshold
- **THEN** it is rotated by logrotate according to the shipped `/etc/logrotate.d/wo-multitenancy` policy

---

### Requirement: Machine-Readable Output
The system SHALL provide JSON output option for read commands.

#### Scenario: List command JSON
- **WHEN** operator runs `wo multitenancy list --json`
- **THEN** system returns site list as JSON array
- **AND** includes all site metadata (domain, php_version, cache_type, baseline_version, tags)

#### Scenario: Status command JSON
- **WHEN** operator runs `wo multitenancy status --json`
- **THEN** system returns status as JSON object
- **AND** includes infrastructure state, site counts, and baseline information

#### Scenario: Create command JSON
- **WHEN** operator runs `wo multitenancy create example.com --php83 --json`
- **THEN** system returns creation result as JSON
- **AND** includes site details, credentials, and timing information

---

### Requirement: Site Tagging
The system SHALL support tagging sites for group operations.

#### Scenario: Create site with tags
- **WHEN** operator runs `wo multitenancy create example.com --php83 --tags=production,client-a`
- **THEN** site is created with specified tags stored in database

#### Scenario: List sites by tag
- **WHEN** operator runs `wo multitenancy list --tags=production`
- **THEN** only sites with matching tag are returned

#### Scenario: Apply baseline by tag
- **WHEN** operator runs `wo multitenancy baseline apply --tags=staging`
- **THEN** baseline is applied only to sites with staging tag
- **AND** other sites are unchanged

#### Scenario: Update rejects tag scoping
- **WHEN** operator runs `wo multitenancy update --tags=production`
- **THEN** the command exits with a descriptive error and no changes are made
- **AND** the error explains that core updates switch a shared symlink for all tenants and points the operator to `wo multitenancy apply --tags=<csv>` for tag-scoped rollout

#### Scenario: Tag format validation
- **WHEN** operator supplies `--tags` with a value outside `[a-z0-9-]`
- **THEN** command exits with a validation error and no changes are made

---

### Requirement: Audit Logging
The system SHALL maintain audit logs for all privileged operations.

#### Scenario: Operation audit
- **WHEN** privileged operation (create, delete, update, rollback, baseline apply) is performed
- **THEN** audit record is written with actor, action, target, result, timestamp, and source IP

#### Scenario: Audit log viewing
- **WHEN** operator runs `wo multitenancy audit --since=24h`
- **THEN** system displays audit entries from last 24 hours
- **AND** entries are formatted with timestamp, actor, action, target, result

#### Scenario: Audit export
- **WHEN** operator runs `wo multitenancy audit --format=json --since=7d`
- **THEN** system exports audit entries as JSON for external analysis

#### Scenario: Audit integrity
- **WHEN** audit record is created
- **THEN** SHA-256 checksum is calculated and stored for tamper detection

#### Scenario: Audit retention
- **WHEN** audit retention period is configured (default 90 days)
- **THEN** records older than the retention window are pruned on the next audit write

---

### Requirement: Webhook Notifications
The system SHALL send webhook notifications for key events.

#### Scenario: Webhook configuration
- **WHEN** operator configures `[webhooks]` section in `multitenancy.conf`
- **THEN** system sends HTTP POST to configured URL for specified events

#### Scenario: Site creation webhook
- **WHEN** new site is created AND webhook is configured for `site_created`
- **THEN** webhook payload includes domain, php_version, cache_type, timestamp

#### Scenario: Update webhook
- **WHEN** WordPress update completes AND webhook is configured for `update_completed`
- **THEN** webhook payload includes new_release, sites_updated, duration

#### Scenario: Webhook signing
- **WHEN** webhook is sent AND `secret` is configured
- **THEN** request includes `X-WO-Signature` header with HMAC-SHA256 signature

#### Scenario: Webhook retry
- **WHEN** webhook delivery fails
- **THEN** system retries with exponential backoff up to 3 attempts

#### Scenario: Webhook failure isolation
- **WHEN** all webhook retries fail
- **THEN** originating operation still completes successfully
- **AND** failure is recorded in the structured log

---

### Requirement: Maintenance Mode
The system SHALL support maintenance mode for sites.

#### Scenario: Enable maintenance
- **WHEN** operator runs `wo multitenancy maintenance --enable --site=example.com`
- **THEN** site displays maintenance page for all visitors
- **AND** nginx returns 503 status code

#### Scenario: Custom maintenance message
- **WHEN** operator runs `wo multitenancy maintenance --enable --message="Back in 10 minutes"`
- **THEN** maintenance page displays custom message

#### Scenario: Admin bypass
- **WHEN** maintenance is enabled AND admin IP is configured
- **THEN** requests from admin IP receive normal site content

#### Scenario: Disable maintenance
- **WHEN** operator runs `wo multitenancy maintenance --disable --site=example.com`
- **THEN** site returns to normal operation immediately

#### Scenario: Global maintenance
- **WHEN** operator runs `wo multitenancy maintenance --enable --all`
- **THEN** every tenant site is placed in maintenance mode in a single operation

---

### Requirement: Baseline Apply
The system SHALL apply baseline configuration to every enabled tenant site, with support for previewing (dry-run), detailed per-site progress (verbose), and restricting the target set by tag.

#### Scenario: Default baseline apply
- **WHEN** operator runs `wo multitenancy apply`
- **THEN** every enabled, non-staging, non-quarantined tenant site receives the current baseline
- **AND** failures are recorded by quarantining the offending site
- **AND** a summary of attempted/succeeded/failed counts is returned

#### Scenario: Dry-run baseline apply
- **WHEN** operator runs `wo multitenancy apply --dry-run`
- **THEN** system reports what changes would be made to each site
- **AND** no actual changes are applied to any site
- **AND** the staging-site gate is skipped in dry-run mode

#### Scenario: Verbose apply
- **WHEN** operator runs `wo multitenancy apply --verbose`
- **THEN** system shows detailed progress for each site
- **AND** includes plugin activation status and per-site timing in milliseconds

#### Scenario: Tagged apply
- **WHEN** operator runs `wo multitenancy apply --tags=production`
- **THEN** baseline is applied only to sites whose tag set intersects the supplied tags
- **AND** non-matching sites are left unchanged

#### Scenario: Dry-run with tag filter
- **WHEN** operator runs `wo multitenancy apply --dry-run --tags=staging`
- **THEN** the preview output is limited to staging-tagged sites only
- **AND** no state is mutated
