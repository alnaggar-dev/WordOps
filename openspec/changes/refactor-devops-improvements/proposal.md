# Change: DevOps Improvements for WordOps Multi-tenancy Plugin

## Executive Summary

This proposal identifies critical gaps in the WordOps Multi-tenancy Plugin from an enterprise DevOps perspective and proposes targeted improvements to enhance monitoring, automation, security, and operational resilience.

---

## Why

The current plugin provides excellent multi-tenancy functionality but lacks enterprise-grade DevOps capabilities required for production environments at scale. Organizations running 50+ sites need:

1. **Proactive monitoring** instead of reactive troubleshooting
2. **Automation APIs** for CI/CD integration
3. **Enhanced security** with secrets management
4. **Operational resilience** with better DR capabilities
5. **Compliance support** for audit requirements

---

## Current State Analysis

### Strengths (What's Working Well)

| Area | Current Implementation | Rating |
|------|----------------------|--------|
| **Atomic Deployments** | Release-based structure with instant symlink switching | Excellent |
| **Rollback** | Single-command rollback via `wo multitenancy rollback` | Excellent |
| **Baseline Management** | Git-tracked changes, version control, auto-propagation | Excellent |
| **Cache Management** | Global cache clearing (~2s regardless of site count) | Excellent |
| **Nginx Integration** | Modular includes, no custom templates needed | Excellent |
| **SSL Management** | Native WOAcme integration | Good |
| **Shared Config** | Centralized wp-config with dry-run, rollback | Good |

### Gaps Identified (Priority Order)

---

## Gap 1: Monitoring & Observability (CRITICAL)

### Current State
- No health check endpoints
- No metrics collection
- Email-only alerting (basic)
- No structured logging
- No distributed tracing

### Impact
- **Incident Response**: Average MTTR is hours instead of minutes
- **Capacity Planning**: No data for scaling decisions
- **SLA Compliance**: Cannot prove uptime metrics
- **Root Cause Analysis**: Manual log correlation required

### Proposed Solution

#### 1.1 Health Check Endpoint
```bash
wo multitenancy health [--json] [--site=<domain>]
```

Returns:
```json
{
  "status": "healthy|degraded|unhealthy",
  "timestamp": "2025-01-15T10:30:00Z",
  "checks": {
    "shared_infrastructure": {"status": "ok", "current_release": "wp-20250115-100000"},
    "database": {"status": "ok", "connection_time_ms": 2},
    "sites": {"total": 50, "healthy": 48, "degraded": 2, "unhealthy": 0},
    "disk_space": {"status": "ok", "available_gb": 45.2, "threshold_gb": 5},
    "php_fpm": {"status": "ok", "active_pools": ["php83"]},
    "nginx": {"status": "ok", "worker_connections": 1024}
  }
}
```

#### 1.2 Structured Logging
```python
# Current
Log.info(self, "Created site: example.com")

# Proposed (JSON structured)
Log.info(self, "site_created", {
    "domain": "example.com",
    "php_version": "8.3",
    "cache_type": "wpfc",
    "baseline_version": 8,
    "duration_ms": 4500
})
```

Output to `/var/log/wo/multitenancy.json`:
```json
{"timestamp":"2025-01-15T10:30:00Z","level":"info","event":"site_created","domain":"example.com","php_version":"8.3","cache_type":"wpfc","baseline_version":8,"duration_ms":4500}
```

---

## Gap 2: Automation & Notifications (HIGH)

### Current State
- CLI-only interface
- No webhook support
- Scripts must parse CLI output

### Impact
- **Automation**: Scripts must parse CLI output
- **Event-Driven**: No way to trigger actions on deployments
- **Notifications**: No automated alerts for operations

### Proposed Solution

#### 2.1 Webhook Notifications
```ini
# /etc/wo/plugins.d/multitenancy.conf
[webhooks]
enabled = true
url = https://slack.example.com/webhook/xxx
events = site_created,site_deleted,update_completed,rollback_triggered,baseline_applied
secret = webhook_signing_secret
```

#### 2.2 Machine-Readable Output
```bash
# Existing commands gain --json flag
wo multitenancy list --json
wo multitenancy status --json
wo multitenancy baseline --json
```

---

## Gap 3: Security Enhancements (HIGH)

