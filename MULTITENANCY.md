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
| Site config | Not shared | `htdocs/wp-config.php` with DB credentials, salts, unique Redis prefix and dedicated Redis database |
| Mutable content | Not shared | `wp-content/uploads`, `wp-content/cache`, `wp-content/upgrade` real directories |
| Web server | Not shared | nginx vhost and cache configuration |

Multi-tenancy has three configuration layers:

- `/etc/wo/plugins.d/multitenancy.conf` configures infrastructure and download sources only: shared root, release retention, WordPress/PHP defaults, health thresholds, and where plugin/theme zip files come from.
- `/var/www/shared/config/baseline.json` is the source of truth for the active baseline: active plugins, active theme, and fleet-wide WordPress `options`. Site creation and `wo multitenancy apply` enforce this file through wp-cli.
- `/var/www/shared/config/wp-config-shared.php` contains PHP constants and shared PHP config that every generated tenant loads.

Each generated tenant `wp-config.php` does a `require_once` of `/var/www/shared/config/wp-config-shared.php` before defining its DB constants. The require is guarded by `WO_BYPASS_SHARED_CONFIG`, and risky paths lint the shared file with `php -l` before proceeding. The shared core is `current -> releases/wp-<timestamp>`; `update` builds a new release and repoints `current`, while `rollback` repoints it to the previous release.

Generated shared config disables loopback WP-Cron (`DISABLE_WP_CRON` true). WordOps maintains `/etc/cron.d/wo-multitenancy` with one every-minute `wp cron event run --due-now` entry per enabled tenant, each prefixed with a deterministic per-domain sleep of 0–59 seconds (CRC32 of the domain, modulo 60) so fleet cron runs are splayed across the minute instead of starting simultaneously; `create`, `delete`, `rename`, and non-dry-run `apply` regenerate the whole file (a failed regeneration makes the command exit nonzero with a hint to run `wo multitenancy apply`), so `wo multitenancy apply` retrofits existing fleets. Because the shared define is guarded, a tenant may define `DISABLE_WP_CRON` as `false` in its own `wp-config.php` before the shared require, but that only re-enables loopback cron in addition to the managed entry — there is no per-site opt-out of `/etc/cron.d/wo-multitenancy`. The PHP stack defaults explicitly set `opcache.enable=1` and `opcache.interned_strings_buffer=64`.

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
    wo multitenancy create redis.example.com --php84 --wpredis --force  # --wpredis is blocked by default; see create options
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
| `apply_workers` | `4` | Parallel workers for `wo multitenancy apply` (clamped to 1–16). Per-site wp-cli work runs concurrently; database tracking updates stay serialized. |
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
| `wo multitenancy update [--force]` | Stage core and compare `$wp_db_version`. A higher schema gates HTTP, drains active PHP/DB work and cron sleepers, promotes assets, runs a loopback canary through the gate, takes quiescent tenant DB dumps, flips core, then runs supervised per-tenant `wp core update-db`. Equal schemas keep the original fast path. Pre-flip failures restore promoted assets before reopening traffic; restore failure intentionally leaves gates active. Post-flip failures stay gated and report partial/nonzero status. Before a schema-bumping flip, the pending tenant migrations are recorded in `config/pending-db-upgrades.json`; if the update is interrupted mid-migration, the next `update` run finishes the leftover tenant migrations from that ledger before doing anything else. `--force` only skips the canary abort. |
| `wo multitenancy rollback [--force]` | Switch `current` back to the previous WordPress core release only. WordPress DB migrations are forward-only: rollback neither reverses schema changes nor restores tenant dumps. It also does not roll back plugin/theme updates after a successful update command. `--force` skips confirmation. |
| `wo multitenancy delete <domain> [--force]` | Delete a tenant with `wo site delete ... --no-prompt`, then remove its multi-tenancy tracking row. |
| `wo multitenancy remove [--force]` | Tear down the entire shared infrastructure. It refuses while sites remain unless `--force` is used. |

### Inspection

