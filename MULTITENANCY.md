# WordOps Multi-tenancy Plugin

The WordOps multi-tenancy plugin lets many WordPress sites share one WordPress core while each site keeps its own `wp-config.php`, database credentials, salts, Redis prefix, uploads, cache, nginx vhost, and cache configuration. One shared core can save about 90% of core disk usage, because one roughly 60 MB core serves the fleet instead of 60 MB per site. Core updates and rollbacks are atomic symlink swaps: `current` moves between timestamped releases.

Operating model: this fork assumes a trust model of one — a solo operator running their own code on their own fleet. Per-tenant isolation and multi-operator workflows are explicit non-goals.

## Model

| Area | Shared | Per site |
| --- | --- | --- |
| WordPress core | `/var/www/shared/current` | `htdocs/wp` symlink to the shared core |
| Releases | `/var/www/shared/releases/wp-<timestamp>/` | Site points at whichever release `current` selects |
| Plugins, themes, MU plugins, languages | `/var/www/shared/wp-content/{plugins,themes,mu-plugins,languages}/` | `htdocs/wp-content/{plugins,themes,mu-plugins,languages}` symlink to shared tree |
| Fleet config | `/var/www/shared/config/wp-config-shared.php` | Required by each generated tenant config |
| Baseline | `/var/www/shared/config/baseline.json` | Site tracking stores the baseline version applied to that site |
| Site config | Not shared | `htdocs/wp-config.php` with DB credentials, salts, unique Redis prefix |
| Mutable content | Not shared | `wp-content/uploads`, `wp-content/cache`, `wp-content/upgrade` real directories |
| Web server | Not shared | nginx vhost and cache configuration |

Each generated tenant `wp-config.php` does a `require_once` of `/var/www/shared/config/wp-config-shared.php` before defining its DB constants. The require is guarded by `WO_BYPASS_SHARED_CONFIG`, and risky paths lint the shared file with `php -l` before proceeding. The shared core is `current -> releases/wp-<timestamp>`; `update` builds a new release and repoints `current`, while `rollback` repoints it to the previous release.

## Quick start

1. Write `/etc/wo/plugins.d/multitenancy.conf`.

    ```ini
    [multitenancy]
    enable_plugin = true
    shared_root = /var/www/shared
    baseline_plugins = redis-cache,nginx-helper
    baseline_theme = twentytwentyfour
    ```

2. Initialize the shared infrastructure.

    ```bash
    wo multitenancy init
    ```

3. Create sites.

    ```bash
    wo multitenancy create example.com --php83 --wpfc
    wo multitenancy create ssl.example.com --php83 --wpfc -le
    wo multitenancy create redis.example.com --php83 --wpredis
    ```

## Install & activate

This plugin ships with this fork's `wo` CLI (see `FORK.md` for fork install and update details). Update the CLI from the fork first:

```bash
wo update --force
```

Enable the plugin through `/etc/wo/plugins.d/multitenancy.conf`:

```ini
[multitenancy]
enable_plugin = true
shared_root = /var/www/shared
keep_releases = 3
php_version = 8.3
baseline_plugins = redis-cache,nginx-helper
baseline_theme = twentytwentyfour
admin_email = admin@example.com
```

Initialize the shared core, baseline, config, database metadata, permissions, and git tracking:

```bash
wo multitenancy init
```

Re-running `wo multitenancy init --force` is safe. It also removes a legacy `wo-baseline-enforcer.php` MU-plugin if one is present.

## Configuration

Configuration lives at `/etc/wo/plugins.d/multitenancy.conf`.

| Key | Default | Meaning |
| --- | --- | --- |
| `enable_plugin` | — | Cement plugin-loader gate; the plugin only loads when set to `true`. |
| `shared_root` | `/var/www/shared` | Shared core, content, config, baseline, and release root. |
| `keep_releases` | `3` | Number of WordPress core releases kept for rollback. |
| `php_version` | `8.3` | Default PHP version when the CLI/site does not specify one. |
| `baseline_plugins` | `nginx-helper,redis-cache` | Comma-separated plugin slugs seeded during `init`. Fetched from WordPress.org unless the slug also appears in a GitHub/URL section. |
| `baseline_theme` | `twentytwentyfour` | Theme slug seeded during `init`. Fetched from WordPress.org unless provided by a GitHub/URL section. |
| `admin_email` | `admin@example.com` | Fallback admin email for site creation. |
| `min_free_space_gb` | `2` | Free-disk threshold (GB) below which the `health` disk check warns. |