### Current State
- GitHub tokens in environment variables
- No secrets rotation
- Basic file permissions
- No WAF integration docs
- No audit logging to SIEM

### Impact
- **Compliance**: Cannot pass SOC2/PCI audits
- **Breach Risk**: Hardcoded secrets vulnerable
- **Forensics**: Insufficient audit trail

### Proposed Solution

#### 3.1 Secrets Management Integration
```ini
# /etc/wo/plugins.d/multitenancy.conf
[secrets]
provider = vault|aws_secrets|file
# Vault
vault_addr = https://vault.example.com:8200
vault_path = secret/data/wordops/multitenancy

# AWS Secrets Manager
aws_secret_name = wordops/multitenancy
aws_region = us-east-1

# File (encrypted)
secrets_file = /etc/wo/secrets.enc
encryption_key_env = WO_SECRETS_KEY
```

Supported secrets:
- `github_token` - GitHub API access
- `webhook_secret` - Webhook signing
- `api_token` - REST API authentication
- `db_password` - MySQL root (for new sites)

#### 3.2 Audit Logging
```bash
wo multitenancy audit [--since=24h] [--format=json|csv]
```

Logs all privileged operations:
```json
{
  "timestamp": "2025-01-15T10:30:00Z",
  "actor": "root",
  "action": "site_created",
  "target": "example.com",
  "source_ip": "192.168.1.100",
  "result": "success",
  "details": {"php_version": "8.3", "cache_type": "wpfc"}
}
```

---

## Gap 4: Disaster Recovery & Backup (HIGH)

### Current State
- Manual backup procedures
- No automated restore
- Single-server architecture
- No cross-region support

### Impact
- **RTO**: Hours instead of minutes
- **RPO**: Undefined data loss window
- **Availability**: Single point of failure

### Proposed Solution

#### 4.1 Integrated Backup
```bash
wo multitenancy backup [--site=<domain>|--all] [--destination=s3|local|rsync]
```

```ini
# /etc/wo/plugins.d/multitenancy.conf
[backup]
enabled = true
schedule = 0 2 * * *  # Daily at 2 AM
retention_days = 30
destination = s3://bucket/wordops-backups
include_uploads = true
include_database = true
encryption = aes-256-gcm
```

#### 4.2 Restore Command
```bash
wo multitenancy restore --site=example.com --from=s3://bucket/backup.tar.gz [--point-in-time=2025-01-14T23:00:00]
```

#### 4.3 Disaster Recovery Mode
```bash
wo multitenancy dr --status
wo multitenancy dr --failover --target=secondary-server
wo multitenancy dr --failback
```

---

## Gap 5: Deployment Strategies (MEDIUM)

### Current State
- All-or-nothing updates
- Staging site testing (manual)
- No gradual rollout

### Impact
- **Risk**: Bad updates affect all sites simultaneously
- **Validation**: Cannot test on subset of production

### Proposed Solution

#### 5.1 Canary Deployments
```bash
wo multitenancy update --canary=10%  # Update 10% of sites first
wo multitenancy update --canary-promote  # Promote to remaining sites
wo multitenancy update --canary-rollback  # Rollback canary sites
```

#### 5.2 Site Groups/Tags
```bash
wo multitenancy create example.com --php83 --wpfc --tags=production,client-a
wo multitenancy update --tags=staging  # Update only staging sites
wo multitenancy baseline apply --tags=production
```

```ini
# Site tagging in database
multitenancy_sites.tags = "production,client-a,region-us"
```

#### 5.3 Maintenance Mode
```bash
wo multitenancy maintenance --enable [--site=<domain>|--all] [--message="Back soon"]
wo multitenancy maintenance --disable [--site=<domain>|--all]
```

---

## Gap 6: Performance & Scaling (MEDIUM)

### Current State
- No performance baselines
- No auto-scaling
- Single-server design

### Impact
- **Capacity**: Unknown headroom
- **Scaling**: Manual intervention required
- **Performance**: No degradation alerts

### Proposed Solution

#### 6.1 Performance Baselines
```bash
wo multitenancy benchmark [--site=<domain>|--all]
```

Measures:
- Page load time (TTFB)
- Database query time
- Cache hit ratio
- PHP-FPM pool utilization