| Command | Purpose |
| --- | --- |
| `wo multitenancy status` | Show infrastructure summary, health checks, releases, sites, disk usage, and baseline. |
| `wo multitenancy list` | Show a table of shared sites with domain, PHP, cache, SSL, and enabled state. |
| `wo multitenancy validate` | Check `baseline.json`, plugin/theme files, sites whose baseline version is behind, and warn when `config/pending-db-upgrades.json` reports tenants left mid-migration by an interrupted update. |
| `wo multitenancy health [--json] [--site=<domain>]` | Run health checks for shared infrastructure, database, disk space, PHP-FPM, nginx, and site HTTP reachability. Exit code: `0` healthy, `2` degraded, `1` unhealthy or uninitialized. |

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
| `wo multitenancy apply [--dry-run] [--prune] [--verbose]` | Apply the current baseline to every enabled site by activating plugins, activating the theme, and updating `options` through wp-cli. Sites are processed in parallel (`apply_workers`, default 4). Default behavior is additive: plugins already active but absent from the baseline stay active. `--prune` is destructive and deactivates active plugins not listed in `baseline.json`; run `--dry-run --prune` first to see the exact would-be-deactivated set. For `--wpfc`/`--wpredis` sites with `nginx-helper` in the baseline, it also (re)enables Nginx Helper cache purging. Reports attempted, succeeded, and failed sites; clears caches globally unless dry-run. Exits nonzero when any site fails. |
| `wo multitenancy history` | Show the last 20 git commits of `config/baseline.json`. |
| `wo multitenancy baseline-rollback --to-version=N [--apply-now] [--force]` | Find the git commit for baseline version `N`, restore that content into `baseline.json`, and commit it as a **new** baseline version (current + 1) so history stays linear and `validate` drift detection keeps working. Optionally apply to sites. This can restore an older baseline without `sources`; afterward updates fall back to `/etc/wo/plugins.d/multitenancy.conf`, and per-item updates fail for slugs still lacking a source. |

### Shared config & maintenance

| Command | Purpose |
| --- | --- |
| `wo multitenancy shared-config --action edit` | Safely edit `wp-config-shared.php`. This is the only supported shared-config action. It uses `$EDITOR` with fallback `vi`, creates a timestamped `.bak`, keeps the 10 newest backups, lints with `php -l`, reloads all installed PHP-FPM versions and nginx on success, and auto-restores the backup on syntax error. |
| `wo multitenancy maintenance --enable\|--disable [--site=<domain> \| --all] [--message="..."]` | Add or remove an nginx 503 maintenance page for one site or the whole fleet, then reload nginx once. Exactly one of enable/disable and exactly one of site/all is required. Exits nonzero when any site or the nginx reload fails. |

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
| `--wpredis` | Use Redis cache. **Blocked by default**: the bundled `nginx-wo` 1.30.4 build segfaults its workers on the srcache/redis2 page-cache path, taking down all tenants. Pass `--force` to override at your own risk; prefer `--wpfc`. |
| `--wprocket` | Use WP Rocket. |
| `--wpce` | Use Cache Enabler. |
| `--wpsc` | Use WP Super Cache. |
| `-le` | Enable Let's Encrypt SSL. |
| `--letsencrypt` | Enable Let's Encrypt SSL. |
| `--hsts` | Enable HSTS. |
| `--dns[=dns_cf]` | Use wildcard/DNS mode. |
| `--admin-user` | WordPress admin username. Default: `SuperDuper`. |
| `--admin-email` | WordPress admin email. Falls back to `admin_email` in config. |

If no PHP flag is passed, the default comes from `php_version` in config, which defaults to 8.4. If no cache flag is passed, the site is created with basic/no cache. SSL is `-le` or `--letsencrypt`; a copied `—le` with an em dash causes `unrecognized arguments`. If Let's Encrypt setup fails, `create` does not abort or roll back the site: it continues, records the tenant as non-SSL, and leaves it on HTTP with a warning; when the failure is nginx configuration validation, the just-written SSL config is also removed so nginx stays valid. Rerun SSL setup after fixing the cause.

Cache purging is automatic: when a site uses `--wpfc` or `--wpredis` and `nginx-helper` is in the baseline `plugins`, both `create` and `apply` seed the Nginx Helper option `rt_wp_nginx_helper_options` with purging enabled and the matching cache method (`enable_fastcgi` or `enable_redis`). Because Nginx Helper 2.3.4+ also gates the purge button behind a custom capability that WP-CLI plugin activation does not grant (activation only adds it for a logged-in admin), `create` and `apply` additionally grant the `Nginx Helper | Purge cache` and `Nginx Helper | Config` capabilities to the administrator role whenever `nginx-helper` is in the baseline — without this the button reports "you do not have the necessary privileges" even with purge enabled. You no longer need to open wp-admin → Nginx Helper and tick "Enable Purge" per site. To retrofit sites created before this behavior, run `wo multitenancy apply`.

`--shared` is accepted globally but `create` ignores it; a created multi-tenancy site always uses shared core. `--force` on `create` overrides the `--wpredis` block and changes SSL setup: with an existing certificate it reinstalls it without the archived-certificate prompt, and for new issuance it skips the DNS-points-to-this-server precheck (DNS validation mode skips that precheck anyway). There is no command to convert an existing standalone WordPress site onto the shared core; shared-core sites must be created fresh with `create`.

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
│   ├── wp-config.php                # per-site DB creds, salts, Redis prefix + dedicated Redis database
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

