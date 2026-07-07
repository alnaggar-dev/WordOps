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

Multi-tenancy has three configuration layers:

- `/etc/wo/plugins.d/multitenancy.conf` configures infrastructure and download sources only: shared root, release retention, WordPress/PHP defaults, health thresholds, and where plugin/theme zip files come from.
- `/var/www/shared/config/baseline.json` is the source of truth for the active baseline: active plugins, active theme, and fleet-wide WordPress `options`. Site creation and `wo multitenancy apply` enforce this file through wp-cli.
- `/var/www/shared/config/wp-config-shared.php` contains PHP constants and shared PHP config that every generated tenant loads.

Each generated tenant `wp-config.php` does a `require_once` of `/var/www/shared/config/wp-config-shared.php` before defining its DB constants. The require is guarded by `WO_BYPASS_SHARED_CONFIG`, and risky paths lint the shared file with `php -l` before proceeding. The shared core is `current -> releases/wp-<timestamp>`; `update` builds a new release and repoints `current`, while `rollback` repoints it to the previous release.

## Quick start

1. Configure shared infrastructure and download sources in `/etc/wo/plugins.d/multitenancy.conf`.

    ```ini
    [multitenancy]
    enable_plugin = true
    shared_root = /var/www/shared

    [wordpress_plugins]
    redis-cache = latest
    nginx-helper = latest

    [wordpress_themes]
    twentytwentyfour = latest
    ```

2. Initialize the shared infrastructure. This creates `/var/www/shared/config/baseline.json` only if it is missing.

    ```bash
    wo multitenancy init
    ```

3. Choose the baseline activation state with CLI helpers, or edit `baseline.json` by hand.

    ```bash
    wo multitenancy add-plugin redis-cache
    wo multitenancy add-plugin nginx-helper
    wo multitenancy set-theme twentytwentyfour
    ```

4. Create sites. New sites receive the plugins, theme, and options from `baseline.json`.

    ```bash
    wo multitenancy create example.com --php84 --wpfc
    wo multitenancy create ssl.example.com --php84 --wpfc -le
    wo multitenancy create redis.example.com --php84 --wpredis
    ```

## Install & activate

This plugin ships with this fork's `wo` CLI (see `FORK.md` for fork install and update details). Update the CLI from the fork first:

```bash
wo update --force
```

Enable the plugin and configure infrastructure/download sources through `/etc/wo/plugins.d/multitenancy.conf`:

```ini
[multitenancy]
enable_plugin = true
shared_root = /var/www/shared
keep_releases = 3
wp_version = latest
php_version = 8.4
admin_email = admin@example.com

[wordpress_plugins]
redis-cache = latest
nginx-helper = latest

[wordpress_themes]
twentytwentyfour = latest
```

Initialize the shared core, baseline file, config, database metadata, permissions, and git tracking:

```bash
wo multitenancy init
```

Then set the baseline activation state with `add-plugin`, `set-theme`, or by editing `/var/www/shared/config/baseline.json`. Re-running `wo multitenancy init --force` re-downloads every configured plugin and theme from its source, leaving a timestamped backup of each previous copy under `backups/assets/`, and repairs shared infrastructure; it never overwrites an existing `baseline.json`. Unlike `update`, a mid-seed failure is per-asset best-effort rather than an all-or-nothing rollback. It also removes a legacy `wo-baseline-enforcer.php` MU-plugin if one is present.

## Configuration

Configuration lives at `/etc/wo/plugins.d/multitenancy.conf`. This file controls infrastructure settings and download sources only; it does not control which plugins or theme are active on tenants.

