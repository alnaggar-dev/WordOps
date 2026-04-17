# Detailed Backup System Design

> **Status: OUT OF SCOPE for `refactor-devops-improvements`.**
>
> Integrated backup / restore was removed from this proposal. Rationale: WordOps already ships `wo backup` and `wo site backup`, and external tools (`restic`, `borg`, `duplicity`) cover encrypted and off-box backups far better than an in-plugin reimplementation. See `proposal.md` → *Non-Goals* for the full reasoning.
>
> This document is retained as reference material only. If a dedicated backup proposal is ever opened, start here and revise — but do not treat any of the commands, schemas, or architecture below as in-flight work.

---

## Overview

The backup system provides automated, encrypted, and verifiable backups for WordOps multi-tenancy sites with support for multiple storage destinations and point-in-time recovery.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Backup System Architecture                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────┐  │
│  │   CLI Layer  │    │  Scheduler   │    │     Restore Manager      │  │
│  │              │    │  (systemd    │    │                          │  │
│  │ wo mt backup │    │   timer)     │    │  wo mt restore           │  │
│  └──────┬───────┘    └──────┬───────┘    └────────────┬─────────────┘  │
│         │                   │                         │                 │
│         ▼                   ▼                         ▼                 │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                      Backup Manager                               │  │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐ │  │
│  │  │  Database  │  │  Uploads   │  │ Shared     │  │  Metadata  │ │  │
│  │  │  Backup    │  │  Backup    │  │ Infra      │  │  Collector │ │  │
│  │  └──────┬─────┘  └──────┬─────┘  └──────┬─────┘  └──────┬─────┘ │  │
│  └─────────┼───────────────┼───────────────┼───────────────┼────────┘  │
│            │               │               │               │           │
│            ▼               ▼               ▼               ▼           │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                     Archive Builder                               │  │
│  │  • tar.gz compression                                            │  │
│  │  • AES-256-GCM encryption (optional)                             │  │
│  │  • SHA-256 checksum                                              │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│            │                                                            │
│            ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                    Storage Destinations                           │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │  │
│  │  │  Local   │  │   S3     │  │  Rsync   │  │  S3-Compatible   │ │  │
│  │  │  Disk    │  │  (AWS)   │  │  Remote  │  │  (MinIO, Wasabi) │ │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘ │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│            │                                                            │
│            ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                    Backup Registry (SQLite)                       │  │
│  │  • Backup metadata                                                │  │
│  │  • Retention tracking                                             │  │
│  │  • Verification status                                            │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Backup Types

### 1. Site Backup (Default)
Backs up a single site's data.

**Contents:**
- MySQL database dump (gzip compressed)
- `wp-content/uploads/` directory
- `wp-config.php` (sanitized - credentials replaced with placeholders)
- Site metadata (PHP version, cache type, baseline version)

**Size Estimate:** 50MB - 5GB per site (depends on uploads)

### 2. Full Infrastructure Backup
Backs up entire multi-tenancy infrastructure.

**Contents:**
- All site databases
- All site uploads
- Shared WordPress core (`/var/www/shared/`)
- Shared plugins/themes
- Baseline configuration
- Nginx configurations
- SSL certificates

**Size Estimate:** 1GB - 50GB (depends on site count and uploads)

### 3. Database-Only Backup
Backs up only databases (fast, small).

**Contents:**
- MySQL database dumps only
- No files

**Size Estimate:** 10MB - 500MB per site

### 4. Incremental Backup (Future)
Only backs up changes since last backup.

**Implementation:** rsync with --link-dest or restic

---

## Database Schema

```sql
-- Backup registry table
CREATE TABLE multitenancy_backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identification
    backup_id VARCHAR(36) UNIQUE NOT NULL,     -- UUID
    site_domain VARCHAR(255),                   -- NULL for infra backup
    backup_type VARCHAR(50) NOT NULL,           -- site|infrastructure|database

    -- Storage
    storage_destination VARCHAR(50) NOT NULL,   -- local|s3|rsync
    storage_path TEXT NOT NULL,                 -- Full path/URL
    storage_bucket VARCHAR(255),                -- S3 bucket name

    -- Size & Compression
    size_bytes INTEGER NOT NULL,
    size_compressed_bytes INTEGER NOT NULL,
    compression_ratio REAL,

    -- Security
    is_encrypted BOOLEAN DEFAULT 0,
    encryption_algorithm VARCHAR(50),           -- aes-256-gcm
    checksum_sha256 VARCHAR(64) NOT NULL,

    -- Contents
    includes_database BOOLEAN DEFAULT 1,
    includes_uploads BOOLEAN DEFAULT 1,
    includes_config BOOLEAN DEFAULT 1,
    database_tables_count INTEGER,
    uploads_files_count INTEGER,

    -- Metadata
    wordpress_version VARCHAR(20),
    baseline_version INTEGER,
    php_version VARCHAR(10),

    -- Timestamps
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    retention_until DATETIME,

    -- Verification
    verified_at DATETIME,
    verification_status VARCHAR(50),            -- pending|valid|corrupted|missing
    last_restore_test DATETIME,

    -- Status
    status VARCHAR(50) DEFAULT 'completed'      -- in_progress|completed|failed|deleted
);

-- Indexes for common queries
CREATE INDEX idx_backups_domain ON multitenancy_backups(site_domain);
CREATE INDEX idx_backups_created ON multitenancy_backups(created_at);
CREATE INDEX idx_backups_retention ON multitenancy_backups(retention_until);
CREATE INDEX idx_backups_status ON multitenancy_backups(status);

-- Backup schedule configuration
CREATE TABLE multitenancy_backup_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_domain VARCHAR(255),                   -- NULL for all sites
    schedule_cron VARCHAR(100) NOT NULL,        -- Cron expression
    backup_type VARCHAR(50) NOT NULL,
    destination VARCHAR(50) NOT NULL,
    retention_days INTEGER DEFAULT 30,
    is_enabled BOOLEAN DEFAULT 1,
    last_run DATETIME,
    next_run DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## Configuration

```ini
# /etc/wo/plugins.d/multitenancy.conf