Defaults are the code fallbacks used when a key is missing. The packaged conf in this fork sets `baseline_plugins = nginx-helper,woocommerce,plausible-analytics,safe-svg` and `baseline_theme = woodmart-child` (provided by `[github_themes]`, so WordPress.org is skipped for it).

Optional source sections define plugins and themes that come from GitHub or direct zip URLs:

| Section | Value format |
| --- | --- |
| `[github_plugins]` | `slug = user/repo,tag|branch,ref` |
| `[github_themes]` | `slug = user/repo,tag|branch,ref` |
| `[url_plugins]` | `slug = https://example.com/plugin.zip` |
| `[url_themes]` | `slug = https://example.com/theme.zip` |

```ini
[multitenancy]
enable_plugin = true
shared_root = /var/www/shared
keep_releases = 3
php_version = 8.3
baseline_plugins = redis-cache,nginx-helper
baseline_theme = twentytwentyfour
admin_email = admin@example.com

[github_plugins]
private-plugin = owner/private-plugin,tag,1.2.3
branch-plugin = owner/branch-plugin,branch,main

[url_themes]
custom-theme = https://example.com/custom-theme.zip
```

Ordering quirk: put all `[multitenancy]` scalar keys before any `[github_*]` or `[url_*]` section. Place the GitHub and URL sections at the end of the file so later scalar keys are not parsed into the wrong section.

For private GitHub repositories, token resolution is `GH_TOKEN`, then `GITHUB_TOKEN`, then `gh auth token`. The token is sent as `Authorization: Bearer <token>`.

Older templates may contain extra keys such as `auto_ssl`, `default_cache`, `enable_hsts`, `wp_memory_limit`, `disable_file_edit`, and others that the current code ignores.

## Commands

Every command is `wo multitenancy <verb> [options]`. There is no `baseline` sub-group; `wo multitenancy baseline` alone only prints the current baseline and change hints.

### Lifecycle