| Key | Default | Meaning |
| --- | --- | --- |
| `enable_plugin` | — | Cement plugin-loader gate; the plugin only loads when set to `true`. |
| `shared_root` | `/var/www/shared` | Shared core, content, config, baseline, and release root. |
| `keep_releases` | `3` | Number of WordPress core releases kept for rollback. |
| `keep_asset_backups` | `3` | Per-plugin/theme backups kept under `backups/assets/` after `init --force`, `update`, `update-plugin`, and `update-theme`. `0` keeps none; a negative value disables pruning. |
| `wp_version` | `latest` | WordPress core version downloaded by `init` and `update`. `latest`, an exact version (e.g. `6.5.2`), or `nightly`; passed to `wp core download --version=...` when pinned. |
| `php_version` | `8.4` | Default PHP version when the CLI/site does not specify one. |
| `admin_email` | `admin@example.com` | Fallback admin email for site creation. |
| `min_free_space_gb` | `2` | Free-disk threshold (GB) below which the `health` disk check warns. |

Defaults are the code fallbacks used when a key is missing. The packaged conf in this fork lists WordPress.org plugin sources in `[wordpress_plugins]` and sources `woodmart`/`woodmart-child` from `[github_themes]`; the active baseline lives in `/var/www/shared/config/baseline.json`.

Optional source sections define plugins and themes downloaded from WordPress.org, GitHub, or direct zip URLs:

| Section | Value format |
| --- | --- |
| `[wordpress_plugins]` | `slug = latest` |
| `[wordpress_themes]` | `slug = latest` or `slug = <version>` |
| `[github_plugins]` | `slug = user/repo,tag|branch,ref` |
| `[github_themes]` | `slug = user/repo,tag|branch,ref` |
| `[url_plugins]` | `slug = https://example.com/plugin.zip` |
| `[url_themes]` | `slug = https://example.com/theme.zip` |

```ini
[multitenancy]
enable_plugin = true
shared_root = /var/www/shared
keep_releases = 3
wp_version = latest
php_version = 8.4
admin_email = admin@example.com

[wordpress_plugins]
redis-cache = latest
nginx-helper = latest

[wordpress_themes]
twentytwentyfour = latest

[github_plugins]
private-plugin = owner/private-plugin,tag,1.2.3
branch-plugin = owner/branch-plugin,branch,main

[url_themes]
custom-theme = https://example.com/custom-theme.zip
```

Ordering quirk: put all `[multitenancy]` scalar keys before any `[wordpress_plugins]`, `[wordpress_themes]`, `[github_*]`, or `[url_*]` section. Place the source sections at the end of the file so later scalar keys are not parsed into the wrong section.

For private GitHub repositories, token resolution is `GH_TOKEN`, then `GITHUB_TOKEN`, then `gh auth token`. The token is sent as `Authorization: Bearer <token>`.

Older templates may contain extra keys such as `baseline_plugins`, `baseline_theme`, `auto_ssl`, `default_cache`, `enable_hsts`, `wp_memory_limit`, `disable_file_edit`, and others. Legacy `baseline_plugins`/`baseline_theme` values are read only as one-time bootstrap seeds when `wo multitenancy init` must create a missing `baseline.json`; current activation is controlled by `baseline.json`.

## Baseline configuration

`/var/www/shared/config/baseline.json` is the git-tracked source of truth for tenant activation, fleet-wide WordPress options, and durable CLI-added plugin/theme source metadata:

```json
{
  "version": 3,
  "generated": "2026-07-04T12:00:00Z",
  "plugins": [
    "redis-cache",
    "nginx-helper",
    "custom-github",
    "premium-url"
  ],
  "theme": "twentytwentyfour",
  "sources": {
    "plugins": {
      "redis-cache": { "type": "wordpress", "version": "latest" },
      "custom-github": { "type": "github", "repo": "owner/repo", "ref_type": "branch", "ref": "main" },
      "premium-url": { "type": "url", "url": "https://example.com/plugin.zip" }
    },
    "themes": {
      "twentytwentyfour": { "type": "wordpress", "version": "latest" },
      "custom-theme": { "type": "github", "repo": "owner/theme", "ref_type": "default", "ref": null }
    }
  },
  "options": {
    "blog_public": false,
    "timezone_string": "UTC",
    "woocommerce_allowed_countries": ["US", "CA"],
    "my_plugin_settings": {
      "enabled": true,
      "mode": "fleet"
    }
  }
}
```

