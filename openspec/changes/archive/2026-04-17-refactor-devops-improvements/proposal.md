# Change: DevOps Improvements for WordOps Multi-tenancy Plugin

## Why

The plugin's core functionality is solid — atomic deployments, rollback, baseline management, git-versioned shared config. What operators lack is the day-to-day observability and accountability layer: there is no one-command health snapshot, logs are human-formatted only, and privileged operations leave no queryable trail.

This proposal adds the minimum DevOps primitives that pay off immediately on a single-server, small-fleet install — the plugin's actual target. It deliberately stops short of enterprise capabilities (pluggable secrets backends, cross-region DR, canary rollouts, integrated backup) because those assume an operating model the plugin doesn't have, and several of them reinvent tools that already exist upstream or in the wider ecosystem.

---

## What Changes

### New subcommands
- `wo multitenancy health [--json] [--site=<domain>]` — Health snapshot across shared infrastructure, database, sites, disk, PHP-FPM, and nginx.
- `wo multitenancy audit [--since=<duration>] [--action=<name>] [--target=<domain>] [--format=json|csv]` — Query the privileged-operation history.
- `wo multitenancy maintenance --enable|--disable [--site=<domain>|--all] [--message=<text>]` — Toggle a 503 maintenance page with optional admin-IP bypass.

### New flags on existing commands
- `--json` on read commands (`list`, `status`, `baseline`) for machine-readable output.
- `--tags=<csv>` on `create` and as a filter on `list`, `update`, `baseline apply` for selective operations.
- `--dry-run` and `--verbose` on `baseline apply` for safer baseline changes.

### New configuration sections
- `[webhooks]` — HTTP POST notifications for key events, HMAC-SHA256 signed.
- `[logging]` — Structured JSON log location and retention.

### Database schema extensions
- `multitenancy_sites.tags` (TEXT) — Comma-separated tag list.
- `multitenancy_audit` table — Privileged-operation history with SHA-256 checksum for tamper detection.

---

## Non-Goals (Explicitly Out of Scope)

The original investigation surfaced additional capabilities that are **not** part of this change. They are listed here so the rationale is preserved and future proposals can pick them up with context:

| Cut capability | Rationale |
|---|---|
| **Pluggable secrets backends (Vault, AWS Secrets Manager, encrypted file)** | The plugin manages a single server and currently holds one meaningful secret (a GitHub token). A three-provider abstraction is speculative. Keep env vars; add a single encrypted-file option only if real need appears. |
| **Integrated backup/restore (S3, AES-256-GCM, point-in-time)** | WordOps already ships `wo backup` / `wo site backup`. Reinventing backup inside this plugin duplicates upstream work and competes with mature tools (`restic`, `borg`, `duplicity`). Document a workflow using existing primitives instead. |
| **Disaster Recovery mode (failover / failback)** | The plugin's single-server architecture makes cross-server DR a different product, not a feature. |
| **Canary deployments** | Only justified above ~50 sites. Site tagging (in scope) already lets operators run a manual staged rollout when they need one. Revisit when real fleet size demands it. |
| **Environment-specific config overlays (`--env=staging`)** | Shared config is already git-versioned, which gives the same override story with less surface area. |
| **Performance benchmarks, auto-scaling hooks, multi-region** | External observability platforms (Prometheus/Grafana) and capacity planning belong outside a provisioning CLI. |
| **SIEM audit export (CEF/LEEF), cryptographic log signing, off-box archival** | A SQLite audit table with per-record checksum is enough for the target deployment. SIEM integration is a separate, larger concern. |

These may become standalone proposals later if fleet size or operating constraints change.

---

## Impact

### Affected specs
- `multitenancy` — Additive extensions only. One MODIFIED requirement (`Baseline Apply`) to cover tag filtering, dry-run, and verbose.

### Affected code
- `wo/cli/plugins/multitenancy.py` — New subcommands (`health`, `audit`, `maintenance`), new flags (`--json`, `--tags`, `--dry-run`, `--verbose`).
- `wo/cli/plugins/multitenancy_functions.py` — `AuditLogger`, `WebhookNotifier`, `StructuredLogger` helpers.
- `wo/cli/plugins/multitenancy_db.py` — `tags` column on `multitenancy_sites`; new `multitenancy_audit` table.
- New file: `wo/cli/plugins/multitenancy_health.py` — Composable `HealthChecker`.
- `config/plugins.d/multitenancy.conf` — `[webhooks]`, `[logging]` sections.

### Breaking changes
**None.** All changes are additive. Rollback is achieved by disabling the new config sections.

### Dependencies
No new mandatory dependencies. Webhook notifier uses stdlib `urllib.request`; structured logging uses stdlib `json` + `logging`; audit checksum uses stdlib `hashlib`.

---

## Implementation Phases

### Phase 1 — Visibility (Weeks 1–2)
- Health check command with `--json` and `--site` filter
- Structured JSON logging to `/var/log/wo/multitenancy.json` with correlation IDs and `duration_ms`
- `--json` output on existing read commands

### Phase 2 — Accountability (Weeks 3–4)
- Site tagging (`tags` column, `--tags` flag and filters)
- Audit logging (SQLite-backed, checksummed, queryable via `audit` subcommand)

### Phase 3 — Operations (Weeks 5–6)
- Maintenance mode (per-site and `--all`, custom message, admin-IP bypass)
- Webhook notifications (HMAC-signed, retry with backoff)

Total scope: ~6 weeks for one developer, vs. ~12 weeks in the original comprehensive proposal.

---

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Time to triage a production issue | Ad-hoc SSH + log grep | One command (`wo multitenancy health`) |
| Record of who did what, when | None | Queryable audit log with retention |
| Automation / CI-CD integration | Parse human text | JSON output + signed webhooks |

---

## References

- [WordOps Documentation](https://docs.wordops.net/)
- Existing plugin modules: `wo/cli/plugins/multitenancy*.py`
- Current baseline config: `config/plugins.d/multitenancy.conf`
