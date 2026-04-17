# Tasks: DevOps Improvements Implementation

## Phase 1: Visibility

### 1.1 Health Check System
- [x] 1.1.1 Create composable `HealthChecker` in new `multitenancy_health.py`
- [x] 1.1.2 Implement shared-infrastructure checker (symlink validity, release exists)
- [x] 1.1.3 Implement database connectivity checker (MySQL ping + timing)
- [x] 1.1.4 Implement site aggregation checker (iterate sites, check WP accessibility)
- [x] 1.1.5 Implement disk-space checker (compare against `min_free_space`)
- [x] 1.1.6 Implement PHP-FPM checker (service status, socket accessibility)
- [x] 1.1.7 Implement nginx checker (service status, `nginx -t`)
- [x] 1.1.8 Add `health` subcommand to `WOMultitenancyController`
- [x] 1.1.9 Add `--json` output format
- [x] 1.1.10 Add `--site=<domain>` filter for single-site health check
- [x] 1.1.11 Write unit tests for each checker

### 1.2 Structured Logging
- [x] 1.2.1 Add `StructuredLogger` helper (wraps `Log`) in `multitenancy_functions.py`
- [x] 1.2.2 Emit JSON lines to `/var/log/wo/multitenancy.json`
- [x] 1.2.3 Add correlation IDs for multi-step operations (create, update, baseline apply)
- [x] 1.2.4 Add `duration_ms` to operation-complete log entries
- [x] 1.2.5 Ship `/etc/logrotate.d/wo-multitenancy` for log rotation
- [x] 1.2.6 Document the log schema in project docs

### 1.3 JSON Output for Read Commands
- [x] 1.3.1 Add `--json` argument to `WOMultitenancyController.Meta.arguments`
- [x] 1.3.2 Create `JsonOutput` helper for consistent formatting
- [x] 1.3.3 Add JSON output to `list`
- [x] 1.3.4 Add JSON output to `status`
- [x] 1.3.5 Add JSON output to `baseline`
- [x] 1.3.6 Emit JSON result objects from `create` and `delete` when `--json` is set
- [x] 1.3.7 Document JSON schemas per command

---

## Phase 2: Accountability

### 2.1 Site Tagging
- [x] 2.1.1 Add `tags` column to `multitenancy_sites` with migration for existing installs
- [x] 2.1.2 Add `--tags` argument to `create`
- [x] 2.1.3 Implement `MTDatabase.get_sites_by_tags()` and `update_site_tags()`
- [x] 2.1.4 Add `--tags` filter to `list`, `update`, `baseline apply`
- [x] 2.1.5 Validate tag format (alphanumeric + hyphen, comma-separated)
- [x] 2.1.6 Document tagging patterns in user docs

### 2.2 Audit Logging
- [x] 2.2.1 Create `multitenancy_audit` table in database
- [x] 2.2.2 Implement `AuditLogger` class with SHA-256 checksum for tamper detection
- [x] 2.2.3 Wire audit logging into: `create`, `delete`, `update`, `rollback`, `baseline apply`, `shared-config` ops
- [x] 2.2.4 Implement `audit` subcommand with `--since`, `--action`, `--target`, `--format=json|csv`
- [x] 2.2.5 Enforce retention policy (default 90 days, configurable) on each audit write
- [x] 2.2.6 Write unit tests covering record shape, checksum, and retention pruning

---

## Phase 3: Operations

### 3.1 Maintenance Mode
- [x] 3.1.1 Create maintenance-page HTML template
- [x] 3.1.2 Implement nginx maintenance include (return 503 with `Retry-After`)
- [x] 3.1.3 Add `maintenance` subcommand (`--enable|--disable`, `--site|--all`, `--message`)
- [x] 3.1.4 Support admin-IP bypass via config
- [x] 3.1.5 Emit `maintenance_enabled` / `maintenance_disabled` events (audit + webhook)

### 3.2 Webhook Notifications
- [x] 3.2.1 Add `[webhooks]` section to config parser
- [x] 3.2.2 Implement `WebhookNotifier` using stdlib `urllib.request`
- [x] 3.2.3 Define stable webhook payload schema (JSON) and document it
- [x] 3.2.4 HMAC-SHA256 signing with `X-WO-Signature` header when `secret` is set
- [x] 3.2.5 Retry with exponential backoff (max 3 attempts, 5s timeout per attempt)
- [x] 3.2.6 Wire notifications into: `site_created`, `site_deleted`, `update_completed`, `rollback_triggered`, `baseline_applied`, `maintenance_enabled`, `maintenance_disabled`
- [x] 3.2.7 Add `wo multitenancy webhook test` helper for verifying integration
- [x] 3.2.8 Ensure webhook failure never fails the originating operation

---

## Documentation

- [x] 4.1 Update `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md` with new subcommands and flags
- [x] 4.2 Document webhook payload schema (one example per event)
- [x] 4.3 Document structured log fields and example Loki / ELK query snippets
- [x] 4.4 Document audit retention configuration and how to query common questions ("who deleted site X?", "what changed today?")

---

## Testing

- [x] 5.1 Unit tests for each health checker
- [x] 5.2 Unit tests for `AuditLogger` (record shape, checksum, retention pruning)
- [x] 5.3 Unit tests for `WebhookNotifier` (signing, retries, graceful failure)
- [x] 5.4 Integration test: full `health` output against a populated test fixture
- [x] 5.5 Integration test: maintenance enable → HTTP 503 → disable round-trip
- [x] 5.6 Integration test: audit entries survive a simulated DB restart

---

## Milestones

| Phase | Key Deliverables |
|-------|-----------------|
| Phase 1 | Health check, structured JSON logs, `--json` everywhere |
| Phase 2 | Site tags, audit logging |
| Phase 3 | Maintenance mode, webhooks |