`plugins` is the additive activation list unless `apply --prune` is used. `theme` is the active theme slug. `options` is applied with `wp option update` during site creation and `wo multitenancy apply`: scalar values become strings (`true`/`false` become `1`/`0`), while arrays and objects are written as JSON.

`sources` is optional and records where shared plugins and themes are downloaded from. Source entries use one of these shapes: WordPress.org `{ "type": "wordpress", "version": "latest" }` or a pinned version, GitHub `{ "type": "github", "repo": "owner/repo", "ref_type": "branch" | "tag" | "default", "ref": "main" | "v1.2.3" | null }`, or URL `{ "type": "url", "url": "https://example.com/plugin.zip" }`. `baseline.json` is now the durable source metadata for CLI-added plugins/themes, while `/etc/wo/plugins.d/multitenancy.conf` remains the packaged/operator source catalog and fallback for older baselines.

`wo multitenancy init` creates `baseline.json` only when the file is missing. It never overwrites an existing baseline, even with `--force`. The first file is a starting template: legacy `baseline_plugins` or `baseline_theme` keys take precedence when present; otherwise plugins are seeded from the keys in `[wordpress_plugins]`, `[github_plugins]`, and `[url_plugins]`, and the theme is seeded from the first `[wordpress_themes]` entry, the `-child` entry in `[github_themes]`, or the first available theme source. After that first write, the file is operator-owned and init does not rewrite it.

Hand-editing `baseline.json` is supported. Keep it under git tracking, bump `version` when changing plugins, theme, or options by hand so `validate` can report site drift correctly, and commit the change through the normal baseline workflow. `history` and `baseline-rollback --to-version=N` operate on `config/baseline.json` regardless of whether the change came from a CLI helper or a hand edit.

## Commands

Every command is `wo multitenancy <verb> [options]`. There is no `baseline` sub-group; `wo multitenancy baseline` alone only prints the current baseline and change hints.

### Lifecycle

