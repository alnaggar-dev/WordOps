# Priority Roadmap: DevOps Improvements

## Recommended Implementation Order

```
Week 1-2:  Health Check → JSON Output → Structured Logging
Week 3-4:  Site Tagging → Audit Logging
Week 5-6:  Maintenance Mode → Webhook Notifications
```

Total scope: ~6 weeks for one developer.

---

## Priority 1: Visibility (Weeks 1–2)
**Theme: Know what's happening right now**

### 1.1 Health Check Command
**Why first:**
- Zero new dependencies
- Immediate troubleshooting value
- Foundation for later monitoring integrations
- ~200 lines of code

**Deliverables:**
```bash
wo multitenancy health              # Human-readable
wo multitenancy health --json       # Machine-readable
wo multitenancy health --site=X     # Single site
```

**Implementation time:** 2–3 days

---

### 1.2 JSON Output for Read Commands
**Why second:**
- Automation scripts stop parsing human text
- Simple flag addition built on existing command handlers

**Deliverables:**
```bash
wo multitenancy list --json
wo multitenancy status --json
wo multitenancy baseline --json
```

**Implementation time:** 2–3 days

---

### 1.3 Structured Logging
**Why third:**
- Immediate debugging value for multi-step operations
- Loki / ELK / Datadog ingestion becomes trivial
- No external dependencies — stdlib `json` + `logging`

**Deliverables:**
- JSON lines to `/var/log/wo/multitenancy.json`
- Correlation IDs per operation
- `duration_ms` on completion
- `logrotate` policy file

**Implementation time:** 2 days

---

## Priority 2: Accountability (Weeks 3–4)
**Theme: Know who did what**

### 2.1 Site Tagging
**Why:**
- Unlocks selective operations (`--tags=staging`, `--tags=production`)
- One-column schema change, trivial to implement
- Gives operators the manual-staged-rollout story without canary machinery

**Deliverables:**
```bash
wo multitenancy create example.com --tags=production,client-a
wo multitenancy list --tags=staging
wo multitenancy baseline apply --tags=production
```

**Implementation time:** 2–3 days

---

### 2.2 Audit Logging
**Why:**
- Answers "who did what, when" after an incident
- Self-contained, no external dependencies
- Tamper-evident via per-record SHA-256 checksum

**Deliverables:**
```bash
wo multitenancy audit --since=24h
wo multitenancy audit --action=site_created
wo multitenancy audit --format=json --since=7d
```

**Implementation time:** 3–4 days

---

## Priority 3: Operations (Weeks 5–6)
**Theme: Safe operations and automation**

### 3.1 Maintenance Mode
**Why:**
- Prevents user-visible errors during deploys and restores
- Trivial to ship (nginx include + small controller method)
- Composes with webhooks so ops channels see the start/end

**Deliverables:**
```bash
wo multitenancy maintenance --enable --site=example.com
wo multitenancy maintenance --enable --all --message="Back in 10 minutes"
wo multitenancy maintenance --disable --site=example.com
```

**Implementation time:** 2–3 days

---

### 3.2 Webhook Notifications
**Why:**
- Slack / Discord alerts, CI/CD triggers
- Stdlib-only implementation (`urllib` + `hmac`)
- Graceful failure — never blocks the originating operation

**Deliverables:**
```ini
[webhooks]
url = https://hooks.slack.com/services/xxx
secret = <signing secret>
events = site_created,update_completed,rollback_triggered,maintenance_enabled
```

**Implementation time:** 2–3 days

---

## Out of Scope (Explicit)

The following were evaluated and cut — see `proposal.md` Non-Goals for rationale:

- Pluggable secrets backends (Vault / AWS Secrets Manager)
- Integrated backup / restore (S3, AES-256-GCM, point-in-time)
- Disaster Recovery mode (multi-server failover / failback)
- Canary deployments
- Environment-specific config overlays
- Performance benchmarks / auto-scaling / multi-region
- SIEM audit export (CEF / LEEF) and cryptographic log signing

These may return as standalone proposals if fleet size or operating constraints change.

---

## Dependencies Between Features

```
Health Check
      │
      ▼
Structured Logging ──► JSON Output
                            │
                            ▼
Site Tagging ──► Audit Logging ──► Webhooks
                      │
                      ▼
                 Maintenance Mode
```

---

## Minimum Viable DevOps (MVP)

If scope has to shrink further, ship these three first:

1. **Health Check** — know when things break
2. **Structured Logging** — correlate across operations
3. **Audit Logging** — know what caused the break

These three provide the observability foundation in under a week of engineering.