After staging core, `update` parses `$wp_db_version` from both the active and staged `wp-includes/version.php`. Equal schema versions keep the existing fast update path. A lower staged schema aborts because automated database downgrade is unsupported; an unreadable or unparseable marker on either side also aborts rather than guessing.

For a higher staged schema, `update` validates every tracked domain, captures the current nginx worker PIDs, then renders the existing per-tenant nginx maintenance gate **fully closed** and reloads nginx. No local or external bypass exists during the first drain or asset promotion. A gate that existed before the update is preserved byte-for-byte and never modified. Gate files live in each tenant's real `/var/www/<domain>/conf/nginx/` and `htdocs/` tree, outside the shared release.

Immediately before the maintenance reload, `update` captures the current nginx worker PIDs. Because nginx reload is graceful, those old-generation workers remain alive until every request admitted before the gate finishes, including slow request bodies and requests not yet visible in FPM. The update then holds every managed `/tmp/wo-cron-<domain>.lock`, installs guarded cron lines, and waits for **all captured nginx workers to exit**. Only after that causal boundary does it require the PHP-FPM listen queue and active-worker count to be zero and tenant `information_schema.innodb_trx` count to be zero. The same wait covers the old cron runner's 60-second splay horizon. Nginx PIDs come from the process table, FPM queue/activity from WordOps' local JSON/full status endpoints, and transactions from MySQL; unreadable probes fail closed. The total bound is 330 seconds, slightly above the 300-second PHP request timeout. Timeout aborts before asset promotion and names the old PIDs, queued/active FPM work, or transactions that did not drain.

Only after the first causal drain does `update` promote shared assets. It then temporarily renders a true-socket loopback bypass using nginx `$realip_remote_addr` rather than header-rewritten `$remote_addr`, reloads nginx a second time, and runs the canary against old core plus new assets; external/Cloudflare traffic remains 503. Both this gated canary and the earlier ungated health canary append a unique `wo_mt_canary` query parameter and send `X-Requested-With: XMLHttpRequest`; WordOps' `map-wp` maps both signals into cache fetch/store bypass, so canonical redirects that drop the query remain cache-proof. Redirects are followed manually for at most five hops: before making the next request, `update` requires HTTP/HTTPS, the default port (80/443), and a host in the tenant's apex/`www` pair; rejected `Location` values are logged and never requested. Each gated hop remains a separate no-follow curl with `--proto =http,https` and both allowed hosts/ports pinned to loopback, preserving TLS SNI and preventing public-DNS, cross-host, non-default-port, or alternate-protocol escapes. A final response must be 2xx. Next it captures that bypass generation, rewrites every update-owned gate fully closed, reloads nginx a third time, and performs the same causal drain again (without another cron-horizon delay). This waits out canary/local stragglers, so no bypass exists during snapshots. Only then does it export point-in-time tenant dumps. Any pre-flip failure releases locks and restores assets **before** ungating. Canary failure and drain-2 timeout already have closed gates; if an exceptional bypass-stage abort occurs, cleanup closes the bypass first. Restore failure retains gates.

After the flip and new release record, each tenant runs `wp core update-db`; its cron lock is released only after that decision. Successful tenants have update-owned gates removed, failed tenants keep theirs, and one batch nginx reload applies successful ungates. Dumps accumulate and remain operator-managed. Upgrade post-flip paths never blanket-ungate.

Immediately before a schema-bumping flip, `update` atomically writes `config/pending-db-upgrades.json` listing the new release and every tenant awaiting `wp core update-db`; each tenant is removed from the ledger as its migration succeeds, and the file is deleted once empty. If the process dies mid-migration (crash, reboot, SIGKILL), `validate` warns about the leftover ledger and the next `update` run recovers first: it re-gates the listed tenants, runs their pending migrations, removes the recovery gates, and only then proceeds — or exits nonzero naming the tenants that still fail.

Each tenant's `wp core update-db` is supervised with a five-minute timeout. A timed-out WP/PHP/MySQL process is terminated by the subprocess runner, recorded as a failed tenant, left maintenance-gated, and has its cron lock released before the update continues to the next tenant.

Post-flip failures do not trigger an automatic rollback. The command still performs release/asset cleanup, reports each failed and still-gated domain, prints the exact `wo multitenancy maintenance --disable --site=<domain>` manual ungate command plus the `wp core update-db --path=… --allow-root` remediation, emits `result=partial` with the failed count and pre-upgrade backup directory, and exits nonzero.

`rollback` changes core files only. WordPress database migrations are forward-only, so it cannot reverse a schema migration. If `wp core update-db` ran, either validate that the older core works with the newer schema or import the appropriate pre-upgrade tenant dump from `shared_root/backups/db/<timestamp>/` with `wp db import`.

