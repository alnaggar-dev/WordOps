# Project Context

## Purpose
WordOps Multi-tenancy Plugin - A fork/extension of WordOps that enables efficient WordPress hosting by sharing a single WordPress core installation across multiple sites. This dramatically reduces disk usage (~90% savings), simplifies updates (atomic deployments with instant rollback), and maintains consistency across all managed sites.

**Target Use Cases:**
- Single administrator managing multiple WordPress sites
- Sites sharing the same plugin/theme sets
- Environments where consistency and update efficiency matter
- Production hosting with minimal disk footprint

## Tech Stack
- **Language:** Python 3.x
- **CLI Framework:** Cement Framework (CementBaseController)
- **Database:** SQLite (`/var/lib/wo/dbase.db`)
- **Web Server:** Nginx with modular include system
- **PHP:** PHP-FPM (supports 7.4, 8.0, 8.1, 8.2, 8.3, 8.4)
- **CMS:** WordPress with WP-CLI
- **SSL:** Let's Encrypt via WOAcme
- **Caching:** FastCGI cache, Redis, WP Rocket, Cache Enabler, WP Super Cache
- **Configuration:** INI files (`/etc/wo/plugins.d/multitenancy.conf`)
- **Templates:** Mustache (WordOps native), modular nginx includes
- **Services:** systemd for PHP-FPM and nginx management

## Project Conventions

### Code Style
- **Naming:** snake_case for functions/variables, PascalCase for classes
- **Module Organization:** Controller/Functions/Database separation pattern
  - `multitenancy.py` - CLI controller with `@expose` decorators
  - `multitenancy_functions.py` - Business logic (MTFunctions, SharedInfrastructure, ReleaseManager, etc.)
  - `multitenancy_db.py` - Database operations (MTDatabase class)
- **Logging:** Use WordOps `Log` class (Log.info, Log.debug, Log.error, Log.warn)
- **Error Handling:** Comprehensive try/except with descriptive error messages
- **Imports:** Group by stdlib, then WordOps core, then plugin modules

### Architecture Patterns
- **Plugin System:** Cement-based plugin architecture extending WordOps
- **Native Integration:** Use WordOps native functions (setupdatabase, WOAcme, WOService) rather than reimplementing
- **Modular Nginx:** Generate nginx configs using WordOps' modular include system (common/wpfc-php83.conf, etc.) instead of custom templates
- **Symlink Structure:** Shared WordPress core via symlinks, transparent to nginx
- **Atomic Operations:** Release switching via symlink swap for zero-downtime updates
- **Baseline Enforcement:** MU-plugin (`wo-baseline-enforcer.php`) auto-activates plugins/themes

### Directory Structure
```
wo/cli/plugins/
├── multitenancy.py           # Main controller
├── multitenancy_functions.py # Core business logic
└── multitenancy_db.py        # Database operations

/var/www/shared/              # Shared infrastructure
├── current -> releases/wp-*  # Active release symlink
├── releases/                 # WordPress core releases
├── wp-content/               # Shared plugins/themes/mu-plugins
└── config/                   # baseline.json, wp-config-shared.php

/var/www/<domain>/            # Per-site structure
├── wp-config.php             # Site-specific config
├── htdocs/
│   ├── wp -> /var/www/shared/current
│   └── wp-content/           # Site-specific uploads/cache
└── conf/nginx/               # Site nginx config
```

### Testing Strategy
- Manual testing via CLI commands
- Health checks after operations (`enable_health_check = true`)
- Canary deployment testing before full rollout
- Nginx configuration validation (`nginx -t`)
- PHP syntax validation for shared config changes

### Git Workflow
- Main branch: `main`
- Commit messages should describe what changed and why
- Co-authored commits with Claude Code attribution when AI-assisted
- No force pushes to main

## Domain Context

### WordPress Multi-tenancy Concepts
- **Shared Core:** Single WordPress installation (wp-admin, wp-includes) serving all sites
- **Per-Site Data:** Each site has own database, uploads, cache directories
- **Baseline:** Enforced set of plugins/themes across all sites
- **MU-Plugin:** Must-use plugin that auto-activates baseline on each site load

### WordOps Integration Points
- `setupdatabase()` - Creates MySQL database and user
- `WOAcme` - SSL certificate management
- `WOService` - Service control (nginx, php-fpm)
- `WOShellExec` - Command execution
- `Log` - Logging infrastructure
- `WOVar` - System variables and paths

### Plugin Sources
- **WordPress.org:** Public plugins via direct download
- **GitHub:** Custom/private plugins with tag/branch support
- **Direct URLs:** Premium/commercial plugins via HTTPS

## Important Constraints

### Technical Constraints
- **Root Access Required:** Plugin operations require sudo/root
- **Linux Only:** Ubuntu 20.04/22.04/24.04, Debian 11/12
- **Dependencies:** WP-CLI, curl, unzip, rsync, jq must be installed
- **Disk Space:** Minimum 2GB free space
- **PHP-FPM Sockets:** Follow WordOps naming convention (php83-fpm.sock, not php8.3-fpm.sock)

### Architectural Constraints
- **No Custom Nginx Templates:** Must use WordOps' existing modular includes
- **No symlink following in set_permissions:** Use `followlinks=False` in os.walk()
- **Atomic Updates Only:** Never modify live WordPress files directly
- **Read-Only Shared Content:** Shared plugins/themes read-only from web user

### Security Constraints
- **No secrets in config files:** Use environment variables for tokens (GITHUB_TOKEN)
- **Database credentials per-site:** Never share database passwords
- **Unique Redis prefixes:** Each site must have unique cache prefix
- **File permissions:** wp-config.php 640, uploads 755/644

## External Dependencies

### System Services
- **Nginx:** Web server with modular configuration
- **PHP-FPM:** PHP processing (multiple versions supported)
- **MySQL/MariaDB:** Database server
- **Redis:** Object caching (optional)
- **systemd:** Service management

### External APIs
- **WordPress.org:** Plugin/theme downloads
- **GitHub API:** Repository downloads (rate limited: 60/hr unauthenticated, 5000/hr authenticated)
- **Let's Encrypt:** SSL certificate issuance via WOAcme

### WordOps Core Dependencies
- WordOps version 3.20.0 or higher
- SQLite database at `/var/lib/wo/dbase.db`
- Nginx templates at `/var/lib/wo/templates/`
- Service configurations managed by WordOps stack commands