| Command | Purpose |
| --- | --- |
| `wo multitenancy init [--force]` | Create shared directories, download core, seed baseline plugins/themes, write `baseline.json` and `wp-config-shared.php`, initialize git tracking, switch release, set permissions, write DB config, and remove a legacy enforcer MU-plugin if present. Re-running with `--force` is safe. |
| `wo multitenancy create <domain> [flags]` | Create a shared-core tenant. See [create options](#create-options). |
| `wo multitenancy update [--force]` | Download a new core, update shared plugins/themes, canary-test, back up and switch release, clear caches, and bump the baseline version. `--force` skips the canary abort. |
| `wo multitenancy rollback [--force]` | Switch `current` back to the previous release. `--force` skips confirmation. |
| `wo multitenancy delete <domain> [--force]` | Delete a tenant with `wo site delete ... --no-prompt`, then remove its multi-tenancy tracking row. |
| `wo multitenancy remove [--force]` | Tear down the entire shared infrastructure. It refuses while sites remain unless `--force` is used. |

### Inspection

| Command | Purpose |
| --- | --- |
| `wo multitenancy status` | Show infrastructure summary, health checks, releases, sites, disk usage, and baseline. |
| `wo multitenancy list` | Show a table of shared sites with domain, PHP, cache, SSL, and enabled state. |
| `wo multitenancy validate` | Check `baseline.json`, plugin/theme files, and sites whose baseline version is behind. |
| `wo multitenancy health [--json] [--site=<domain>]` | Run health checks for shared infrastructure, database, disk space, PHP-FPM, nginx, and site HTTP reachability. |

### Baseline

| Command | Purpose |
| --- | --- |
| `wo multitenancy baseline` | Show current baseline version, plugins, and theme. Read-only. Extra arguments are ignored. |
| `wo multitenancy add-plugin <slug> [--github=user/repo] [--branch=<b> \| --tag=<t>] [--url=<zip>] [--apply-now]` | Add a plugin to `baseline.json` and commit it. Default source is WordPress.org; GitHub and URL zip sources are supported. `--apply-now` rolls out immediately. |
| `wo multitenancy add-theme <slug> [--github=user/repo] [--branch=<b> \| --tag=<t>] [--url=<zip>] [--set-default] [--apply-now]` | Add a theme from WordPress.org, GitHub, or URL zip and commit it. `--set-default` makes it the baseline active theme. `--apply-now` rolls out when the theme is also default. |
| `wo multitenancy remove-plugin <slug> [--apply-now]` | Remove a plugin from the baseline and commit it. This is baseline-only; it does not live-deactivate the plugin on sites. |
| `wo multitenancy update-plugin <slug>` | Re-fetch a plugin from its original source. |
| `wo multitenancy update-theme` | Re-fetch the configured baseline theme from its original source. Takes no slug. |
| `wo multitenancy set-theme <slug> [--apply-now]` | Set an already-present shared theme as the baseline default, commit it, and optionally apply. |
| `wo multitenancy apply [--dry-run] [--verbose]` | Apply the current baseline to every enabled site by activating plugins and theme through wp-cli. Reports attempted, succeeded, and failed sites; clears caches globally unless dry-run. On plugin activation failure it restores that site's previous active plugins and reports failure. |
| `wo multitenancy history` | Show the last 20 git commits of `config/baseline.json`. |
| `wo multitenancy baseline-rollback --to-version=N [--apply-now] [--force]` | Find the git commit for baseline version `N`, check out `baseline.json` from it, commit the rollback, and optionally apply to sites. |

### Shared config & maintenance

| Command | Purpose |
| --- | --- |
| `wo multitenancy shared-config --action edit` | Safely edit `wp-config-shared.php`. This is the only supported shared-config action. It uses `$EDITOR` with fallback `vi`, creates a timestamped `.bak`, keeps the 10 newest backups, lints with `php -l`, reloads all installed PHP-FPM versions and nginx on success, and auto-restores the backup on syntax error. |
| `wo multitenancy maintenance --enable\|--disable [--site=<domain> \| --all] [--message="..."]` | Add or remove an nginx 503 maintenance page for one site or the whole fleet, then reload nginx once. Exactly one of enable/disable and exactly one of site/all is required. |

### Preflight

`init`, `create`, `update`, and `apply` run `php -l` on `wp-config-shared.php` before continuing and refuse to proceed on a syntax error.

## create options

| Option | Meaning |
| --- | --- |
| `<domain>` | Required positional domain. |
| `--php74` | Use PHP 7.4. |
| `--php80` | Use PHP 8.0. |
| `--php81` | Use PHP 8.1. |
| `--php82` | Use PHP 8.2. |
| `--php83` | Use PHP 8.3. |
| `--php84` | Use PHP 8.4. |
| `--wpfc` | Use FastCGI cache. |
| `--wpredis` | Use Redis cache. |
| `--wprocket` | Use WP Rocket. |
| `--wpce` | Use Cache Enabler. |
| `--wpsc` | Use WP Super Cache. |
| `-le` | Enable Let's Encrypt SSL. |
| `--letsencrypt` | Enable Let's Encrypt SSL. |
| `--hsts` | Enable HSTS. |
| `--dns[=dns_cf]` | Use wildcard/DNS mode. |
| `--admin-user` | WordPress admin username. Default: `admin`. |
| `--admin-email` | WordPress admin email. Falls back to `admin_email` in config. |

If no PHP flag is passed, the default comes from `php_version` in config, which defaults to 8.3. If no cache flag is passed, the site is created with basic/no cache. SSL is `-le` or `--letsencrypt`; a copied `—le` with an em dash causes `unrecognized arguments`.

`--force` and `--shared` are accepted globally, but `create` ignores them. A created multi-tenancy site always uses shared core. There is no command to convert an existing standalone WordPress site onto the shared core; shared-core sites must be created fresh with `create`.

## Directory structure

Shared tree:

```text
/var/www/shared/
├── current -> releases/wp-YYYYMMDD-HHMMSS
├── releases/
│   └── wp-YYYYMMDD-HHMMSS/          # full WP core (+ router wp-config.php); keep_releases kept
├── wp-content/
│   ├── plugins/                     # shared, baseline-managed
│   ├── themes/                      # shared, baseline-managed
│   ├── mu-plugins/                  # shared
│   └── languages/                   # shared
├── config/
│   ├── wp-config-shared.php         # fleet-wide, require_once'd
│   └── baseline.json                # baseline set + version
└── .git/                            # tracks config/baseline.json only
```

Per-site tree:

```text
/var/www/<domain>/
├── htdocs/
│   ├── wp-config.php                # per-site DB creds, salts, Redis prefix
│   ├── wp -> /var/www/shared/current
│   ├── wp-login.php -> shared core
│   ├── wp-admin -> shared core
│   ├── wp-includes -> shared core
│   ├── wp-cron.php -> shared core
│   ├── xmlrpc.php -> shared core
│   ├── wp-comments-post.php -> shared core
│   ├── wp-settings.php -> shared core
│   ├── index.php                    # generated; requires wp/wp-blog-header.php
│   └── wp-content/
│       ├── plugins -> shared tree
│       ├── themes -> shared tree
│       ├── mu-plugins -> shared tree
│       ├── languages -> shared tree
│       ├── uploads/                 # real per-site dir
│       ├── cache/                   # real per-site dir
│       └── upgrade/                 # real per-site dir
└── conf/nginx/                      # vhost + cache config; +maintenance include
```

## Updates, rollback & baseline history

Core update and rollback are atomic symlink operations. `update` builds a new `releases/wp-<timestamp>` tree and repoints `current`; `rollback` repoints `current` to the previous release. By default, `keep_releases = 3` keeps three releases for rollback.

Baseline changes are git-committed under `shared_root/.git`, with tracking limited to `config/baseline.json`. `history` shows recent baseline commits, and `baseline-rollback --to-version=N` checks out `baseline.json` from the commit for version `N` and commits that rollback.

Sites do not auto-upgrade on visit. Run `wo multitenancy apply`, or use `--apply-now` on baseline-changing commands that support it.

## Shared config safety & recovery

Use `wo multitenancy shared-config --action edit` for shared config changes. The command creates a timestamped backup, keeps the 10 newest backups, opens `$EDITOR` or `vi`, lints the saved file with `php -l`, reloads all installed PHP-FPM versions and nginx on success, and restores the backup automatically on syntax error.

If a shared-config edit still downs a site, edit that site's `/var/www/<domain>/htdocs/wp-config.php` and set:

```php
define('WO_BYPASS_SHARED_CONFIG', true);
```

This bypass brings the site up while you fix `/var/www/shared/config/wp-config-shared.php`. Remove the bypass after the shared file is fixed.

## Tenancy caveat

Plugins and themes are shared read-only symlinks. A plugin that writes into its own plugin directory, such as license files, generated assets, or caches under `wp-content/plugins/<slug>/`, can collide across sites or fail on the read-only tree. Keep mutable per-site state in `uploads`, and vet plugins before adding them to the baseline; most plugins store state in the database or uploads and are fine.

## Troubleshooting

| Symptom | Cause/fix |
| --- | --- |
| Site cannot find WordPress core | Check the site core symlink with `ls -la /var/www/<d>/htdocs/wp` and check `/var/www/shared/current`. |
| nginx config or vhost issue | Run `nginx -t && systemctl reload nginx`. |
| PHP-FPM unavailable | Check `systemctl status php8.3-fpm` or the service matching the site's PHP version. |
| Uploads fail or media cannot be written | Fix uploads ownership with `chown -R www-data:www-data …/wp-content/uploads`. |
| SSL flag rejected as `unrecognized arguments` | Use `-le` or `--letsencrypt`; replace any copied em dash with a normal hyphen. |
| Redis cross-talk or lost cache isolation | Each site needs a unique `redis_prefix`, enforced in the DB. Recreate a site whose `wp-config.php` lost its prefix. |
| `validate` flags a fresh site as behind | The create-time baseline version may not have been recorded; run `wo multitenancy apply`. |
| Shared-config syntax error | Run `wo multitenancy shared-config --action edit` to use linted editing, or temporarily set `WO_BYPASS_SHARED_CONFIG` in the affected site's config while fixing the shared file. |

## Logs

All multi-tenancy logging goes to `/var/log/wo/wordops.log`.

```bash
wo --debug multitenancy <cmd>
tail -f /var/log/wo/wordops.log
```
