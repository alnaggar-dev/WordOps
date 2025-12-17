# Design: DevOps Improvements Architecture

## Context

The WordOps Multi-tenancy Plugin currently provides excellent core functionality but lacks enterprise DevOps capabilities. This design document outlines the technical architecture for adding monitoring, automation, security, and operational features while maintaining:

1. **Backward compatibility** - No breaking changes to existing commands
2. **WordOps patterns** - Following native WordOps conventions
3. **Optional dependencies** - Advanced features don't break basic installation
4. **Simplicity** - Minimal complexity for each feature

### Constraints

- Must work with Python 3.8+ (Ubuntu 20.04 compatibility)
- Cannot modify WordOps core files
- SQLite database only (no external DB)
- Single-server architecture (multi-server is future scope)
- Optional features must gracefully degrade

### Stakeholders

- **Site Operators**: Need monitoring, alerting, backup/restore
- **DevOps Engineers**: Need CI/CD integration, automation APIs
- **Security Teams**: Need audit logs, secrets management, scanning
- **Developers**: Need structured logs, debugging tools

---

## Goals / Non-Goals

### Goals
- Add comprehensive health checking and monitoring
- Enable automation via APIs and webhooks
- Improve security posture with secrets management
- Provide backup/restore capabilities
- Support gradual deployments (canary)
- Maintain all existing functionality unchanged

### Non-Goals
- Multi-server/cluster support (future phase)
- Container/Kubernetes deployment (different architecture)
- Real-time streaming logs (too complex)
- Custom WordPress modifications (plugin scope only)
- GUI/dashboard (CLI and API only)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     WordOps Multi-tenancy Plugin                      │
├─────────────────────────────────────────────────────────────────────┤
│  CLI Layer (multitenancy.py)                                         │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────┐          │
│  │  health  │  metrics │  audit   │  backup  │   api    │          │
│  └────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┘          │
├───────┼──────────┼──────────┼──────────┼──────────┼─────────────────┤
│  Service Layer (new modules)                                          │
│  ┌────┴─────┐┌───┴────┐┌────┴────┐┌────┴────┐┌────┴────┐           │
│  │Monitoring││ Audit  ││Security ││ Backup  ││   API   │           │
│  │ Module   ││ Module ││ Module  ││ Module  ││ Server  │           │
│  └────┬─────┘└───┬────┘└────┬────┘└────┬────┘└────┬────┘           │
├───────┼──────────┼──────────┼──────────┼──────────┼─────────────────┤
│  Core Layer (existing)                                                │
│  ┌────┴──────────┴──────────┴──────────┴──────────┴────┐           │
│  │              MTFunctions / MTDatabase                 │           │
│  │                SharedInfrastructure                   │           │
│  └───────────────────────────────────────────────────────┘           │
├─────────────────────────────────────────────────────────────────────┤
│  Storage Layer                                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │   SQLite     │  │  Log Files   │  │   Backups    │               │
│  │   Database   │  │  (JSON/text) │  │  (S3/local)  │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Decisions

### Decision 1: Module Organization

**Decision**: Create separate Python modules for each capability domain.

**Structure**:
```
wo/cli/plugins/
├── multitenancy.py              # CLI controller (existing)
├── multitenancy_functions.py    # Core functions (existing)
├── multitenancy_db.py           # Database ops (existing)
├── multitenancy_health.py       # NEW: Health checks
├── multitenancy_audit.py        # NEW: Audit logging
├── multitenancy_backup.py       # NEW: Backup/restore
├── multitenancy_webhooks.py     # NEW: Webhook notifications
└── multitenancy_secrets.py      # NEW: Secrets management
```

**Rationale**:
- Clear separation of concerns
- Each module can be tested independently
- Optional features can be disabled by not loading module
- Follows WordOps plugin pattern

**Alternatives Considered**:
- Single file: Would become unwieldy (>3000 lines)
- Separate package: More complex, harder to install

---

### Decision 2: Health Check Architecture

