# Tasks: DevOps Improvements Implementation

## Phase 1: Foundation

### 1.1 Health Check System
- [ ] 1.1.1 Create `health_check()` method in `multitenancy_functions.py`
- [ ] 1.1.2 Implement shared infrastructure health check (symlink validity, release exists)
- [ ] 1.1.3 Implement database connectivity check (MySQL connection test)
- [ ] 1.1.4 Implement site health aggregation (iterate sites, check WP accessibility)
- [ ] 1.1.5 Implement disk space check (compare against `min_free_space` config)
- [ ] 1.1.6 Implement PHP-FPM status check (service status, socket accessibility)
- [ ] 1.1.7 Implement nginx status check (service status, config validity)
- [ ] 1.1.8 Add `health` subcommand to `WOMultitenancyController`
- [ ] 1.1.9 Add `--json` output format
- [ ] 1.1.10 Add `--site=<domain>` filter for single-site health check
- [ ] 1.1.11 Write unit tests for health check functions

### 1.2 Structured Logging
- [ ] 1.2.1 Create `MultitenancyLogger` class in new `multitenancy_logging.py`
- [ ] 1.2.2 Implement JSON structured log output format
- [ ] 1.2.3 Add log rotation configuration
- [ ] 1.2.4 Configure separate log file `/var/log/wo/multitenancy.json`
- [ ] 1.2.5 Add correlation IDs for tracing operations
- [ ] 1.2.6 Refactor existing `Log.info/debug/error` calls to use structured logging
- [ ] 1.2.7 Add timing metrics to all operations (duration_ms)
- [ ] 1.2.8 Document log format and fields

### 1.3 JSON Output for All Commands
- [ ] 1.3.1 Add `--json` argument to `WOMultitenancyController.Meta.arguments`
- [ ] 1.3.2 Create `JsonOutput` helper class for consistent formatting
- [ ] 1.3.3 Refactor `list` command to support JSON output
- [ ] 1.3.4 Refactor `status` command to support JSON output
- [ ] 1.3.5 Refactor `baseline` command to support JSON output
- [ ] 1.3.6 Refactor `create` command to support JSON output
- [ ] 1.3.7 Add JSON output to all remaining commands
- [ ] 1.3.8 Document JSON schemas for each command

### 1.4 Site Tagging
- [ ] 1.4.1 Add `tags` column to `multitenancy_sites` table
- [ ] 1.4.2 Create migration for existing installations
- [ ] 1.4.3 Add `--tags` argument to `create` command
- [ ] 1.4.4 Implement `MTDatabase.get_sites_by_tags()` method
- [ ] 1.4.5 Add `tags` subcommand for managing site tags
- [ ] 1.4.6 Support `--tags` filter in `list`, `update`, `baseline apply`
- [ ] 1.4.7 Add tag validation (alphanumeric, hyphen, comma-separated)
- [ ] 1.4.8 Document tagging best practices

---

## Phase 2: Audit & Notifications

### 2.1 Audit Logging
- [ ] 2.1.1 Create `multitenancy_audit` table in database
- [ ] 2.1.2 Implement `AuditLogger` class
- [ ] 2.1.3 Add audit logging to `create` operation
- [ ] 2.1.4 Add audit logging to `delete` operation
- [ ] 2.1.5 Add audit logging to `update` operation
- [ ] 2.1.6 Add audit logging to `rollback` operation
- [ ] 2.1.7 Add audit logging to `baseline apply` operation
- [ ] 2.1.8 Add audit logging to `shared-config` operations
- [ ] 2.1.9 Implement `audit` subcommand with filters
- [ ] 2.1.10 Support export to CSV/JSON format
- [ ] 2.1.11 Add audit log retention/rotation

### 2.2 Webhook Notifications
- [ ] 2.2.1 Add `[webhooks]` section to config parser
- [ ] 2.2.2 Implement `WebhookNotifier` class
- [ ] 2.2.3 Define webhook payload schema
- [ ] 2.2.4 Implement HMAC signing for webhook security
- [ ] 2.2.5 Add webhook calls to key operations (create, delete, update, rollback)
- [ ] 2.2.6 Implement retry logic with exponential backoff
- [ ] 2.2.7 Add webhook test command
- [ ] 2.2.8 Support multiple webhook URLs
- [ ] 2.2.9 Document webhook integration (Slack, Discord, custom)

---

## Phase 3: Security