| Command | Purpose |
| --- | --- |
| `wo multitenancy init [--force]` | Create shared directories, download core (honoring `wp_version`), create `baseline.json` only if it is missing, write `wp-config-shared.php`, initialize git tracking, switch release, set permissions, write DB config, and remove a legacy enforcer MU-plugin if present. Re-running with `--force` re-downloads all configured plugins/themes from their sources (backing up previous copies) but never overwrites an existing baseline. |
| `wo multitenancy create <domain> [flags]` | Create a shared-core tenant, then apply the baseline plugins, theme, and options from `baseline.json`. For `--wpfc`/`--wpredis` sites that include `nginx-helper` in the baseline, it also enables Nginx Helper cache purging automatically. See [create options](#create-options). |
| `wo multitenancy update [--force]` | Download a new core honoring `wp_version` from config, refresh all shared plugin/theme sources from baseline metadata or config fallback, restore shared assets if a download/promote fails or the canary aborts, then back up and switch the core release and clear caches. `--force` skips the canary abort. This command does not bump the baseline version. |
| `wo multitenancy rollback [--force]` | Switch `current` back to the previous WordPress core release only. It does not roll back plugin/theme updates after a successful update command. Failed bulk updates restore shared asset backups automatically before returning. `--force` skips confirmation. |
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
| `wo multitenancy update-plugin <slug>` | Re-fetch a plugin from baseline `sources` metadata first, then `/etc/wo/plugins.d/multitenancy.conf` fallback. Shared plugin files become live for all sites immediately. |
| `wo multitenancy update-theme` | Re-fetch the configured baseline theme from baseline `sources` metadata first, then `/etc/wo/plugins.d/multitenancy.conf` fallback. Shared theme files become live for all sites immediately. Takes no slug. |
| `wo multitenancy set-theme <slug> [--apply-now]` | Set an already-present shared theme as the baseline default, commit it, and optionally apply. |
| `wo multitenancy apply [--dry-run] [--prune] [--verbose]` | Apply the current baseline to every enabled site by activating plugins, activating the theme, and updating `options` through wp-cli. Default behavior is additive: plugins already active but absent from the baseline stay active. `--prune` is destructive and deactivates active plugins not listed in `baseline.json`; run `--dry-run --prune` first to see the exact would-be-deactivated set. For `--wpfc`/`--wpredis` sites with `nginx-helper` in the baseline, it also (re)enables Nginx Helper cache purging. Reports attempted, succeeded, and failed sites; clears caches globally unless dry-run. |
| `wo multitenancy history` | Show the last 20 git commits of `config/baseline.json`. |
| `wo multitenancy baseline-rollback --to-version=N [--apply-now] [--force]` | Find the git commit for baseline version `N`, check out `baseline.json` from it, commit the rollback, and optionally apply to sites. This can restore an older baseline without `sources`; afterward updates fall back to `/etc/wo/plugins.d/multitenancy.conf`, and per-item updates fail for slugs still lacking a source. |

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
| `--admin-user` | WordPress admin username. Default: `SuperDuper`. |
| `--admin-email` | WordPress admin email. Falls back to `admin_email` in config. |

If no PHP flag is passed, the default comes from `php_version` in config, which defaults to 8.4. If no cache flag is passed, the site is created with basic/no cache. SSL is `-le` or `--letsencrypt`; a copied `—le` with an em dash causes `unrecognized arguments`.

Cache purging is automatic: when a site uses `--wpfc` or `--wpredis` and `nginx-helper` is in the baseline `plugins`, both `create` and `apply` seed the Nginx Helper option `rt_wp_nginx_helper_options` with purging enabled and the matching cache method (`enable_fastcgi` or `enable_redis`). Because Nginx Helper 2.3.4+ also gates the purge button behind a custom capability that WP-CLI plugin activation does not grant (activation only adds it for a logged-in admin), `create` and `apply` additionally grant the `Nginx Helper | Purge cache` and `Nginx Helper | Config` capabilities to the administrator role whenever `nginx-helper` is in the baseline — without this the button reports "you do not have the necessary privileges" even with purge enabled. You no longer need to open wp-admin → Nginx Helper and tick "Enable Purge" per site. To retrofit sites created before this behavior, run `wo multitenancy apply`.

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
│   └── baseline.json                # active plugins/theme, options, version
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

Core update and rollback are atomic symlink operations. `update` builds a new `releases/wp-<timestamp>` tree and repoints `current`; `rollback` repoints `current` to the previous release. By default, `keep_releases = 3` keeps three releases for rollback. When `wp_version` pins a version, `update` re-downloads that pinned version rather than the latest; change the pin or set it back to `latest` to move the core forward.

Baseline changes are git-committed under `shared_root/.git`, with tracking limited to `config/baseline.json`. `history` shows recent baseline commits, and `baseline-rollback --to-version=N` checks out `baseline.json` from the commit for version `N` and commits that rollback. `update` changes the shared core and shared plugin/theme files only; it does not bump the baseline version.

Plugin and theme refreshes leave a timestamped backup of the previous copy under `shared_root/backups/assets/<stamp>/<plugins|themes>/<slug>`. On success, `init --force`, `update`, `update-plugin`, and `update-theme` prune these to the newest `keep_asset_backups` (default 3) per asset; set it to `0` to keep none, or a negative value to disable pruning.

Sites do not auto-upgrade on visit. Run `wo multitenancy apply`, or use `--apply-now` on baseline-changing commands that support it. By default `apply` is additive; include `--prune` only when you intentionally want plugins outside `baseline.json` deactivated.

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
| PHP-FPM unavailable | Check `systemctl status php8.4-fpm` or the service matching the site's PHP version. |
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