**Decision**: Implement health checks as composable checker functions.

**Design**:
```python
# multitenancy_monitoring.py

class HealthChecker:
    """Composable health check system."""

    def __init__(self, app):
        self.app = app
        self.checkers = []

    def register(self, name, checker_func, critical=True):
        """Register a health check function."""
        self.checkers.append({
            'name': name,
            'func': checker_func,
            'critical': critical
        })

    def run_all(self) -> dict:
        """Run all registered health checks."""
        results = {
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'checks': {}
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
                    'error': str(e)
                }
                if checker['critical']:
                    results['status'] = 'unhealthy'

        return results

# Built-in checkers
def check_shared_infrastructure():
    """Check shared symlink and current release."""
    current = '/var/www/shared/current'
    if not os.path.islink(current):
        return {'status': 'error', 'message': 'Current symlink missing'}

    target = os.readlink(current)
    if not os.path.isdir(target):
        return {'status': 'error', 'message': f'Release {target} not found'}

    return {
        'status': 'ok',
        'current_release': os.path.basename(target)
    }

def check_database():
    """Check MySQL connectivity."""
    start = time.time()
    try:
        # Use WordOps MySQL check
        WOShellExec.cmd_exec(None, 'mysqladmin ping -s')
        return {
            'status': 'ok',
            'connection_time_ms': int((time.time() - start) * 1000)
        }
    except:
        return {'status': 'error', 'message': 'MySQL not responding'}

def check_disk_space(threshold_mb=1000):
    """Check available disk space."""
    stat = os.statvfs('/var/www')
    available_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)

    return {
        'status': 'ok' if available_mb > threshold_mb else 'warning',
        'available_mb': int(available_mb),
        'threshold_mb': threshold_mb
    }
```

**Rationale**:
- Extensible: Easy to add new health checks
- Configurable: Critical vs non-critical checks
- Consistent: All checks return same structure
- Testable: Each checker can be unit tested

---

### Decision 3: Audit Logging Storage

**Decision**: Store audit logs in SQLite with configurable retention.

**Schema**:
```sql
CREATE TABLE multitenancy_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    event_id VARCHAR(36),        -- UUID for correlation
    actor VARCHAR(255),          -- Username/system
    actor_ip VARCHAR(45),        -- IPv4/IPv6
    action VARCHAR(100),         -- Operation name
    target VARCHAR(255),         -- Domain/resource
    target_type VARCHAR(50),     -- site/baseline/config
    result VARCHAR(50),          -- success/failure/error
    duration_ms INTEGER,         -- Operation duration
    details TEXT,                -- JSON extra data
    checksum VARCHAR(64)         -- SHA-256 for integrity
);

CREATE INDEX idx_audit_timestamp ON multitenancy_audit(timestamp);
CREATE INDEX idx_audit_actor ON multitenancy_audit(actor);
CREATE INDEX idx_audit_action ON multitenancy_audit(action);
CREATE INDEX idx_audit_target ON multitenancy_audit(target);
```

**Implementation**:
```python
# multitenancy_audit.py

class AuditLogger:
    """Audit logging for privileged operations."""

    def __init__(self, app):
        self.app = app
        self.event_id = str(uuid.uuid4())

    def log(self, action, target, result='success', details=None):
        """Log an audit event."""
        record = {
            'event_id': self.event_id,
            'actor': self._get_actor(),
            'actor_ip': self._get_source_ip(),
            'action': action,
            'target': target,
            'target_type': self._infer_target_type(target),
            'result': result,
            'details': json.dumps(details) if details else None,
        }

        # Calculate checksum for integrity
        record['checksum'] = self._calculate_checksum(record)

        MTDatabase.insert_audit_log(self.app, record)

        # Also log to structured log file
        Log.info(self.app, f"AUDIT: {action} on {target} by {record['actor']}")

    def _get_actor(self):
        """Get current user."""
        return os.environ.get('USER', 'system')

    def _get_source_ip(self):
        """Get source IP if available."""
        return os.environ.get('SSH_CLIENT', '').split()[0] if 'SSH_CLIENT' in os.environ else 'local'

    def _calculate_checksum(self, record):
        """Calculate SHA-256 checksum for tamper detection."""
        data = json.dumps(record, sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()
```

