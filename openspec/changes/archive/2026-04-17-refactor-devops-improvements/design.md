# Design: DevOps Improvements Architecture

## Context

The WordOps Multi-tenancy Plugin's core functionality (atomic releases, rollback, git-versioned baseline, shared config) is solid. This document covers the technical design of a focused set of DevOps primitives — health checking, structured logging, machine-readable output, site tagging, audit logging, maintenance mode, and webhooks — added without changing existing behavior.

Out-of-scope features (secrets backends, integrated backup, DR failover, canary deployments, environment overlays, SIEM export) are called out in the proposal's Non-Goals section and are not designed here.

### Constraints

- Python 3.8+ (Ubuntu 20.04 compatibility)
- Cannot modify WordOps core files
- SQLite (reuse the existing WordOps DB)
- Single-server architecture
- No new mandatory dependencies — stdlib only for webhooks and logging

### Stakeholders

- **Site operators** — want health visibility, maintenance mode, audit trail
- **Automation / CI-CD** — want JSON output and signed webhooks
- **Small-team ops** — want accountability without running a full observability stack

---

## Goals / Non-Goals

### Goals
- Health checking and structured logging for incident response
- JSON output and webhooks for automation
- Audit trail for privileged operations, with tamper detection
- Maintenance mode for safe deploys
- Site tagging for selective operations
- Zero breaking changes

### Non-Goals
- Pluggable secrets backends (Vault, AWS Secrets Manager, multi-provider abstraction)
- Integrated backup/restore — use existing WordOps commands + external tools
- Cross-server DR, canary rollouts, performance benchmarks
- Environment-specific config overlays (git-versioned shared config already covers this)
- SIEM export formats, cryptographic log signing, off-box audit archival
- GUI / dashboard, multi-server/cluster support, real-time streaming logs

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                   WordOps Multi-tenancy Plugin                       │
├─────────────────────────────────────────────────────────────────────┤
│  CLI Layer (multitenancy.py)                                         │
│  ┌──────────┬──────────┬──────────────┬──────────┬──────────┐       │
│  │  health  │  audit   │ maintenance  │   list   │  create  │  ...  │
│  └────┬─────┴────┬─────┴──────┬───────┴────┬─────┴────┬─────┘       │
├───────┼──────────┼────────────┼────────────┼──────────┼──────────────┤
│  Service Layer                                                       │
│  ┌────┴─────┐┌───┴────┐┌──────┴─────┐┌─────┴────┐┌────┴────┐        │
│  │ Health   ││ Audit  ││ Webhook    ││Structured││  JSON   │        │
│  │ Checker  ││ Logger ││ Notifier   ││  Logger  ││ Output  │        │
│  └────┬─────┘└───┬────┘└──────┬─────┘└─────┬────┘└────┬────┘        │
├───────┼──────────┼────────────┼────────────┼──────────┼──────────────┤
│  Core Layer (existing)                                               │
│  ┌────┴──────────┴────────────┴────────────┴──────────┴────┐        │
│  │            MTFunctions / MTDatabase                       │        │
│  │              SharedInfrastructure                         │        │
│  └───────────────────────────────────────────────────────────┘        │
├─────────────────────────────────────────────────────────────────────┤
│  Storage                                                             │
│  ┌──────────────────────────┐   ┌─────────────────────────────────┐ │
│  │  SQLite                  │   │ /var/log/wo/multitenancy.json   │ │
│  │  (multitenancy_audit,    │   │ (structured JSON log, rotated   │ │
│  │   multitenancy_sites)    │   │  via logrotate)                 │ │
│  └──────────────────────────┘   └─────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Decisions

### Decision 1: Module Organization

**Decision**: Add one new module (`multitenancy_health.py`) and extend the three existing modules. No new package.

```
wo/cli/plugins/
├── multitenancy.py              # CLI controller — new subcommands and flags
├── multitenancy_functions.py    # Core — add AuditLogger, WebhookNotifier, StructuredLogger
├── multitenancy_db.py           # DB — add `tags` column, multitenancy_audit table
└── multitenancy_health.py       # NEW — composable HealthChecker
```

**Rationale**:
- Keeps the plugin easy to reason about — four files instead of eight.
- `HealthChecker` gets its own file because it is the most likely thing to grow over time.
- Audit, webhook, and structured-log helpers live next to the functions they instrument (in `multitenancy_functions.py`), which is already the center of gravity for business logic.

**Alternatives considered**:
- Separate module per concern (audit/webhook/logging/secrets/backup) — unnecessary fragmentation at this scope; was a driver of the original proposal's complexity.
- Single file — `multitenancy_functions.py` is already sizeable.

---

### Decision 2: Health Check Architecture

**Decision**: Composable checker functions registered into a single `HealthChecker`.