[backup]
# Enable/disable backup feature
enabled = true

# Default backup destination
# Options: local, s3, rsync
default_destination = local

# Local backup settings
local_path = /var/backups/wordops

# S3/S3-compatible settings
s3_bucket = my-wordops-backups
s3_region = us-east-1
s3_prefix = backups/
s3_endpoint =                          # Leave empty for AWS, set for MinIO/Wasabi
s3_access_key_env = AWS_ACCESS_KEY_ID  # Environment variable name
s3_secret_key_env = AWS_SECRET_ACCESS_KEY

# Rsync settings
rsync_destination = backup@backup-server:/backups/wordops
rsync_ssh_key = /root/.ssh/backup_key

# Encryption
encryption_enabled = true
encryption_algorithm = aes-256-gcm
# Key from secrets provider or environment variable
encryption_key_env = WO_BACKUP_ENCRYPTION_KEY

# Retention
retention_days = 30
retention_min_backups = 3              # Keep at least N backups regardless of age

# Scheduling (cron format)
schedule_enabled = true
schedule_cron = 0 2 * * *              # Daily at 2 AM
schedule_type = site                    # site|infrastructure|database

# Performance
parallel_uploads = 4                    # For multi-site backups
compression_level = 6                   # 1-9, higher = smaller but slower
temp_directory = /tmp/wo_backups

# Notifications
notify_on_success = false
notify_on_failure = true
notify_webhook = https://hooks.slack.com/xxx

# Verification
auto_verify = true                      # Verify backup after creation
verify_restore_test = false             # Actually restore to temp location (slow)
```

---

## CLI Commands

### Backup Commands

```bash
# Single site backup
wo multitenancy backup --site=example.com

# Single site with options
wo multitenancy backup --site=example.com \
    --destination=s3 \
    --encrypt \
    --no-uploads \
    --retention=90

# All sites backup
wo multitenancy backup --all

# Infrastructure backup (everything)
wo multitenancy backup --infrastructure

# Database only (fast)
wo multitenancy backup --site=example.com --database-only

# Backup with custom destination
wo multitenancy backup --site=example.com --destination=s3://custom-bucket/path

# List backups
wo multitenancy backup list [--site=example.com] [--json]

# Verify backup integrity
wo multitenancy backup verify --backup-id=abc123
wo multitenancy backup verify --all

# Delete old backups
wo multitenancy backup prune [--older-than=30d] [--keep-min=3]

# Show backup details
wo multitenancy backup show --backup-id=abc123
```

### Restore Commands

```bash
# Restore site from latest backup
wo multitenancy restore --site=example.com

# Restore from specific backup
wo multitenancy restore --site=example.com --backup-id=abc123

# Restore from specific path
wo multitenancy restore --site=example.com --from=s3://bucket/backup.tar.gz

# Point-in-time restore
wo multitenancy restore --site=example.com --point-in-time="2025-01-14 23:00:00"

# Restore to different domain (clone)
wo multitenancy restore --site=example.com --to=staging.example.com

# Dry-run (show what would be restored)
wo multitenancy restore --site=example.com --dry-run

# Restore database only
wo multitenancy restore --site=example.com --database-only

# Restore uploads only
wo multitenancy restore --site=example.com --uploads-only

# Force restore (overwrite existing)
wo multitenancy restore --site=example.com --force
```

---

## Implementation

### Core Classes

```python
# multitenancy_backup.py

import os
import json
import gzip
import tarfile
import hashlib
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from dataclasses import dataclass
from enum import Enum
import uuid


class BackupType(Enum):
    SITE = "site"
    INFRASTRUCTURE = "infrastructure"
    DATABASE = "database"