Baseline changes are git-committed under `shared_root/.git`, with tracking limited to `config/baseline.json`. `history` shows recent baseline commits, and `baseline-rollback --to-version=N` restores the `baseline.json` content from the commit for version `N` and commits it as a new baseline version (current + 1), keeping history linear. `update` changes the shared core and shared plugin/theme files only; it does not bump the baseline version.

Plugin and theme refreshes leave a timestamped backup of the previous copy under `shared_root/backups/assets/<stamp>/<plugins|themes>/<slug>`. On success, `init --force`, `update`, `update-plugin`, and `update-theme` prune these to the newest `keep_asset_backups` (default 3) per asset; set it to `0` to keep none, or a negative value to disable pruning.

**Plugin migration warning:** schema-gated automatic dumps cover WordPress core schema changes only. Before an `update` that ships plugins with their own schema or data migrations, manually run `wp db export /secure/backup/<domain>.sql --path=/var/www/<domain>/htdocs --allow-root` for every tenant. Reverting shared plugin code cannot undo those database changes.

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
| Redis cross-talk or lost cache isolation | Each site needs a unique `redis_prefix` and a dedicated `redis_db` (both enforced in the DB). Object Cache Pro flushes with `FLUSHDB`, so tenants sharing one Redis database wipe each other's cache — including OCP's metadata key, which triggers fleet-wide integrity-flush loops that keep every cache cold and make wp-admin run update/license checks inline on every page load. `wo multitenancy apply` backfills a dedicated database for tenants still on shared db 0; database 0 is reserved for standard sites. When the fleet outgrows the Redis `databases` limit, `create`/`apply` raise it in `/etc/redis/redis.conf` and restart Redis. |
| `validate` flags a fresh site as behind | The create-time baseline version may not have been recorded; run `wo multitenancy apply`. |
| Shared-config syntax error | Run `wo multitenancy shared-config --action edit` to use linted editing, or temporarily set `WO_BYPASS_SHARED_CONFIG` in the affected site's config while fixing the shared file. |

## Logs

Interactive multi-tenancy commands log to `/var/log/wo/wordops.log`. Scheduled backup jobs also append their stdout and stderr to `/var/log/wo/backup.log`.

```bash
wo --debug multitenancy <cmd>
tail -f /var/log/wo/wordops.log
tail -f /var/log/wo/backup.log  # exists after the first scheduled backup starts
```

## Fleet Backups (Cloudflare R2)

Fleet backups are encrypted restic snapshots stored in one Cloudflare R2
repository. The repository is shared by the whole multi-tenancy fleet, so
restic can deduplicate shared assets and similar database dumps while tags keep
each tenant addressable.

- **Databases:** one full logical `mariadb-dump` per enabled tenant every hour
  (the normal target is `:07`, so the RPO is one hour).
- **Files:** one daily snapshot of the recoverability set: tenant uploads,
  `wp-config.php`, nginx state, shared baseline/config/content, the tracking
  database, and `/etc/letsencrypt`.
- **Retention:** database snapshots keep `24h,7d,4w,3m`; file snapshots keep
  `7d,4w,6m`. The monthly tails are intentional late-discovery protection.
- **Restores:** replacement semantics are used throughout. Restoring a site
  makes its post-restore state equal to the selected snapshot: files are
  synchronized with deletion (`rsync --delete`) and the database is
  drop-and-recreated before import. A restore is never an overlay that leaves
  post-snapshot files or tables behind.

There is one repository, not one repository per tenant. The snapshot tags are
the tenant boundary: hourly database snapshots use `db` and `site:<domain>`;
daily files use `files`; safety snapshots add `pre-restore`,
`operation:<id>`, and (for fleet captures) `fleet`.

### Backup setup

1. Create a dedicated R2 bucket and an S3 API token with **Object Read & Write**
   scoped only to that bucket. Use separate buckets and tokens for test and
   production, and rotate any credential exposed in logs, chat, or shell
   history. The bucket MUST have **no lifecycle or expiry rules**. R2 lifecycle
   deletion removes restic pack files rather than applying a coherent snapshot
   policy and corrupts the repository. Retention belongs to restic alone.
2. Check whether this server already has backup credentials:

   ```bash
   test -f /etc/wo/backup.env \
     && grep '^RESTIC_REPOSITORY=' /etc/wo/backup.env \
     || echo 'No existing backup configuration'
   ```

   A complete `/etc/wo/backup.env` is reused: `backup init` will not prompt for
   replacement credentials or generate a new repository password. If it names
   an old or test repository, move it aside deliberately before continuing.
3. Run the setup command as root:

   ```bash
   wo multitenancy backup init
   ```

   `backup init` installs the official restic `0.19.1` binary at
   `/usr/local/bin/restic`, verifies the release SHA256 for the machine
   architecture before installing it, and refuses an unverified binary. This
   pinned release is new enough for `--stdin-from-command`,
   `--retry-lock`, and the restore behavior used here.