```python
class HealthChecker:
    def __init__(self, app):
        self.app = app
        self.checkers = []

    def register(self, name, checker_func, critical=True):
        self.checkers.append({
            'name': name,
            'func': checker_func,
            'critical': critical,
        })

    def run_all(self) -> dict:
        results = {
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'checks': {},
        }
        for checker in self.checkers:
            try:
                check_result = checker['func']()
                results['checks'][checker['name']] = check_result
                if check_result.get('status') != 'ok':
                    if checker['critical']:
                        results['status'] = 'unhealthy'
                    elif results['status'] == 'healthy':
                        results['status'] = 'degraded'
            except Exception as e:
                results['checks'][checker['name']] = {
                    'status': 'error',
                    'error': str(e),
                }
                if checker['critical']:
                    results['status'] = 'unhealthy'
        return results
```

Built-in checkers: shared infrastructure (symlink / release), database (MySQL ping + timing), disk space, PHP-FPM pool, nginx service, and per-site WordPress accessibility.

**Rationale**:
- Extensible — adding a new checker means one small function, no controller changes.
- Each checker is a unit-testable pure function.
- Consistent envelope across all checks makes JSON output and alerting trivial.

---

### Decision 3: Audit Logging Storage

**Decision**: SQLite table with a SHA-256 checksum per record for tamper detection. Retention enforced opportunistically on write.

```sql
CREATE TABLE multitenancy_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    event_id VARCHAR(36),      -- correlation UUID (shared with structured log)
    actor VARCHAR(255),        -- username or "system"
    actor_ip VARCHAR(45),      -- SSH_CLIENT if present, else "local"
    action VARCHAR(100),       -- e.g. site_created
    target VARCHAR(255),       -- e.g. example.com
    target_type VARCHAR(50),   -- site | baseline | config
    result VARCHAR(50),        -- success | failure | error
    duration_ms INTEGER,
    details TEXT,              -- JSON extra data
    checksum VARCHAR(64)       -- SHA-256 of canonicalized record
);

CREATE INDEX idx_audit_timestamp ON multitenancy_audit(timestamp);
CREATE INDEX idx_audit_actor     ON multitenancy_audit(actor);
CREATE INDEX idx_audit_action    ON multitenancy_audit(action);
CREATE INDEX idx_audit_target    ON multitenancy_audit(target);
```

`AuditLogger` writes one row per privileged operation and correlates it to structured-log lines via shared `event_id`. Retention (default 90 days, configurable) is pruned lazily on each write — no cron, no systemd timer.

**Rationale**:
- SQLite is already a plugin dependency.
- One table, four indexes — fits cleanly into existing schema management.
- Checksum covers the "did someone tamper with the audit table" concern without requiring an external log sink.

**Non-goal (explicit)**: SIEM export formats (CEF/LEEF), cryptographic log signing, off-box archival — all are separate concerns that can be layered on later if compliance requirements appear. The checksum gives us tamper *detection*; anything beyond that is a different proposal.

---

### Decision 4: Webhook Delivery

**Decision**: Synchronous HTTP POST via stdlib `urllib.request`, HMAC-SHA256 signed when a secret is configured, retried with exponential backoff (3 attempts max, 5-second timeout each). Failure never fails the originating operation.

```python
class WebhookNotifier:
    def notify(self, event: str, payload: dict):
        if event not in self.enabled_events:
            return
        body = json.dumps({
            'event': event,
            'timestamp': datetime.utcnow().isoformat(),
            'data': payload,
        }).encode()
        headers = {'Content-Type': 'application/json'}
        if self.secret:
            sig = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
            headers['X-WO-Signature'] = f'sha256={sig}'
        for attempt in range(3):
            try:
                urllib.request.urlopen(
                    urllib.request.Request(self.url, data=body, headers=headers),
                    timeout=5,
                )
                return
            except Exception:
                time.sleep(2 ** attempt)
        Log.warn(self.app, f'Webhook for {event} failed after 3 attempts')
```

**Rationale**:
- Stdlib-only — no new mandatory dependency (`requests` is tempting but unjustified here).
- Synchronous is acceptable for CLI operations that already take seconds.
- Graceful failure: a broken Slack endpoint must never prevent a site from being created.

---

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| Webhook endpoint latency blocks the CLI | Low | 5s timeout per attempt; failure logs a warning and continues |
| Audit table growth | Low | Configurable retention (default 90 days), four indexes for query speed |
| Structured log schema churn | Low | Document schema in code; add `schema_version` to each record |
| Synchronous logging on the hot path | Low | JSON log write is a single line; if it becomes a bottleneck, promote to `QueueHandler` |

---

## Migration Plan

### Rollout (non-breaking)
1. **Phase 1**: Ship `multitenancy_health.py`, structured logger helper, `--json` flag. No DB change.
2. **Phase 2**: DB migration adds the `tags` column and the `multitenancy_audit` table. Existing installs get them on next `wo multitenancy` invocation via `MTDatabase.initialize_tables`.
3. **Phase 3**: New `maintenance` subcommand and `[webhooks]` config section — both are no-ops until explicitly enabled.

### Rollback
All changes are additive. Operators disable by removing the new config sections; the audit table and `tags` column can remain unused without side effects.

---

## Open Questions

1. **Structured-log rotation**: ship `/etc/logrotate.d/wo-multitenancy` (leaning toward yes — simpler, matches WordOps conventions) or use Python's `RotatingFileHandler`?
2. **Audit retention default**: 90 days feels right for small-team ops hygiene. Confirm with real operators before coding the default.