class BackupStatus(Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass
class BackupMetadata:
    """Metadata stored with each backup."""
    backup_id: str
    site_domain: Optional[str]
    backup_type: BackupType
    wordpress_version: str
    baseline_version: int
    php_version: str
    created_at: datetime
    includes_database: bool
    includes_uploads: bool
    includes_config: bool
    database_tables: List[str]
    uploads_file_count: int
    original_size_bytes: int

    def to_json(self) -> str:
        return json.dumps({
            'backup_id': self.backup_id,
            'site_domain': self.site_domain,
            'backup_type': self.backup_type.value,
            'wordpress_version': self.wordpress_version,
            'baseline_version': self.baseline_version,
            'php_version': self.php_version,
            'created_at': self.created_at.isoformat(),
            'includes_database': self.includes_database,
            'includes_uploads': self.includes_uploads,
            'includes_config': self.includes_config,
            'database_tables': self.database_tables,
            'uploads_file_count': self.uploads_file_count,
            'original_size_bytes': self.original_size_bytes
        }, indent=2)


class BackupDestination(ABC):
    """Abstract base class for backup storage destinations."""

    @abstractmethod
    def upload(self, local_path: str, remote_path: str) -> bool:
        """Upload backup to destination."""
        pass

    @abstractmethod
    def download(self, remote_path: str, local_path: str) -> bool:
        """Download backup from destination."""
        pass

    @abstractmethod
    def list(self, prefix: str = "") -> List[Dict]:
        """List backups at destination."""
        pass

    @abstractmethod
    def delete(self, remote_path: str) -> bool:
        """Delete backup from destination."""
        pass

    @abstractmethod
    def exists(self, remote_path: str) -> bool:
        """Check if backup exists."""
        pass


class LocalBackupDestination(BackupDestination):
    """Local filesystem backup storage."""

    def __init__(self, base_path: str):
        self.base_path = base_path
        os.makedirs(base_path, exist_ok=True)

    def upload(self, local_path: str, remote_path: str) -> bool:
        dest = os.path.join(self.base_path, remote_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # Use rsync for atomic copy with checksum verification
        cmd = ['rsync', '-a', '--checksum', local_path, dest]
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0

    def download(self, remote_path: str, local_path: str) -> bool:
        src = os.path.join(self.base_path, remote_path)
        if not os.path.exists(src):
            return False

        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        cmd = ['rsync', '-a', '--checksum', src, local_path]
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0

    def list(self, prefix: str = "") -> List[Dict]:
        search_path = os.path.join(self.base_path, prefix)
        results = []

        if os.path.exists(search_path):
            for root, dirs, files in os.walk(search_path):
                for f in files:
                    if f.endswith('.tar.gz') or f.endswith('.tar.gz.enc'):
                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, self.base_path)
                        stat = os.stat(full_path)
                        results.append({
                            'path': rel_path,
                            'size': stat.st_size,
                            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
                        })

        return results

    def delete(self, remote_path: str) -> bool:
        full_path = os.path.join(self.base_path, remote_path)
        if os.path.exists(full_path):
            os.remove(full_path)
            return True
        return False

    def exists(self, remote_path: str) -> bool:
        return os.path.exists(os.path.join(self.base_path, remote_path))


class S3BackupDestination(BackupDestination):
    """S3-compatible storage backup destination."""

    def __init__(self, bucket: str, prefix: str = "",
                 region: str = "us-east-1", endpoint: str = None):
        self.bucket = bucket
        self.prefix = prefix.strip('/')
        self.region = region
        self.endpoint = endpoint
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3
            kwargs = {'region_name': self.region}
            if self.endpoint:
                kwargs['endpoint_url'] = self.endpoint
            self._client = boto3.client('s3', **kwargs)
        return self._client

    def _full_key(self, path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    def upload(self, local_path: str, remote_path: str) -> bool:
        key = self._full_key(remote_path)

        # Use multipart upload for large files
        file_size = os.path.getsize(local_path)

        if file_size > 100 * 1024 * 1024:  # > 100MB
            from boto3.s3.transfer import TransferConfig
            config = TransferConfig(
                multipart_threshold=100 * 1024 * 1024,
                multipart_chunksize=100 * 1024 * 1024,
                max_concurrency=4
            )
            self.client.upload_file(local_path, self.bucket, key, Config=config)
        else:
            self.client.upload_file(local_path, self.bucket, key)

        return True

    def download(self, remote_path: str, local_path: str) -> bool:
        key = self._full_key(remote_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        self.client.download_file(self.bucket, key, local_path)
        return True

    def list(self, prefix: str = "") -> List[Dict]:
        search_prefix = self._full_key(prefix) if prefix else self.prefix

        results = []
        paginator = self.client.get_paginator('list_objects_v2')

        for page in paginator.paginate(Bucket=self.bucket, Prefix=search_prefix):
            for obj in page.get('Contents', []):
                results.append({
                    'path': obj['Key'].replace(f"{self.prefix}/", "", 1) if self.prefix else obj['Key'],
                    'size': obj['Size'],
                    'modified': obj['LastModified'].isoformat()
                })

        return results

    def delete(self, remote_path: str) -> bool:
        key = self._full_key(remote_path)
        self.client.delete_object(Bucket=self.bucket, Key=key)
        return True

    def exists(self, remote_path: str) -> bool:
        key = self._full_key(remote_path)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except:
            return False


class BackupEncryption:
    """Handle backup encryption/decryption."""

    def __init__(self, key: bytes):
        """Initialize with 32-byte key for AES-256."""
        if len(key) != 32:
            # Derive 32-byte key from provided key
            self.key = hashlib.sha256(key).digest()
        else:
            self.key = key

    def encrypt_file(self, input_path: str, output_path: str) -> bool:
        """Encrypt file using AES-256-GCM."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import secrets

        # Generate random nonce
        nonce = secrets.token_bytes(12)

        # Read and encrypt
        with open(input_path, 'rb') as f:
            plaintext = f.read()

        aesgcm = AESGCM(self.key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        # Write nonce + ciphertext
        with open(output_path, 'wb') as f:
            f.write(nonce)
            f.write(ciphertext)

        return True

    def decrypt_file(self, input_path: str, output_path: str) -> bool:
        """Decrypt file."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        with open(input_path, 'rb') as f:
            nonce = f.read(12)
            ciphertext = f.read()

        aesgcm = AESGCM(self.key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)

        with open(output_path, 'wb') as f:
            f.write(plaintext)

        return True


class SiteBackupBuilder:
    """Build backup archive for a single site."""

    def __init__(self, app, domain: str, config: dict):
        self.app = app
        self.domain = domain
        self.config = config
        self.site_root = f"/var/www/{domain}"
        self.temp_dir = None
        self.metadata = None

    def create_backup(self,
                      include_database: bool = True,
                      include_uploads: bool = True,
                      include_config: bool = True) -> str:
        """Create backup and return path to archive."""

        backup_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.temp_dir = f"/tmp/wo_backup_{self.domain}_{timestamp}"
        os.makedirs(self.temp_dir, exist_ok=True)

        try:
            # Collect metadata
            self.metadata = self._collect_metadata(backup_id,
                                                    include_database,
                                                    include_uploads,
                                                    include_config)

            # Backup components
            if include_database:
                self._backup_database()

            if include_uploads:
                self._backup_uploads()

            if include_config:
                self._backup_config()

            # Write metadata
            self._write_metadata()

            # Create archive
            archive_path = self._create_archive(timestamp)

            return archive_path

        except Exception as e:
            Log.error(self.app, f"Backup failed for {self.domain}: {e}")
            raise
        finally:
            # Cleanup happens after upload
            pass

    def cleanup(self):
        """Remove temporary files."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir)

    def _collect_metadata(self, backup_id: str,
                          include_db: bool, include_uploads: bool,
                          include_config: bool) -> BackupMetadata:
        """Collect backup metadata."""

        # Get site info from database
        site_info = MTDatabase.get_site_by_domain(self.app, self.domain)

        # Get WordPress version
        wp_version = self._get_wordpress_version()

        # Count uploads
        uploads_count = 0
        uploads_path = f"{self.site_root}/htdocs/wp-content/uploads"
        if include_uploads and os.path.exists(uploads_path):
            for root, dirs, files in os.walk(uploads_path):
                uploads_count += len(files)

        # Get database tables
        db_tables = []
        if include_db:
            db_tables = self._get_database_tables()

        return BackupMetadata(
            backup_id=backup_id,
            site_domain=self.domain,
            backup_type=BackupType.SITE,
            wordpress_version=wp_version,
            baseline_version=site_info.get('baseline_version', 0),
            php_version=site_info.get('php_version', '8.3'),
            created_at=datetime.now(),
            includes_database=include_db,
            includes_uploads=include_uploads,
            includes_config=include_config,
            database_tables=db_tables,
            uploads_file_count=uploads_count,
            original_size_bytes=0  # Updated after backup
        )

    def _backup_database(self):
        """Export MySQL database."""

        # Get database credentials from wp-config.php
        db_creds = self._parse_wp_config_db()

        dump_file = os.path.join(self.temp_dir, 'database.sql')
        dump_gz = f"{dump_file}.gz"

        # mysqldump with optimal settings
        cmd = [
            'mysqldump',
            f"--user={db_creds['user']}",
            f"--password={db_creds['password']}",
            f"--host={db_creds['host']}",
            '--single-transaction',      # Consistent backup without locking
            '--quick',                   # Don't buffer, write directly
            '--lock-tables=false',       # Don't lock tables
            '--routines',                # Include stored procedures
            '--triggers',                # Include triggers
            '--events',                  # Include events
            db_creds['name']
        ]

        # Dump and compress in one step
        with gzip.open(dump_gz, 'wb') as gz:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()

            if process.returncode != 0:
                raise Exception(f"mysqldump failed: {stderr.decode()}")

            gz.write(stdout)

        Log.debug(self.app, f"Database backup created: {dump_gz}")

    def _backup_uploads(self):
        """Backup uploads directory."""

        uploads_src = f"{self.site_root}/htdocs/wp-content/uploads"
        uploads_dest = os.path.join(self.temp_dir, 'uploads')

        if not os.path.exists(uploads_src):
            Log.debug(self.app, f"No uploads directory for {self.domain}")
            return

        # Use rsync for efficient copy
        cmd = [
            'rsync', '-a',
            '--exclude=cache/',          # Exclude cache directories
            '--exclude=*.log',           # Exclude log files
            uploads_src + '/',
            uploads_dest
        ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise Exception(f"Uploads backup failed: {result.stderr.decode()}")

        Log.debug(self.app, f"Uploads backup created: {uploads_dest}")

    def _backup_config(self):
        """Backup wp-config.php (sanitized)."""

        config_src = f"{self.site_root}/wp-config.php"
        config_dest = os.path.join(self.temp_dir, 'wp-config.php')

        if not os.path.exists(config_src):
            Log.debug(self.app, f"No wp-config.php for {self.domain}")
            return

        with open(config_src, 'r') as f:
            content = f.read()

        # Sanitize sensitive data
        import re

        # Replace database password
        content = re.sub(
            r"define\(\s*'DB_PASSWORD'\s*,\s*'[^']*'\s*\)",
            "define('DB_PASSWORD', '{{DB_PASSWORD}}')",
            content
        )

        # Replace auth keys/salts
        for key in ['AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY',
                    'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT']:
            content = re.sub(
                rf"define\(\s*'{key}'\s*,\s*'[^']*'\s*\)",
                f"define('{key}', '{{{{key}}}}')",
                content
            )

        with open(config_dest, 'w') as f:
            f.write(content)

        Log.debug(self.app, f"Config backup created: {config_dest}")

    def _write_metadata(self):
        """Write metadata JSON file."""

        metadata_path = os.path.join(self.temp_dir, 'backup_metadata.json')

        with open(metadata_path, 'w') as f:
            f.write(self.metadata.to_json())

    def _create_archive(self, timestamp: str) -> str:
        """Create tar.gz archive."""

        archive_name = f"{self.domain}_{timestamp}.tar.gz"
        archive_path = f"/tmp/{archive_name}"

        # Calculate original size
        original_size = 0
        for root, dirs, files in os.walk(self.temp_dir):
            for f in files:
                original_size += os.path.getsize(os.path.join(root, f))

        self.metadata.original_size_bytes = original_size

        # Create tarball
        with tarfile.open(archive_path, 'w:gz',
                         compresslevel=self.config.get('compression_level', 6)) as tar:
            tar.add(self.temp_dir, arcname=self.domain)

        Log.debug(self.app, f"Archive created: {archive_path}")
        return archive_path

    def _get_wordpress_version(self) -> str:
        """Get WordPress version from site."""
        try:
            cmd = ['wp', 'core', 'version',
                   f'--path={self.site_root}/htdocs',
                   '--allow-root']
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.stdout.strip()
        except:
            return 'unknown'

    def _get_database_tables(self) -> List[str]:
        """Get list of database tables."""
        db_creds = self._parse_wp_config_db()

        cmd = [
            'mysql',
            f"--user={db_creds['user']}",
            f"--password={db_creds['password']}",
            f"--host={db_creds['host']}",
            '-N', '-e', 'SHOW TABLES',
            db_creds['name']
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip().split('\n')
        return []

    def _parse_wp_config_db(self) -> dict:
        """Parse database credentials from wp-config.php."""
        config_path = f"{self.site_root}/wp-config.php"

        with open(config_path, 'r') as f:
            content = f.read()

        import re

        def extract(key):
            match = re.search(rf"define\(\s*'{key}'\s*,\s*'([^']*)'\s*\)", content)
            return match.group(1) if match else None

        return {
            'name': extract('DB_NAME'),
            'user': extract('DB_USER'),
            'password': extract('DB_PASSWORD'),
            'host': extract('DB_HOST') or 'localhost'
        }


class BackupManager:
    """High-level backup management."""

    def __init__(self, app, config: dict):
        self.app = app
        self.config = config
        self.destination = self._create_destination()
        self.encryption = self._create_encryption() if config.get('encryption_enabled') else None

    def _create_destination(self) -> BackupDestination:
        """Create appropriate backup destination."""
        dest_type = self.config.get('default_destination', 'local')

        if dest_type == 'local':
            return LocalBackupDestination(
                self.config.get('local_path', '/var/backups/wordops')
            )
        elif dest_type == 's3':
            return S3BackupDestination(
                bucket=self.config['s3_bucket'],
                prefix=self.config.get('s3_prefix', ''),
                region=self.config.get('s3_region', 'us-east-1'),
                endpoint=self.config.get('s3_endpoint')
            )
        else:
            raise ValueError(f"Unknown backup destination: {dest_type}")

    def _create_encryption(self) -> BackupEncryption:
        """Create encryption handler."""
        key_env = self.config.get('encryption_key_env', 'WO_BACKUP_ENCRYPTION_KEY')
        key = os.environ.get(key_env)

        if not key:
            raise ValueError(f"Encryption key not found in {key_env}")

        return BackupEncryption(key.encode())

    def backup_site(self, domain: str,
                    include_database: bool = True,
                    include_uploads: bool = True,
                    retention_days: int = None) -> dict:
        """Create backup for a single site."""

        Log.info(self.app, f"Starting backup for {domain}")

        builder = SiteBackupBuilder(self.app, domain, self.config)

        try:
            # Create backup archive
            archive_path = builder.create_backup(
                include_database=include_database,
                include_uploads=include_uploads
            )

            # Calculate checksum
            checksum = self._calculate_checksum(archive_path)

            # Encrypt if enabled
            final_path = archive_path
            if self.encryption:
                encrypted_path = f"{archive_path}.enc"
                self.encryption.encrypt_file(archive_path, encrypted_path)
                os.remove(archive_path)
                final_path = encrypted_path

            # Determine remote path
            timestamp = datetime.now().strftime('%Y/%m/%d')
            remote_path = f"{domain}/{timestamp}/{os.path.basename(final_path)}"

            # Upload to destination
            self.destination.upload(final_path, remote_path)

            # Calculate retention
            retention = retention_days or self.config.get('retention_days', 30)
            retention_until = datetime.now() + timedelta(days=retention)

            # Record in database
            backup_record = {
                'backup_id': builder.metadata.backup_id,
                'site_domain': domain,
                'backup_type': 'site',
                'storage_destination': self.config.get('default_destination', 'local'),
                'storage_path': remote_path,
                'size_bytes': builder.metadata.original_size_bytes,
                'size_compressed_bytes': os.path.getsize(final_path),
                'is_encrypted': bool(self.encryption),
                'checksum_sha256': checksum,
                'includes_database': include_database,
                'includes_uploads': include_uploads,
                'wordpress_version': builder.metadata.wordpress_version,
                'baseline_version': builder.metadata.baseline_version,
                'retention_until': retention_until.isoformat(),
                'status': 'completed'
            }

            MTDatabase.record_backup(self.app, backup_record)

            # Cleanup local files
            os.remove(final_path)
            builder.cleanup()

            # Verify if configured
            if self.config.get('auto_verify'):
                self.verify_backup(backup_record['backup_id'])

            Log.info(self.app, f"Backup completed: {backup_record['backup_id']}")

            return backup_record

        except Exception as e:
            Log.error(self.app, f"Backup failed for {domain}: {e}")
            builder.cleanup()
            raise

    def restore_site(self, domain: str,
                     backup_id: str = None,
                     from_path: str = None,
                     restore_database: bool = True,
                     restore_uploads: bool = True,
                     dry_run: bool = False) -> dict:
        """Restore site from backup."""

        # Find backup
        if backup_id:
            backup = MTDatabase.get_backup(self.app, backup_id)
            if not backup:
                raise ValueError(f"Backup not found: {backup_id}")
            from_path = backup['storage_path']
        elif not from_path:
            # Get latest backup for domain
            backup = MTDatabase.get_latest_backup(self.app, domain)
            if not backup:
                raise ValueError(f"No backups found for {domain}")
            from_path = backup['storage_path']

        Log.info(self.app, f"Restoring {domain} from {from_path}")

        if dry_run:
            return self._dry_run_restore(domain, from_path)

        # Download backup
        temp_archive = f"/tmp/restore_{domain}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
        self.destination.download(from_path, temp_archive)

        # Decrypt if needed
        if temp_archive.endswith('.enc'):
            decrypted = temp_archive.replace('.enc', '')
            self.encryption.decrypt_file(temp_archive, decrypted)
            os.remove(temp_archive)
            temp_archive = decrypted

        # Extract
        temp_dir = temp_archive.replace('.tar.gz', '')
        os.makedirs(temp_dir, exist_ok=True)

        with tarfile.open(temp_archive, 'r:gz') as tar:
            tar.extractall(temp_dir)

        extracted_dir = os.path.join(temp_dir, domain)

        try:
            # Restore database
            if restore_database:
                self._restore_database(domain, extracted_dir)

            # Restore uploads
            if restore_uploads:
                self._restore_uploads(domain, extracted_dir)

            # Clear caches
            MTFunctions.clear_site_cache(self.app, domain)

            Log.info(self.app, f"Restore completed for {domain}")

            return {
                'status': 'completed',
                'domain': domain,
                'restored_from': from_path,
                'restored_database': restore_database,
                'restored_uploads': restore_uploads
            }

        finally:
            # Cleanup
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(temp_archive):
                os.remove(temp_archive)

    def verify_backup(self, backup_id: str) -> dict:
        """Verify backup integrity."""

        backup = MTDatabase.get_backup(self.app, backup_id)
        if not backup:
            raise ValueError(f"Backup not found: {backup_id}")

        # Check if backup exists at destination
        if not self.destination.exists(backup['storage_path']):
            MTDatabase.update_backup_verification(self.app, backup_id, 'missing')
            return {'status': 'missing', 'backup_id': backup_id}

        # Download and verify checksum
        temp_file = f"/tmp/verify_{backup_id}.tar.gz"
        self.destination.download(backup['storage_path'], temp_file)

        checksum = self._calculate_checksum(temp_file)
        os.remove(temp_file)

        if checksum == backup['checksum_sha256']:
            MTDatabase.update_backup_verification(self.app, backup_id, 'valid')
            return {'status': 'valid', 'backup_id': backup_id, 'checksum': checksum}
        else:
            MTDatabase.update_backup_verification(self.app, backup_id, 'corrupted')
            return {
                'status': 'corrupted',
                'backup_id': backup_id,
                'expected': backup['checksum_sha256'],
                'actual': checksum
            }

    def prune_old_backups(self, older_than_days: int = None, keep_min: int = None):
        """Delete old backups based on retention policy."""

        older_than = older_than_days or self.config.get('retention_days', 30)
        keep_min = keep_min or self.config.get('retention_min_backups', 3)

        cutoff = datetime.now() - timedelta(days=older_than)

        # Get all backups grouped by domain
        all_backups = MTDatabase.get_all_backups(self.app)

        by_domain = {}
        for backup in all_backups:
            domain = backup['site_domain']
            if domain not in by_domain:
                by_domain[domain] = []
            by_domain[domain].append(backup)

        deleted = []

        for domain, backups in by_domain.items():
            # Sort by date (newest first)
            backups.sort(key=lambda x: x['created_at'], reverse=True)

            # Keep minimum count
            to_check = backups[keep_min:]

            for backup in to_check:
                created = datetime.fromisoformat(backup['created_at'])
                if created < cutoff:
                    # Delete from storage
                    self.destination.delete(backup['storage_path'])

                    # Mark as deleted in database
                    MTDatabase.update_backup_status(self.app, backup['backup_id'], 'deleted')

                    deleted.append(backup['backup_id'])
                    Log.info(self.app, f"Deleted old backup: {backup['backup_id']}")

        return {'deleted_count': len(deleted), 'deleted_ids': deleted}

    def _calculate_checksum(self, file_path: str) -> str:
        """Calculate SHA-256 checksum of file."""
        sha256 = hashlib.sha256()

        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)

        return sha256.hexdigest()

    def _restore_database(self, domain: str, backup_dir: str):
        """Restore database from backup."""

        dump_file = os.path.join(backup_dir, 'database.sql.gz')
        if not os.path.exists(dump_file):
            Log.debug(self.app, "No database backup found")
            return

        # Get current database credentials
        site_root = f"/var/www/{domain}"
        db_creds = self._parse_wp_config_db(site_root)

        # Restore
        with gzip.open(dump_file, 'rb') as gz:
            cmd = [
                'mysql',
                f"--user={db_creds['user']}",
                f"--password={db_creds['password']}",
                f"--host={db_creds['host']}",
                db_creds['name']
            ]

            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            _, stderr = process.communicate(gz.read())

            if process.returncode != 0:
                raise Exception(f"Database restore failed: {stderr.decode()}")

        Log.debug(self.app, f"Database restored for {domain}")

    def _restore_uploads(self, domain: str, backup_dir: str):
        """Restore uploads directory from backup."""

        uploads_backup = os.path.join(backup_dir, 'uploads')
        if not os.path.exists(uploads_backup):
            Log.debug(self.app, "No uploads backup found")
            return

        uploads_dest = f"/var/www/{domain}/htdocs/wp-content/uploads"

        # Sync uploads (preserving any new files)
        cmd = [
            'rsync', '-a',
            '--delete',  # Remove files not in backup
            uploads_backup + '/',
            uploads_dest
        ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise Exception(f"Uploads restore failed: {result.stderr.decode()}")

        # Fix permissions
        subprocess.run(['chown', '-R', 'www-data:www-data', uploads_dest])

        Log.debug(self.app, f"Uploads restored for {domain}")

    def _dry_run_restore(self, domain: str, from_path: str) -> dict:
        """Preview restore without making changes."""

        # Download backup metadata only
        temp_archive = f"/tmp/dryrun_{datetime.now().timestamp()}.tar.gz"
        self.destination.download(from_path, temp_archive)

        # Extract just metadata
        with tarfile.open(temp_archive, 'r:gz') as tar:
            for member in tar.getmembers():
                if member.name.endswith('backup_metadata.json'):
                    f = tar.extractfile(member)
                    metadata = json.load(f)
                    break

        os.remove(temp_archive)

        return {
            'dry_run': True,
            'domain': domain,
            'would_restore_from': from_path,
            'backup_metadata': metadata,
            'would_restore_database': metadata.get('includes_database'),
            'would_restore_uploads': metadata.get('includes_uploads'),
            'uploads_file_count': metadata.get('uploads_file_count'),
            'database_tables': metadata.get('database_tables')
        }
```

---

## Restore Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│                      Restore Workflow                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌──────────────┐                                              │
│   │ User Request │                                              │
│   │  --site=X    │                                              │
│   │  --from=path │                                              │
│   └──────┬───────┘                                              │
│          │                                                       │
│          ▼                                                       │
│   ┌──────────────────────────────────────────────────┐          │
│   │ 1. Validate Request                               │          │
│   │    • Site exists (or --to for clone)             │          │
│   │    • Backup exists and accessible                │          │
│   │    • Checksum valid                              │          │
│   └──────────────────────────┬───────────────────────┘          │
│                              │                                   │
│                              ▼                                   │
│   ┌──────────────────────────────────────────────────┐          │
│   │ 2. Download Backup                                │          │
│   │    • Stream from S3/local                        │          │
│   │    • Verify checksum                             │          │
│   │    • Decrypt if encrypted                        │          │
│   └──────────────────────────┬───────────────────────┘          │
│                              │                                   │
│                              ▼                                   │
│   ┌──────────────────────────────────────────────────┐          │
│   │ 3. Enable Maintenance Mode (optional)             │          │
│   │    • Show maintenance page                       │          │
│   │    • Prevent user access during restore          │          │
│   └──────────────────────────┬───────────────────────┘          │
│                              │                                   │
│                              ▼                                   │
│   ┌──────────────────────────────────────────────────┐          │
│   │ 4. Extract Archive                                │          │
│   │    • Extract to temp directory                   │          │
│   │    • Parse metadata.json                         │          │
│   └──────────────────────────┬───────────────────────┘          │
│                              │                                   │
│          ┌───────────────────┼───────────────────┐              │
│          │                   │                   │              │
│          ▼                   ▼                   ▼              │
│   ┌────────────┐      ┌────────────┐      ┌────────────┐       │
│   │ 5a. Restore│      │ 5b. Restore│      │ 5c. Restore│       │
│   │  Database  │      │  Uploads   │      │   Config   │       │
│   │            │      │            │      │ (optional) │       │
│   │ • Drop/    │      │ • rsync    │      │ • Merge    │       │
│   │   recreate │      │   --delete │      │   settings │       │
│   │ • Import   │      │ • Fix perms│      │            │       │
│   │   dump     │      │            │      │            │       │
│   └─────┬──────┘      └──────┬─────┘      └──────┬─────┘       │
│         │                    │                   │              │
│         └────────────────────┼───────────────────┘              │
│                              │                                   │
│                              ▼                                   │
│   ┌──────────────────────────────────────────────────┐          │
│   │ 6. Post-Restore Tasks                             │          │
│   │    • Clear all caches                            │          │
│   │    • Update site metadata                        │          │
│   │    • Run wp-cron                                 │          │
│   │    • Verify site accessible                      │          │
│   └──────────────────────────┬───────────────────────┘          │
│                              │                                   │
│                              ▼                                   │
│   ┌──────────────────────────────────────────────────┐          │
│   │ 7. Disable Maintenance Mode                       │          │
│   │    • Restore normal operation                    │          │
│   │    • Send webhook notification                   │          │
│   └──────────────────────────┬───────────────────────┘          │
│                              │                                   │
│                              ▼                                   │
│   ┌──────────────────────────────────────────────────┐          │
│   │ 8. Cleanup                                        │          │
│   │    • Remove temp files                           │          │
│   │    • Log restore completion                      │          │
│   │    • Record in audit log                         │          │
│   └──────────────────────────────────────────────────┘          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Backup Schedule (systemd Timer)

```ini
# /etc/systemd/system/wo-multitenancy-backup.service
[Unit]
Description=WordOps Multitenancy Backup
After=network.target mysql.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/wo multitenancy backup --all
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/wo-multitenancy-backup.timer
[Unit]
Description=Daily WordOps Multitenancy Backup

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
RandomizedDelaySec=1800

[Install]
WantedBy=timers.target
```

---

## S3 Lifecycle Policy (Recommended)

```json
{
  "Rules": [
    {
      "ID": "WordOpsBackupLifecycle",
      "Status": "Enabled",
      "Prefix": "backups/",
      "Transitions": [
        {
          "Days": 30,
          "StorageClass": "STANDARD_IA"
        },
        {
          "Days": 90,
          "StorageClass": "GLACIER"
        }
      ],
      "Expiration": {
        "Days": 365
      }
    }
  ]
}
```

---

## Cost Estimates

### Storage Costs (AWS S3)

| Sites | Avg Size/Site | Monthly Backups | Storage | Cost/Month |
|-------|---------------|-----------------|---------|------------|
| 10 | 500MB | 30 | 150GB | ~$3.50 |
| 50 | 500MB | 30 | 750GB | ~$17.25 |
| 100 | 500MB | 30 | 1.5TB | ~$34.50 |

*Assumes S3 Standard pricing at $0.023/GB*

### With Glacier Transition (90+ days)

| Sites | Monthly Cost | With Glacier |
|-------|-------------|--------------|
| 10 | $3.50 | $1.50 |
| 50 | $17.25 | $7.50 |
| 100 | $34.50 | $15.00 |

---

## Testing Checklist

- [ ] Single site backup creates valid archive
- [ ] Database backup is restorable
- [ ] Uploads backup preserves permissions
- [ ] Encryption/decryption works correctly
- [ ] Checksum verification detects corruption
- [ ] S3 upload/download works
- [ ] Retention policy deletes old backups
- [ ] Restore recovers site completely
- [ ] Point-in-time restore finds correct backup
- [ ] Dry-run shows accurate preview
- [ ] Scheduled backups run on time
- [ ] Webhook notifications fire on success/failure
- [ ] Audit log records all backup operations