4. Answer the R2 prompts. For an automatic-jurisdiction bucket, enter
   `https://<account_id>.r2.cloudflarestorage.com` as the endpoint. For an EU
   jurisdiction bucket, enter
   `https://<account_id>.eu.r2.cloudflarestorage.com`; the default endpoint
   returns `AccessDenied` for an EU bucket even when the credentials are
   correct. The bucket name has its own prompt, so do not append it to the
   endpoint. The command writes the credentials and generated repository
   password to `/etc/wo/backup.env` as root-owned mode `0600`:

   ```sh
   AWS_ACCESS_KEY_ID=…
   AWS_SECRET_ACCESS_KEY=…
   RESTIC_REPOSITORY=s3:https://<account_id>.r2.cloudflarestorage.com/<bucket>
   RESTIC_PASSWORD=<generated>
   RESTIC_CACHE_DIR=/var/cache/restic
   ```

   It then runs `restic init` against that repository, writes the schedules to
   `/etc/cron.d/wo-backup`, and performs the first end-to-end database and
   files run. Verify the installation before considering setup complete:

   ```bash
   stat -c '%U:%G %a %n' /etc/wo/backup.env /etc/cron.d/wo-backup
   systemctl is-active cron
   wo multitenancy backup status
   wo multitenancy backup list --db
   wo multitenancy backup list --files
   wo multitenancy backup check
   ```

   `backup init` and manual backup commands write their output to the terminal.
   `/var/log/wo/backup.log` is created by shell redirection when the first
   scheduled cron job starts; its absence before that first run is expected.
5. `backup init` prints the repository password **once**. The exact warning is:

   ```text
   WARNING: This repository password is printed only once. It MUST be stored off-box — losing it loses every backup.
   ```

   Copy the password to an off-box password manager or other protected
   storage immediately. R2 credentials without `RESTIC_PASSWORD` cannot
   decrypt or restore this repository.

The generated cron file uses the server's local timezone: hourly database at
`:07`, daily files at `03:10` followed by the family retention pass, weekly
`restic forget --prune` on Sunday at `04:00`, and the monthly metadata-only
check on day 1 at `05:00`. All restic calls use `--retry-lock 5m`; a transient
R2 failure or stale restic lock must not silently strand the next hourly slot.

### Backup configuration

Non-secret backup settings are optional keys in
`/etc/wo/plugins.d/multitenancy.conf`. The complete section, including the
defaults, is:

```ini
[backup]
enable_backup = true
db_schedule_minute = 7          ; hourly at :07
files_schedule = 03:10          ; daily
prune_schedule = Sun 04:00      ; weekly
keep_db = 24h,7d,4w,3m
keep_files = 7d,4w,6m
deleted_tenant_grace = 30d      ; forget a deleted tenant's snapshots this long after deletion
check_schedule = 1 05:00        ; monthly metadata-only restic check (day-of-month HH:MM)
db_ping_url =                   ; optional dead-man URL for the hourly DB run (healthchecks.io style)
files_ping_url =                ; optional dead-man URL for the daily files run
prune_ping_url =                ; optional dead-man URL for the weekly prune
check_ping_url =                ; optional dead-man URL for the monthly check
```

The `[backup]` values are non-secret; R2 keys and `RESTIC_PASSWORD` stay in
`/etc/wo/backup.env`. Keep all `[backup]` scalar keys above the plugin/theme
source sections (`[wordpress_plugins]`, `[wordpress_themes]`, `[github_*]`,
and `[url_*]`). This is the same scalar-section ordering quirk as the rest of
this file: source sections belong at the end so later scalar keys are not
parsed into the wrong section.

Configure one healthchecks.io-style dead-man check **per job**, not one URL for
the whole fleet. The jobs have different periods and each URL receives
`/start` at entry, the base URL on success, and `/fail` on failure. Set each
check's grace to at least **2 times that job's period**: one legitimate
lock-skip must not page, while two consecutive misses should. In particular,
the hourly DB, daily files, weekly prune, and monthly check each need their own
grace window.

### Backup CLI

Every command is under `wo multitenancy backup`:

```text
wo multitenancy backup init
wo multitenancy backup run [--db|--files|--all]
wo multitenancy backup list [<domain>] [--db|--files]
wo multitenancy backup restore <domain> --db|--files|--all [--at=T] [--snapshot=ID] [--operation=ID] [--force]
wo multitenancy backup restore --all-sites [--at=T] [--force]
wo multitenancy backup status
wo multitenancy backup prune
wo multitenancy backup check
wo multitenancy backup forget-site <domain> [--force]
```

