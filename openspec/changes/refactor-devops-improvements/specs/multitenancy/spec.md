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

---

### Requirement: Machine-Readable Output
The system SHALL provide JSON output option for all commands.

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

#### Scenario: Update by tag
- **WHEN** operator runs `wo multitenancy update --tags=canary`
- **THEN** WordPress core is updated only for sites with canary tag

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

---

### Requirement: Webhook Notifications
The system SHALL send webhook notifications for key events.

#### Scenario: Webhook configuration
- **WHEN** operator configures `[webhooks]` section in multitenancy.conf
- **THEN** system sends HTTP POST to configured URL for specified events

#### Scenario: Site creation webhook
- **WHEN** new site is created AND webhook is configured for `site_created`
- **THEN** webhook payload includes domain, php_version, cache_type, timestamp

#### Scenario: Update webhook
- **WHEN** WordPress update completes AND webhook is configured for `update_completed`
- **THEN** webhook payload includes new_release, sites_updated, duration

#### Scenario: Webhook signing
- **WHEN** webhook is sent AND secret is configured
- **THEN** request includes `X-WO-Signature` header with HMAC-SHA256 signature

#### Scenario: Webhook retry
- **WHEN** webhook delivery fails
- **THEN** system retries with exponential backoff up to 3 times

---

### Requirement: Secrets Management
The system SHALL support secure secrets storage with multiple providers.

#### Scenario: Environment variable secrets (default)
- **WHEN** no secrets provider is configured
- **THEN** system reads secrets from `WO_MT_*` environment variables

#### Scenario: HashiCorp Vault integration
- **WHEN** secrets provider is configured as `vault`
- **THEN** system retrieves secrets from HashiCorp Vault at configured path

#### Scenario: Encrypted file secrets
- **WHEN** secrets provider is configured as `file`
- **THEN** system reads secrets from AES-encrypted file

#### Scenario: GitHub token from secrets
- **WHEN** GitHub plugin source is used
- **THEN** system retrieves `github_token` from configured secrets provider
- **AND** token is never logged or displayed

---

### Requirement: Backup System
The system SHALL provide automated backup capabilities.

#### Scenario: Full site backup
- **WHEN** operator runs `wo multitenancy backup --site=example.com`
- **THEN** system creates backup including database and uploads
- **AND** backup is stored at configured destination

#### Scenario: S3 backup destination
- **WHEN** backup destination is configured as `s3://bucket/path`
- **THEN** backup is uploaded to S3-compatible storage
- **AND** backup metadata is recorded in database

#### Scenario: Backup encryption
- **WHEN** backup encryption is enabled in config
- **THEN** backup archive is encrypted with AES-256-GCM before storage

#### Scenario: Backup retention
- **WHEN** backup is created AND retention is configured
- **THEN** backups older than retention period are automatically deleted

---

### Requirement: Restore System
The system SHALL provide restore capabilities from backups.

#### Scenario: Full restore
- **WHEN** operator runs `wo multitenancy restore --site=example.com --from=s3://bucket/backup.tar.gz`
- **THEN** system restores database and uploads from backup
- **AND** site is operational after restore

#### Scenario: Restore dry-run
- **WHEN** operator runs `wo multitenancy restore --site=example.com --from=path --dry-run`
- **THEN** system validates backup and shows what would be restored
- **AND** no changes are made to site

#### Scenario: Point-in-time restore
- **WHEN** operator runs `wo multitenancy restore --site=example.com --point-in-time=2025-01-14T23:00:00`
- **THEN** system restores from nearest backup before specified time

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

---

### Requirement: Canary Deployments
The system SHALL support gradual rollout of updates.

#### Scenario: Canary update
- **WHEN** operator runs `wo multitenancy update --canary=10%`
- **THEN** WordPress core is updated on 10% of sites
- **AND** remaining sites are unchanged

#### Scenario: Canary promotion
- **WHEN** operator runs `wo multitenancy update --canary-promote`
- **THEN** update is applied to all remaining sites

#### Scenario: Canary rollback
- **WHEN** operator runs `wo multitenancy update --canary-rollback`
- **THEN** canary sites are rolled back to previous release
- **AND** non-canary sites remain unchanged

#### Scenario: Canary with tags
- **WHEN** operator runs `wo multitenancy update --canary=100% --tags=staging`
- **THEN** only staging-tagged sites are updated (full canary within tag group)

---

### Requirement: Environment Configuration
The system SHALL support environment-specific configuration.

#### Scenario: Environment override
- **WHEN** `multitenancy.staging.conf` exists AND `--env=staging` is passed
- **THEN** staging config values override base config

#### Scenario: Default environment
- **WHEN** `--env` is not specified
- **THEN** system uses base `multitenancy.conf` only

#### Scenario: Environment validation
- **WHEN** operator runs `wo multitenancy config validate --env=production`
- **THEN** system validates environment-specific config
- **AND** reports any errors or warnings

---

## MODIFIED Requirements

### Requirement: Baseline Apply
The system SHALL apply baseline configuration to sites with dry-run support.

#### Scenario: Dry-run baseline apply
- **WHEN** operator runs `wo multitenancy baseline apply --dry-run`
- **THEN** system shows what changes would be made to each site
- **AND** no actual changes are applied

#### Scenario: Verbose apply
- **WHEN** operator runs `wo multitenancy baseline apply --verbose`
- **THEN** system shows detailed progress for each site
- **AND** includes plugin activation status and timing

#### Scenario: Tagged apply (new)
- **WHEN** operator runs `wo multitenancy baseline apply --tags=production`
- **THEN** baseline is applied only to production-tagged sites