**Rationale**:
- SQLite: Already used by WordOps, no new dependencies
- Indexed: Fast queries by common filters
- Integrity: Checksum for tamper detection
- Retention: Easy to implement cleanup

**Alternatives Considered**:
- File-based logs: Harder to query, no integrity
- External service: Adds complexity and dependency

---

### Decision 4: Secrets Management Architecture

**Decision**: Use provider pattern with pluggable backends.

**Design**:
```python
# multitenancy_secrets.py

from abc import ABC, abstractmethod

class SecretsProvider(ABC):
    """Abstract secrets provider."""

    @abstractmethod
    def get_secret(self, key: str) -> str:
        """Retrieve a secret by key."""
        pass

    @abstractmethod
    def set_secret(self, key: str, value: str) -> bool:
        """Store a secret."""
        pass


class EnvSecretsProvider(SecretsProvider):
    """Environment variable secrets (current behavior)."""

    def get_secret(self, key: str) -> str:
        env_key = f'WO_MT_{key.upper()}'
        return os.environ.get(env_key)

    def set_secret(self, key: str, value: str) -> bool:
        # Cannot persist env vars
        return False


class FileSecretsProvider(SecretsProvider):
    """Encrypted file secrets."""

    def __init__(self, file_path: str, encryption_key: str):
        self.file_path = file_path
        self.encryption_key = encryption_key
        self._cache = None

    def get_secret(self, key: str) -> str:
        secrets = self._load_secrets()
        return secrets.get(key)

    def set_secret(self, key: str, value: str) -> bool:
        secrets = self._load_secrets()
        secrets[key] = value
        return self._save_secrets(secrets)

    def _load_secrets(self):
        if self._cache is not None:
            return self._cache

        if not os.path.exists(self.file_path):
            return {}

        with open(self.file_path, 'rb') as f:
            encrypted = f.read()

        # Decrypt using Fernet (AES-128-CBC)
        from cryptography.fernet import Fernet
        fernet = Fernet(self.encryption_key)
        decrypted = fernet.decrypt(encrypted)

        self._cache = json.loads(decrypted)
        return self._cache


class VaultSecretsProvider(SecretsProvider):
    """HashiCorp Vault secrets."""

    def __init__(self, vault_addr: str, vault_token: str, vault_path: str):
        self.vault_addr = vault_addr
        self.vault_token = vault_token
        self.vault_path = vault_path
        self._client = None

    def get_secret(self, key: str) -> str:
        client = self._get_client()
        result = client.secrets.kv.v2.read_secret_version(
            path=self.vault_path,
            mount_point='secret'
        )
        return result['data']['data'].get(key)

    def _get_client(self):
        if self._client is None:
            import hvac
            self._client = hvac.Client(
                url=self.vault_addr,
                token=self.vault_token
            )
        return self._client


def get_secrets_provider(config: dict) -> SecretsProvider:
    """Factory function to create secrets provider."""
    provider_type = config.get('secrets', {}).get('provider', 'env')

    if provider_type == 'vault':
        return VaultSecretsProvider(
            config['secrets']['vault_addr'],
            os.environ.get('VAULT_TOKEN'),
            config['secrets']['vault_path']
        )
    elif provider_type == 'file':
        return FileSecretsProvider(
            config['secrets']['secrets_file'],
            os.environ.get(config['secrets']['encryption_key_env'])
        )
    else:
        return EnvSecretsProvider()
```

**Rationale**:
- Pluggable: Easy to add new providers
- Backward compatible: Env vars still work
- Secure: Encryption at rest for file provider
- Enterprise: Vault support for large deployments

---

### Decision 5: Backup Storage Architecture

**Decision**: Support multiple destinations with common interface.