#### 6.2 Resource Monitoring Integration
```ini
[monitoring]
cpu_alert_threshold = 80
memory_alert_threshold = 85
disk_alert_threshold = 90
response_time_alert_ms = 500
```

---

## Gap 7: Configuration Management (MEDIUM)

### Current State
- Single environment config
- No environment-specific overrides
- No config validation pre-apply

### Impact
- **Staging/Production Drift**: Hard to maintain differences
- **Errors**: Invalid configs can break sites

### Proposed Solution

#### 7.1 Environment-Specific Config
```
/etc/wo/plugins.d/
├── multitenancy.conf           # Base config
├── multitenancy.staging.conf   # Staging overrides
└── multitenancy.production.conf # Production overrides
```

```bash
wo multitenancy --env=staging create example.com --php83
```

#### 7.2 Config Validation
```bash
wo multitenancy config validate [--file=/path/to/config]
wo multitenancy baseline apply --dry-run --verbose
```

---

## What Changes

### New Commands
- `wo multitenancy health` - Health check endpoint
- `wo multitenancy audit` - Audit log viewer
- `wo multitenancy backup` - Backup management
- `wo multitenancy restore` - Restore from backup
- `wo multitenancy maintenance` - Maintenance mode
- `wo multitenancy benchmark` - Performance testing

### New Configuration Sections
- `[webhooks]` - Webhook notifications
- `[secrets]` - Secrets management
- `[backup]` - Backup configuration
- `[monitoring]` - Alerting thresholds

### New Flags for Existing Commands
- `--json` - Machine-readable output (all commands)
- `--tags` - Site grouping (create, update, baseline apply)
- `--canary` - Gradual rollout (update)
- `--dry-run` - Preview changes (baseline apply)
- `--env` - Environment selection (all commands)

### Database Schema Extensions
```sql
-- Site tags
ALTER TABLE multitenancy_sites ADD COLUMN tags TEXT;

-- Audit log
CREATE TABLE multitenancy_audit (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    actor VARCHAR(255),
    action VARCHAR(100),
    target VARCHAR(255),
    source_ip VARCHAR(45),
    result VARCHAR(50),
    details TEXT
);

-- Backup history
CREATE TABLE multitenancy_backups (
    id INTEGER PRIMARY KEY,
    site_domain VARCHAR(255),
    backup_path TEXT,
    backup_type VARCHAR(50),
    size_bytes INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    retention_until DATETIME
);
```

---

## Impact

### Affected Specs
- `multitenancy` - Core multi-tenancy capability (extensive additions)

### Affected Code
- `multitenancy.py` - New controller methods
- `multitenancy_functions.py` - New utility functions
- `multitenancy_db.py` - New tables and queries
- New file: `multitenancy_health.py` - Health checks
- New file: `multitenancy_backup.py` - Backup/restore

### Breaking Changes
- **NONE** - All changes are additive

### Dependencies
- Optional: `boto3` (S3 backup)
- Optional: `hvac` (HashiCorp Vault)
- Optional: `cryptography` (backup encryption)

---

## Implementation Phases

### Phase 1: Foundation
- Health check command
- Structured logging
- `--json` flag for all commands
- Site tagging

### Phase 2: Audit & Notifications
- Audit logging
- Webhook notifications

### Phase 3: Security
- Secrets management integration
- Enhanced audit trail

### Phase 4: Operations
- Backup/restore commands
- Maintenance mode
- Canary deployments

---

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| MTTR (Mean Time to Recovery) | ~2 hours | <15 minutes |
| Deployment Confidence | Manual testing | Automated health checks |
| Audit Compliance | None | Full audit trail |
| Backup RTO | Unknown | <30 minutes |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Backup storage costs | Medium | Retention policies, compression |
| Complexity increase | Medium | Phased rollout, feature flags |
| Performance overhead | Low | Optional components, efficient implementations |

---

## Open Questions

1. **Secrets Provider Priority**: Which provider to implement first?
2. **Backup Destination**: S3-compatible only, or also support GCS/Azure?

---

## References

- [HashiCorp Vault Integration](https://www.vaultproject.io/docs/secrets/kv)
- [WordOps Documentation](https://docs.wordops.net/)
- [WordPress Security Best Practices](https://developer.wordpress.org/plugins/security/)