### 3.1 Secrets Management
- [ ] 3.1.1 Add `[secrets]` section to config parser
- [ ] 3.1.2 Create `SecretsProvider` abstract class
- [ ] 3.1.3 Implement `FileSecretsProvider` (encrypted file)
- [ ] 3.1.4 Implement `EnvSecretsProvider` (environment variables - current)
- [ ] 3.1.5 Implement `VaultSecretsProvider` (HashiCorp Vault)
- [ ] 3.1.6 Implement `AWSSecretsProvider` (AWS Secrets Manager)
- [ ] 3.1.7 Refactor GitHub token retrieval to use secrets provider
- [ ] 3.1.8 Add secret rotation support
- [ ] 3.1.9 Add `secrets` subcommand for management
- [ ] 3.1.10 Document secrets setup for each provider

### 3.2 Enhanced Audit Trail
- [ ] 3.2.1 Add source IP tracking to audit logs
- [ ] 3.2.2 Add session/correlation ID tracking
- [ ] 3.2.3 Implement audit log signing for tamper detection
- [ ] 3.2.4 Add SIEM export format (CEF/LEEF)
- [ ] 3.2.5 Implement audit log archival
- [ ] 3.2.6 Add compliance reporting templates

---

## Phase 4: Operations

### 4.1 Backup System
- [ ] 4.1.1 Create `multitenancy_backup.py` module
- [ ] 4.1.2 Add `[backup]` section to config parser
- [ ] 4.1.3 Create `multitenancy_backups` table
- [ ] 4.1.4 Implement local backup (tar.gz)
- [ ] 4.1.5 Implement S3 backup destination
- [ ] 4.1.6 Implement rsync backup destination
- [ ] 4.1.7 Support database-only backup
- [ ] 4.1.8 Support uploads-only backup
- [ ] 4.1.9 Support full backup
- [ ] 4.1.10 Implement backup encryption (AES-256)
- [ ] 4.1.11 Add `backup` subcommand
- [ ] 4.1.12 Implement backup retention/cleanup
- [ ] 4.1.13 Add backup verification

### 4.2 Restore System
- [ ] 4.2.1 Implement local restore
- [ ] 4.2.2 Implement S3 restore
- [ ] 4.2.3 Implement point-in-time restore
- [ ] 4.2.4 Add `restore` subcommand
- [ ] 4.2.5 Add restore dry-run mode
- [ ] 4.2.6 Implement restore verification
- [ ] 4.2.7 Document disaster recovery procedures

### 4.3 Maintenance Mode
- [ ] 4.3.1 Create maintenance page template
- [ ] 4.3.2 Implement nginx maintenance configuration
- [ ] 4.3.3 Add `maintenance` subcommand
- [ ] 4.3.4 Support custom maintenance message
- [ ] 4.3.5 Support scheduled maintenance windows
- [ ] 4.3.6 Add maintenance mode bypass for admin IPs
- [ ] 4.3.7 Integrate with webhook notifications

### 4.4 Canary Deployments
- [ ] 4.4.1 Add `--canary` flag to `update` command
- [ ] 4.4.2 Implement site selection algorithm (percentage-based)
- [ ] 4.4.3 Track canary state in database
- [ ] 4.4.4 Implement `--canary-promote` for full rollout
- [ ] 4.4.5 Implement `--canary-rollback` for canary abort
- [ ] 4.4.6 Add canary health check validation
- [ ] 4.4.7 Support tag-based canary selection
- [ ] 4.4.8 Document canary deployment workflow

---

## Documentation

### 5.1 User Documentation
- [ ] 5.1.1 Update WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md with new features
- [ ] 5.1.2 Add backup/restore runbook
- [ ] 5.1.3 Add troubleshooting section for new features

### 5.2 Developer Documentation
- [ ] 5.2.1 Document module architecture
- [ ] 5.2.2 Document extension points
- [ ] 5.2.3 Add contributing guide
- [ ] 5.2.4 Document testing procedures

---

## Testing

### 6.1 Unit Tests
- [ ] 6.1.1 Create test suite for health check module
- [ ] 6.1.2 Create test suite for audit logging
- [ ] 6.1.3 Create test suite for webhook notifications
- [ ] 6.1.4 Create test suite for secrets providers
- [ ] 6.1.5 Create test suite for backup/restore

### 6.2 Integration Tests
- [ ] 6.2.1 End-to-end health check validation
- [ ] 6.2.2 Webhook delivery test
- [ ] 6.2.3 Backup/restore cycle test
- [ ] 6.2.4 Canary deployment workflow test

### 6.3 Performance Tests
- [ ] 6.3.1 Health check latency benchmarks
- [ ] 6.3.2 Backup/restore speed benchmarks

---

## Milestones

| Phase | Key Deliverables |
|-------|-----------------|
| Phase 1 | Health checks, JSON output, site tags |
| Phase 2 | Audit logging, webhooks |
| Phase 3 | Secrets management |
| Phase 4 | Backup/restore, maintenance mode, canary |