**Design**:
```python
# multitenancy_backup.py

class BackupDestination(ABC):
    """Abstract backup destination."""

    @abstractmethod
    def upload(self, local_path: str, remote_path: str) -> bool:
        pass

    @abstractmethod
    def download(self, remote_path: str, local_path: str) -> bool:
        pass

    @abstractmethod
    def list(self, prefix: str) -> List[str]:
        pass

    @abstractmethod
    def delete(self, remote_path: str) -> bool:
        pass


class LocalBackupDestination(BackupDestination):
    """Local filesystem backup."""

    def __init__(self, base_path: str):
        self.base_path = base_path
        os.makedirs(base_path, exist_ok=True)

    def upload(self, local_path: str, remote_path: str) -> bool:
        dest = os.path.join(self.base_path, remote_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(local_path, dest)
        return True


class S3BackupDestination(BackupDestination):
    """S3-compatible storage backup."""

    def __init__(self, bucket: str, prefix: str = '', **kwargs):
        import boto3
        self.bucket = bucket
        self.prefix = prefix
        self.s3 = boto3.client('s3', **kwargs)

    def upload(self, local_path: str, remote_path: str) -> bool:
        key = f"{self.prefix}/{remote_path}" if self.prefix else remote_path
        self.s3.upload_file(local_path, self.bucket, key)
        return True


class BackupManager:
    """Manage site backups."""

    def __init__(self, app, destination: BackupDestination):
        self.app = app
        self.destination = destination

    def backup_site(self, domain: str, include_db=True, include_uploads=True):
        """Create backup for a site."""
        backup_id = f"{domain}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        temp_dir = f"/tmp/wo_backup_{backup_id}"
        os.makedirs(temp_dir, exist_ok=True)

        try:
            # Backup database
            if include_db:
                self._backup_database(domain, temp_dir)

            # Backup uploads
            if include_uploads:
                self._backup_uploads(domain, temp_dir)

            # Create archive
            archive_path = f"{temp_dir}.tar.gz"
            self._create_archive(temp_dir, archive_path)

            # Encrypt if configured
            if self.config.get('encryption'):
                archive_path = self._encrypt_archive(archive_path)

            # Upload to destination
            remote_path = f"backups/{domain}/{backup_id}.tar.gz"
            self.destination.upload(archive_path, remote_path)

            # Record in database
            MTDatabase.record_backup(self.app, {
                'domain': domain,
                'path': remote_path,
                'size': os.path.getsize(archive_path),
                'type': 'full' if include_db and include_uploads else 'partial'
            })

            return backup_id

        finally:
            # Cleanup temp files
            shutil.rmtree(temp_dir, ignore_errors=True)
```

**Rationale**:
- Pluggable: Easy to add GCS, Azure, etc.
- Encrypted: Optional AES encryption
- Tracked: Backups recorded in database
- Consistent: Same interface for all destinations

---

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Dependency Conflicts** | Medium - boto3/hvac | Optional deps, graceful degradation |
| **Storage Costs** | Low - Backup storage | Retention policies, compression |
| **Complexity** | Medium - More code | Modular design, clear documentation |

---

## Migration Plan

### Phase 1: Foundation (Non-Breaking)
1. Add new modules without modifying existing code
2. Add `--json` flag as optional output
3. New database tables with graceful handling if missing

### Phase 2: Integration
1. Integrate audit logging into existing operations
2. Add webhook calls to key operations
3. Migrate GitHub token to secrets provider

### Rollback
- All features are additive, rollback = disable in config
- Database tables can remain (unused)
- No changes to existing file structures

---

## Open Questions

1. **Backup Encryption Key**: Store in Vault or generate per-installation?
2. **Audit Log Size**: When to rotate/archive? (Proposed: 90 days)

---

## References

- [HashiCorp Vault Python Client](https://hvac.readthedocs.io/)
- [12 Factor App - Config](https://12factor.net/config)
- [WordOps Documentation](https://docs.wordops.net/)
