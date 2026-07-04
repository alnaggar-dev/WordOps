# WordOps Multi-tenancy Plugin — Reference

A WordOps plugin that runs many WordPress sites from **one shared WordPress
core**. Sites symlink a single `/var/www/shared/current` release for ~90% disk
savings and atomic, symlink-swap rollback. Each site keeps its **own**
`wp-config.php` (unique DB credentials and Redis prefix) and its own
`wp-content/uploads`, so tenants are isolated where it matters while sharing
core, plugins, and themes.

> Operating model: **trust-model-of-one** — a solo operator, own code, own
> fleet. Per-tenant isolation and multi-operator workflows are
> explicit non-goals. This plugin is intentionally small.

---

## 1. Model

Two things are shared, two things are per-site:

| Shared (one copy) | Per-site (one each) |
|-------------------|---------------------|
| WordPress core (`/var/www/shared/current`) | `wp-config.php` (DB creds, salts, Redis prefix) |
| `wp-content/{plugins,themes,mu-plugins,languages}` | `wp-content/uploads`, `wp-content/cache` |
| `config/wp-config-shared.php` (fleet-wide settings) | nginx vhost + cache config |
| `config/baseline.json` (baseline plugin/theme set) | SQLite tracking row |

Every tenant `wp-config.php` does `require_once` of
`/var/www/shared/config/wp-config-shared.php` **before** its `DB_*` constants,
so one file applies fleet-wide settings (security, performance, cache, debug)
to all sites at once. A PHP syntax error in that file takes **every** site
down, so the plugin lints it with `php -l` before any operation
(see *Preflight* below) and on every edit.

The shared core is a symlink: `current -> releases/wp-<timestamp>`. `update`
builds a new release and repoints the symlink; `rollback` repoints it to the
previous release. Both are instant and affect all sites at once.

---

## 2. Install & activate

This is the **`alnaggar-dev` fork** — Git-only, no PyPI. The plugin ships
inside the `wo` CLI.

```bash
# Update the installed CLI from the fork (root)
wo update --force
```

Enable the plugin in its config (installed to `/etc/wo/plugins.d/`):

```ini
# /etc/wo/plugins.d/multitenancy.conf
[multitenancy]
enable_plugin = true
shared_root   = /var/www/shared
php_version   = 8.3
baseline_plugins = nginx-helper,woocommerce,...
baseline_theme   = woodmart-child
```

GitHub-sourced plugins/themes are declared in `[github_plugins]` /
`[github_themes]`; direct-URL sources in `[url_plugins]` / `[url_themes]`.

Then initialize the shared infrastructure once:

```bash
wo multitenancy init
```

`init` creates the shared tree, seeds baseline plugins/themes, writes
`wp-config-shared.php`, and sets up git tracking for `baseline.json`. Re-running
`init --force` is safe and also removes the legacy baseline-enforcer MU-plugin
if an older install left one behind.

---

## 3. Commands

`wo multitenancy <command> [options]`

### Lifecycle

