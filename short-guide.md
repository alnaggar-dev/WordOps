# WordOps Multi-tenancy Quick Guide

## Quick Start

### 1. Activate Plugin
```bash
sudo mkdir -p /etc/wo/plugins.d
sudo tee /etc/wo/plugins.d/multitenancy.conf >/dev/null <<'EOF'
[multitenancy]
enable_plugin = true
shared_root = /var/www/shared
baseline_plugins = nginx-helper,redis-cache
baseline_theme = twentytwentyfour
EOF
```

### 2. Initialize Shared Infrastructure
```bash
wo multitenancy init
```

### 3. Create Sites
```bash
# Basic site
wo multitenancy create example.com --php83 --wpfc

# With SSL
wo multitenancy create example.com --php83 --wpfc --le

# With Redis cache
wo multitenancy create example.com --php83 --wpredis --le
```

---

## Essential Commands

| Command | Description |
|---------|-------------|
| `wo multitenancy init` | Initialize shared WordPress |
| `wo multitenancy create <domain> [options]` | Create new site |
| `wo multitenancy list` | List all sites |
| `wo multitenancy status` | Show health status |
| `wo multitenancy update` | Update WordPress core |
| `wo multitenancy rollback` | Rollback to previous version |
| `wo multitenancy baseline` | Show baseline config |

---

## Site Creation Options

**PHP Versions:** `--php74` `--php80` `--php81` `--php82` `--php83` `--php84`

**Cache Types:**
- `--wpfc` - FastCGI cache
- `--wpredis` - Redis cache
- `--wprocket` - WP Rocket
- `--wpce` - Cache Enabler
- `--wpsc` - WP Super Cache

**SSL:** `--le` or `--letsencrypt`

**HSTS:** `--hsts`

---

## Configuration File

**Location:** `/etc/wo/plugins.d/multitenancy.conf`

```ini
[multitenancy]
enable_plugin = true
shared_root = /var/www/shared
baseline_plugins = nginx-helper,redis-cache,contact-form-7
baseline_theme = twentytwentyfour

# GitHub plugins (optional)
[github_plugins]
my-plugin = mycompany/my-plugin,tag,v1.0.0

# URL plugins (optional)
[url_plugins]
premium-plugin = https://example.com/plugin.zip
```

After config changes: `wo multitenancy init --force`

---

## Baseline Management

```bash
# Add plugin from WordPress.org
wo multitenancy baseline add-plugin contact-form-7

# Add plugin from GitHub
wo multitenancy baseline add-plugin my-plugin --github=user/repo --tag=v1.0.0

# Add plugin from URL
wo multitenancy baseline add-plugin premium --url=https://example.com/plugin.zip

# Remove plugin
wo multitenancy baseline remove-plugin old-plugin

# Apply baseline to all sites
wo multitenancy baseline apply
```

---

## Shared Config Management

```bash
# Edit shared config (opens $EDITOR; on save runs php -l and reloads
# services; a timestamped .bak is kept, broken edits auto-revert)
wo multitenancy shared-config --action edit
```

---

## Directory Structure

```
/var/www/shared/
├── current -> releases/wp-YYYYMMDD-HHMMSS
├── releases/
├── wp-content/
│   ├── plugins/
│   ├── themes/
│   └── mu-plugins/
└── config/
    └── baseline.json

/var/www/example.com/
├── wp-config.php
├── htdocs/
│   ├── wp -> /var/www/shared/current
│   └── wp-content/
│       ├── uploads/  (site-specific)
│       └── cache/    (site-specific)
└── conf/nginx/
```

---

## Quick Troubleshooting

```bash
# Check plugin is active
wo multitenancy --help

# Check symlinks
ls -la /var/www/example.com/htdocs/wp
ls -la /var/www/shared/current

# Test nginx
nginx -t && systemctl reload nginx

# Check PHP-FPM
systemctl status php8.3-fpm

# Fix permissions
chown -R www-data:www-data /var/www/example.com/htdocs/wp-content/uploads
```

---

## Common Issues

| Issue | Fix |
|-------|-----|
| SSL not working | Use `--le` (double hyphen, not em dash) |
| 404 errors | Check symlinks exist |
| Blank page | Verify theme is installed |
| Permission denied | Run `chown -R www-data:www-data` on uploads |

---

## Update Workflow

```bash
# Update WordPress (all sites)
wo multitenancy update

# If issues, rollback instantly
wo multitenancy rollback
```