`run` defaults to `--all`; use `--db` or `--files` to run exactly one family.
`--snapshot=ID` is valid only with `--db` or `--files`, because one restic
snapshot belongs to one family. `--all` resolves a database snapshot and a
files snapshot independently, then prints both timestamps and their skew
before confirmation. Use `--at=T` to resolve the newest snapshot at or before
the supplied time. `--operation=ID` resolves the complete, shared
`operation:<id>` safety set and is valid for a single-site `--all` restore.
The command rejects a missing, duplicate, wrong-site, or incomplete operation
set before writing anything.

`list` filters by family and optionally by domain. `status` reports the last
success and duration for each family and tenant, per-tenant `data_added`
(restic's post-dedup upload bytes), repository statistics, snapshot counts,
the last check age, stale tenants, orphan-tag anomalies, and pending
tombstones. `prune` is the manual `forget --prune` operation. `forget-site`
is the explicit operator action for an orphan `site:<domain>` tag when there
is no live tenant and no tombstone; it is not an automatic substitute for the
tombstone lifecycle, and fleet-tagged safety snapshots are not carved out of
their fleet operation.

`check` runs the scheduled monthly metadata-only restic check. Once per
quarter, perform the deeper read check manually from a protected shell:

```bash
set -a
. /etc/wo/backup.env
set +a
/usr/local/bin/restic --retry-lock 5m check --read-data-subset=5%
```

### Restore semantics

#### Single site

Use an explicit scope; a bare restore is rejected:

```bash
wo multitenancy backup restore <domain> --db
wo multitenancy backup restore <domain> --files
wo multitenancy backup restore <domain> --all
```

`--at=T` resolves the newest matching snapshot at or before `T`.
`--snapshot=ID` names one snapshot and therefore works only with `--db` or
`--files`. `--all` resolves the hourly database and daily files snapshots
separately, prints their timestamps and skew, and asks for confirmation unless
`--force` is supplied. To restore a safety capture, use
`--operation=<id>`; the operation must contain exactly one valid snapshot per
requested family for this site.

The restore enables the existing per-site maintenance gate before taking its
first safety snapshot. The safety capture covers exactly the write-set:
current DB for `--db`, all site files for `--files`, or both for `--all`.
Each safety snapshot is tagged `pre-restore`, `site:<domain>`, and
`operation:<id>`, and the current DB dump is also retained locally as
`/var/lib/wo-backup/restore/<domain>-<stamp>/pre-restore.sql`. That operation
ID is the rollback handle.

Restore order is files first, then DB. Files are staged and applied with
`rsync -a --delete`; optional `force-ssl-<domain>.conf` is deleted when it is
absent from the selected snapshot. The DB restore materializes the dump,
checks it, drops and recreates the target database, and imports it. nginx must
pass `nginx -t` before reload. Ownership and modes are verified per path
(nginx configuration root-owned, `wp-config.php` `0640`, and site trees
owned by `www-data`); a blanket recursive `chown` is not safe.

The current `dbase.db` row is authoritative for a single-site restore. The
staged `wp-config.php` has its `DB_NAME`, `DB_USER`, `DB_PASSWORD`, and
`DB_HOST` rewritten to the current row before it is installed, and the dump is
imported into that current database/user. The site row and MySQL users/grants
are not changed by a single-site restore.

If the files phase fails, leave the gate up and use the printed rollback
command:

```bash
wo multitenancy backup restore <domain> --files --operation=<id>
```

If DB import fails, the command immediately rolls back from the local
`pre-restore.sql` without depending on R2, retains the staging files, and
leaves the gate up. Only a second failure during that local rollback changes
the result to printed manual recovery commands. Never blanket-ungate a failed
site; investigate it and disable its gate only after validation.

#### Fleet restore on a surviving box

`restore --all-sites` is manifest-driven. It stages and verifies `dbase.db`
from the selected snapshot, derives the tenant list and DB mapping from those
restored rows (not from a potentially mangled current tracking database), and
prints the snapshot/current manifest diff before confirmation. It creates one
operation safety set before writing: per-site DB and files captures, a global
paths capture, and a repository-resident `operation-manifest.json`.

Current-only tenants are **quarantined before the cutover**. Quarantine means
they are removed from nginx's served set as a batch, nginx is tested and
reloaded immediately, and then each tenant's site tree, vhost,
`force-ssl` configuration, cron entry, and DB dump are moved into a root-only
0700 directory:

```text
/var/lib/wo-backup/quarantine/<domain>-<stamp>/
```

The current-only database and user are dropped and a normal tombstone is
written. Nothing remains reachable, but the data exists both in that
quarantine directory and in the remote `pre-restore` safety snapshots. A
reload failure aborts before the quarantine/cutover proceeds. There is no
preserve flag: leaving an unknown tenant served would violate replacement
semantics.

After the snapshot global metadata is activated, the fleet restore performs an
ensure-DB pass from the snapshot rows and then applies the manifest tenant
restore. It continues past individual failures and exits nonzero with a
per-site summary if any site fails. To reinstate a legitimate current-only
tenant during the deleted-tenant grace window, create a fresh registered site
and restore its safety set:

```bash
wo multitenancy create <domain>
wo multitenancy backup restore <domain> --all --operation=<id> --force
```

The fleet operation ID is printed in the quarantine summary and stored in its
manifest. Reinstate within `deleted_tenant_grace`; otherwise use the
quarantine data for deliberate manual recovery.

#### Deleted-site restore

`backup restore` never fabricates a missing site record. Recreate the site
first, which registers new credentials, and then restore it:

```bash
wo multitenancy create deleted.example.com
wo multitenancy backup restore deleted.example.com --all --force
```

The files restore rewrites historical `DB_*` values to the newly registered
current row, so the recreated site and its imported dump agree.

### Deleted tenants and tombstones

`wo multitenancy delete <domain>` removes the tenant row and, after the
successful removal, writes a durable root-only tombstone at
`/var/lib/wo-backup/tombstones/<domain>.json` containing the domain and UTC
deletion time. Delete does not need R2 to succeed and never forgets remote
snapshots inline.

The daily retention job processes tombstones only. After
`deleted_tenant_grace` (default `30d`), it finds that domain's snapshots,
excludes snapshots tagged `fleet`, forgets the remaining IDs, and removes the
tombstone only after the remote forget succeeds. A failed forget is retried
the next day. No `--keep-*` policy and no absence from `dbase.db` authorizes
deletion: an untracked `site:` tag is an anomaly for `status`/`health` and
requires explicit `backup forget-site`.

If a site is recreated or an older `dbase.db` is restored while its tombstone
exists, the live tracking row wins; the stale tombstone is dropped with a
warning and the snapshots are retained. The supported recovery is always
**recreate then restore within the grace window**.

### Dead box — DR runbook

Use this order on a replacement server. Do not run a restore against a blank
box until the fork, the backup credentials, the stack, and the shared
multi-tenancy scaffolding are ready. Pick the files snapshot timestamp/ID and
the matching database restore point before starting.

1. **Install the fork.** Run the fork installer, not the upstream/PyPI path:

   ```bash
   wget -qO wo https://raw.githubusercontent.com/alnaggar-dev/WordOps/main/install
   sudo bash wo
   ```

2. **Restore the backup environment and repository password from off-box
   storage.** Do not run `backup init` on the replacement box; it would be a
   new repository setup. For example, after attaching protected off-box
   recovery media:

   ```bash
   install -o root -g root -m 0600 /mnt/off-box/wo-backup.env /etc/wo/backup.env
   test "$(stat -c '%a' /etc/wo/backup.env)" = 600
   grep -q '^RESTIC_PASSWORD=.' /etc/wo/backup.env
   ```

   The file must contain the original R2 endpoint, access key, secret, and
   `RESTIC_PASSWORD`; losing that password loses the encrypted repository.
3. **Install the stack and every PHP version in use.** Install nginx, MariaDB,
   WP-CLI, and the PHP-FPM versions recorded for the tenants:

   ```bash
   wo stack install
   # Repeat the selectors for every version in the restored tenant rows.
   wo stack install --php83 --php84
   ```

   Do not omit an older PHP-FPM version merely because the replacement's
   default is newer; restored vhosts point at their recorded PHP sockets.
4. **Initialize multi-tenancy FIRST, before any restore.** This plain init
   repairs/writes shared directories, config, and git scaffolding even without
   `--force`; running it after a restore could clobber restored global state:

   ```bash
   wo multitenancy init
   ```

5. **Restore the global paths from the selected files snapshot.** Restore
   shared baseline/config/content, the multitenancy config, `/etc/letsencrypt`,
   and the stable SQLite staging pathname. Restore to `/` so the recorded
   absolute paths are materialized; do not restore `dbase.db` directly onto
   the live path:

   ```bash
   SNAPSHOT_ID='<files-snapshot-id>'
   /usr/local/bin/restic --retry-lock 5m restore "$SNAPSHOT_ID" --target / \
     --include /var/www/shared/config \
     --include /var/www/shared/.git \
     --include /var/www/shared/wp-content \
     --include /etc/wo/plugins.d/multitenancy.conf \
     --include /var/lib/wo-backup/staging/dbase.db \
     --include /etc/letsencrypt
   ```

   The database copy materializes at
   `/var/lib/wo-backup/staging/dbase.db`. Verify it before activating it, then
   atomically move it onto the live metadata path:

   ```bash
   python3 - <<'PY'
   import os
   import sqlite3

   staged = '/var/lib/wo-backup/staging/dbase.db'
   live = '/var/lib/wo/dbase.db'
   with sqlite3.connect(staged) as conn:
       result = conn.execute('PRAGMA integrity_check').fetchone()[0]
   if result != 'ok':
       raise SystemExit('dbase.db integrity check failed: ' + str(result))
   os.replace(staged, live)
   PY
   chown root:root /var/lib/wo/dbase.db
   chmod 0600 /var/lib/wo/dbase.db
   ```

6. **Run the ensure-DB pass from the restored rows.** A fresh box has no
   tenant databases, users, or grants, and logical dumps do not carry them.
   The fleet restore command performs this pass before importing sites: it
   recreates **every MySQL database, user, and grant** described by the
   restored `dbase.db` rows, using the historical fleet credentials as the
   authority for this fleet/DR scope:

   ```bash
   wo multitenancy backup restore --all-sites --at='<restore-time>' --force
   ```

7. **Run the per-site restore loop / fleet restore.** The `--all-sites`
   command in step 6 is the manifest-driven fleet loop: it gates affected
   sites, restores global metadata and each tenant's files and DB with
   replacement semantics, quarantines current-only tenants before cutover,
   and reports every per-site result. Do not substitute a loop that enumerates
   the pre-restore current database; the snapshot manifest is authoritative.

   `apply` does not repair the per-site core links on existing sites. Before
   applying the baseline, recreate the links and generated front controller
   explicitly (the daily file set intentionally excludes re-downloadable
   shared core releases):

   ```bash
   set -eu
   for h in /var/www/*/htdocs; do
       [ -d "$h" ] || continue
       mkdir -p "$h/wp-content"
       for name in wp wp-content/plugins wp-content/themes \
           wp-content/mu-plugins wp-content/languages \
           wp-login.php wp-admin wp-includes wp-cron.php \
           xmlrpc.php wp-comments-post.php wp-settings.php; do
           if [ -e "$h/$name" ] && [ ! -L "$h/$name" ]; then
               echo "Refusing to replace real path: $h/$name" >&2
               exit 1
           fi
           rm -f "$h/$name"
       done
       ln -s /var/www/shared/current "$h/wp"
       for name in wp-login.php wp-admin wp-includes wp-cron.php \
           xmlrpc.php wp-comments-post.php wp-settings.php; do
           ln -s "wp/$name" "$h/$name"
       done
       for name in plugins themes mu-plugins languages; do
           ln -s "/var/www/shared/wp-content/$name" "$h/wp-content/$name"
       done
       if [ ! -e "$h/index.php" ]; then
           cat > "$h/index.php" <<'PHP'
   <?php
   define( 'WP_USE_THEMES', true );
   require __DIR__ . '/wp/wp-blog-header.php';
   PHP
       fi
   done
   ```

8. **Apply the restored baseline.** After the links above exist, apply the
   restored baseline to every enabled tenant and validate nginx:

   ```bash
   wo multitenancy apply
   nginx -t
   systemctl reload nginx
   ```

   Keep failed tenants gated until their per-site restore is repaired and
   validated.
9. **Reinstate the acme.sh renewal cron.** `/etc/letsencrypt` contains the
   account and certificate state, but a replacement box also needs the renewal
   scheduler:

   ```bash
   /etc/letsencrypt/acme.sh \
     --config-home /etc/letsencrypt/config --install-cronjob
   ```

   Confirm the installed root cron entry and run a non-destructive renewal
   check before serving production traffic.
10. **Cut DNS over last.** Confirm `nginx -t`, PHP-FPM sockets, HTTPS
    certificates, and representative site responses on the replacement IP,
    then update the authoritative A/AAAA records (and any proxy/origin
    settings) to the replacement server. Keep the old DNS target available
    until those checks pass.

### DR code-verification note

`BaselineApplicator.apply_baseline_to_site` starts at
`wo/cli/plugins/multitenancy_functions.py:3889` and its body through line 4062
only applies plugins, options, cache configuration, and the theme through
WP-CLI. `apply_baseline_to_sites` calls it at lines 4346-4354. It does not call
`create_shared_symlinks`; that helper is the create-time path at lines 448-515
and only creates missing links/index files. Therefore `wo multitenancy apply`
does **not** regenerate `wp`, `wp-login.php`, or the generated `index.php` for
an existing restored tenant, which is why the explicit relink loop is required
in step 7.

### Backup threat model

This design accepts that a root compromise can wipe the repository: the R2
credentials live on the server and restic needs delete rights for its own
locks and retention. R2 prefix-scoped append-only credentials do not make the
normal backup flow safe from a root compromise. The escape hatch is a second
restic repository copied with `restic copy`, initiated/pulled **from another
machine** that holds independent credentials:

```text
restic -r <second-repository> copy --from-repo <primary-repository>
```

Keep the second repository and its credentials outside the first server's
root trust boundary.