| Command | What it does |
|---------|--------------|
| `init` | Create shared infrastructure (run once). |
| `create <domain> --php83 --wpfc` | Create a tenant site on the shared core. Cache flags: `--wpfc`, `--wpredis`, `--wprocket`, `--wpce`, `--wpsc`. SSL: `--le` (Let's Encrypt), `--hsts`, `--dns`. |
| `update` | Build a new shared WordPress release and switch all sites to it. |
| `rollback` | Switch all sites back to the previous release (instant). |
| `delete <domain>` | Remove a tenant site and its tracking row. |
| `remove` | Tear down the entire shared infrastructure (dangerous). |

### Inspection

| Command | What it does |
|---------|--------------|
| `status` | Infrastructure summary: release, disk usage, site counts, health checks. |
| `list` | Table of all shared sites (domain, PHP, cache, SSL, status). |
| `validate` | Flag sites whose `baseline_version` is behind `baseline.json`. |
| `health [--json]` | Per-check snapshot across infra, DB, disk, PHP-FPM, nginx, and each site. `--json` prints a machine-readable envelope; otherwise a text report. |

### Baseline management

The **baseline** is the curated set of plugins + theme every site should run,
stored in `config/baseline.json`. Changes update `baseline.json`; run
`apply` to propagate to live sites.

| Command | What it does |
|---------|--------------|
| `baseline` | Show the current baseline (version, plugins, theme). |
| `baseline add-plugin <slug>` | Add a plugin. Sources: WordPress.org (default), `--github=user/repo [--branch \| --tag]`, or `--url=<zip>`. Add `--apply-now` to roll out immediately. |
| `baseline add-theme <slug>` | Add a theme (same sources). `--set-default` makes it the active theme; `--apply-now` to roll out. |
| `baseline remove-plugin <slug>` | Remove a plugin from the baseline. |
| `baseline remove-theme <slug>` | Remove a theme from the baseline. |
| `baseline update-plugin <slug>` | Re-fetch a plugin from its original source. |
| `baseline update-theme <slug>` | Re-fetch a theme from its original source. |
| `baseline apply [--dry-run] [--verbose]` | Apply the baseline to every enabled site. `--dry-run` previews; `--verbose` prints per-site timings. |

`apply` reports `attempted / succeeded / failed` and logs each outcome to
`/var/log/wo/wordops.log`. A site that fails to converge is reported as
`failed` (with the error) and left as-is — fix it and re-run `apply`.

### Shared config & maintenance

| Command | What it does |
|---------|--------------|
| `shared-config --action edit` | Open `wp-config-shared.php` in `$EDITOR`. On save it is linted with `php -l`; on success PHP-FPM + nginx reload (so the change beats OPcache) and a timestamped `.bak` is kept (10 newest). On a syntax error the backup is restored automatically — no site is left broken. `edit` is the only supported action. |
| `maintenance --enable [--site=<domain> \| --all] [--message="..."]` | Drop an nginx 503 maintenance page in front of one site or the whole fleet. |
| `maintenance --disable [--site=<domain> \| --all]` | Remove the maintenance page. |

### Preflight

`init`, `create`, `update`, and `baseline apply` first run a `php -l`
preflight on `wp-config-shared.php`. If it has a syntax error they refuse to
run (a broken shared config would break every site) and point you at
`shared-config --action edit`. The check passes when the file is valid, absent
(first `init`), or `php` is not installed.

---

## 4. Directory structure

Shared infrastructure (`shared_root`, default `/var/www/shared`):

```
/var/www/shared/
├── current -> releases/wp-YYYYMMDD-HHMMSS   # active core (symlink)
├── releases/                                # kept for rollback (keep_releases)
│   └── wp-YYYYMMDD-HHMMSS/                   # a full WordPress core
├── wp-content/
│   ├── plugins/                             # shared, baseline-managed
│   ├── themes/                              # shared, baseline-managed
│   ├── mu-plugins/                          # shared must-use plugins
│   └── languages/
├── config/
│   ├── wp-config-shared.php                 # fleet-wide settings (require_once'd)
│   └── baseline.json                        # baseline plugin/theme set + version
└── .git/                                    # tracks baseline.json
```

Per-site (`/var/www/<domain>/`):

```
/var/www/example.com/
├── wp-config.php                            # per-site: DB creds, salts, Redis prefix
├── htdocs/
│   ├── wp -> /var/www/shared/current        # core (symlink)
│   ├── wp-login.php, wp-admin, wp-includes  # symlinks into shared core
│   └── wp-content/
│       ├── plugins  -> shared plugins        # symlink
│       ├── themes   -> shared themes         # symlink
│       ├── mu-plugins -> shared mu-plugins   # symlink
│       ├── languages -> shared languages     # symlink
│       ├── uploads/                          # site-specific (real dir)
│       └── cache/                            # site-specific (real dir)
└── conf/nginx/                              # vhost + cache config (+ maintenance include)
```

---

## 5. Tenancy caveat

This is a **trust-model of one operator running a curated plugin set**, not a
hostile multi-tenant SaaS. Because `wp-content/plugins` and `themes` are shared
symlinks, a plugin that writes into its **own** plugin directory (license files,
generated assets, caches under `wp-content/plugins/<slug>/…`) will collide
across sites or fail on the read-only shared tree. Keep all mutable per-site
state under `wp-content/uploads` (which is real and per-site). Vet plugins for
this behavior before adding them to the baseline; most well-behaved plugins
store state in the database or `uploads` and work fine.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `wp-config-shared.php has a PHP syntax error` on any command | The preflight caught a broken shared config. Fix it: `wo multitenancy shared-config --action edit` (it lints on save and reverts on error). |
| Every site is down after editing shared config | A syntax error slipped in via a manual edit (not `shared-config --action edit`). Restore the newest backup: `cp /var/www/shared/config/wp-config-shared.php.bak.* …` then `php -l` it; reload PHP-FPM + nginx. |
| `validate` lists a freshly-created site as outdated | The create-time baseline version wasn't recorded (older core). Run `wo multitenancy baseline apply`. |
| New baseline plugin not active on sites | Baseline changes only edit `baseline.json`. Roll out with `wo multitenancy baseline apply` (or `add-plugin … --apply-now`). |
| 404s / blank pages on a site | A core symlink is missing. Check `ls -la /var/www/<domain>/htdocs/wp` and `/var/www/shared/current`. |
| A plugin breaks only on shared sites | It writes into its own plugin dir (shared/read-only). Remove it from the baseline or replace it; keep state in `uploads`. |
| SSL flag ignored on `create` | Use `--le` (double hyphen), not an em dash `—le`. |
| Redis cache cross-talk between sites | Each site must have a unique `redis_prefix` (enforced by a unique index). Recreate the site if its `wp-config.php` is missing the prefix. |
| `maintenance --enable` returns 503 everywhere as expected, but admins are locked out too | By design — the maintenance page applies to all IPs. Disable it (`maintenance --disable`) to regain access. |
| PHP-FPM didn't pick up a shared-config change | `shared-config --action edit` reloads FPM automatically; after a manual edit, reload it yourself: `systemctl reload php8.3-fpm nginx`. |

---

## 7. Logs

Operations log to `/var/log/wo/wordops.log` (the standard WordOps log). Run any
command with `wo --debug multitenancy <command>` for verbose tracing, and tail
`tail -f /var/log/wo/wordops.log`.
