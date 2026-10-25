# WordOps Multi-tenancy Plugin

Stores share WordPress core and site code. Each store keeps its database, credentials, salts, Redis database, uploads, cache, and nginx configuration.
The operator owns the code and the fleet. Tenant security isolation and multi-operator workflows are not goals.

## Content model

WordPress core and site content have separate releases. `update` changes core only. `deploy` changes content only.
Plugin, theme, MU-plugin, translation, and baseline changes come only from the site repository's `tools/deploy`.
WordOps does not download or manage individual plugins and themes.

```text
/var/www/shared/
  current -> releases/wp-YYYYMMDD-HHMMSS
  releases/wp-YYYYMMDD-HHMMSS/           WordPress core
  content/
    YYYYMMDD-HHMMSS-abcdef0/
      wp-content/{plugins,themes,mu-plugins,languages}/
      baseline.json
      RELEASE
    current -> YYYYMMDD-HHMMSS-abcdef0
    history.jsonl
  wp-content -> content/current/wp-content
  config/
    baseline.json -> ../content/current/baseline.json
    wp-config-shared.php
  backups/db/
```

`content/current` is a relative link. A content deploy replaces only this link.
The `wp-content` and `config/baseline.json` links stay fixed.
Tenant content links still point through `/var/www/shared/wp-content`.
Each tenant's `htdocs/wp` points through `/var/www/shared/current`.
Uploads, cache, and upgrade folders remain real per-site directories.

`RELEASE` is JSON with `commit`, `short`, `branch`, `date`, `built`, and plugin/theme version maps.
Release names contain a timestamp and seven lowercase hexadecimal commit characters.
Cutover uses a `YYYYMMDD-HHMMSS-pre-cutover` name instead.
`content/history.jsonl` records each operation's time, release, previous release, path, restore point, and result.
WordOps keeps the five newest content releases, plus the live and previous releases if needed.
Core retention is separate.

## Configuration and baseline

Enable the plugin in `/etc/wo/plugins.d/multitenancy.conf`.
This file contains infrastructure settings, not content sources.

| Key | Default | Meaning |
| --- | --- | --- |
| `enable_plugin` | `true` in the packaged file | Enable the plugin. |
| `shared_root` | `/var/www/shared` | Shared infrastructure root. |
| `keep_releases` | `3` | Core releases retained for rollback. |
| `wp_version` | `latest` fallback | Core version for init/update. The packaged file pins `7.0.2`. |
| `php_version` | `8.4` | Default PHP version. |
| `admin_email` | `admin@example.com` | Default tenant admin email. |
| `apply_workers` | `4` | Parallel apply workers, limited to 1–16. |
| `min_free_space_gb` | `2` | Free-space warning threshold. |

The site repository owns `baseline.json`. Deploy it with content rather than editing the live copy.
It selects active plugins, the theme, and fleet-wide WordPress options:

```json
{
  "plugins": ["nginx-helper", "object-cache-pro"],
  "theme": "woodmart-child",
  "options": {
    "blog_public": false,
    "timezone_string": "UTC"
  }
}
```

Apply is additive. Plugins absent from this list stay active unless you use `apply --prune`.
Scalar options become strings, with booleans converted to `1` or `0`. Arrays and objects use JSON.
WordOps does not maintain baseline Git history or revision counters.

`config/wp-config-shared.php` contains fleet-wide PHP constants.
Tenant configs require it before their database constants, unless `WO_BYPASS_SHARED_CONFIG` is true.
`init`, `create`, `update`, and `apply` check its syntax with `php -l`.

## Initialization and cutover

```bash
wo multitenancy init
```

Init creates infrastructure folders and fixed content links, downloads core, writes shared configuration, and records the core release.
It does not create a baseline or a content release.
`init --force` reinitializes core infrastructure without downloading site content or changing ownership inside content releases.
Existing real content folders and baseline files remain untouched.

For an existing fleet with the old layout, run this once:

```bash
wo multitenancy cutover
```

Cutover requires tracked shared sites. It enables maintenance and drains active nginx, PHP, database, and cron work before moving folders.
It moves existing content and baseline into a `pre-cutover` release, writes `RELEASE`, and creates the content links.
It resets caches, gracefully reloads PHP-FPM then nginx, removes maintenance, and checks the fleet. Failure attempts to restore the old layout.
It refuses a fleet that already uses the fixed content link.

The drain reads `/status?json&full` directly over FastCGI from **both** WordOps
tenant pools for each tracked PHP version: `/var/run/php/php84-fpm.sock` and
`/var/run/php/php84-two-fpm.sock` for PHP 8.4. It requires `/usr/bin/cgi-fcgi`
(`libfcgi-bin`, included with new PHP stack installs and stack upgrades), the
managed pool names `www-php84` / `www-two-php84`, and `pm.status_path = /status`.
On an existing installation without the client, install `libfcgi-bin` before
cutover. Missing sockets, failed requests, or incomplete status responses block
the drain; queued requests and all non-idle workers except the status request
must disappear before cutover.

The admin `/fpm/status/php84` page is not a drain prerequisite: port 22222 uses
HTTPS, its route requires a matching file under `/var/www/22222/htdocs/fpm/status/`,
and it load-balances the two pools rather than reporting both. Stack setup
creates these files only for the first installed PHP version, so a later PHP
version can return 404 even with healthy pools. Changing HTTPS settings or
adding that page's marker file does not establish a safe drain.

For a fresh server, use this order:

1. Run `wo multitenancy init` on the server.
2. On the Mac, run `DEPLOY_HOST=<host> tools/deploy` from the site repository.
3. Run `wo multitenancy create <domain>` on the server with the required site options.

The first deploy creates `content/current`. It does not require tracked sites or a previous release.
The upload omits `--link-dest` because there is no live release to share files with.
Dry-run reports the first release and shows that code-only deployment is not possible.
With no tenants, deploy prepares the release and switches the link without maintenance, database work, or store checks.
History records the first deploy as `safe`, with no previous release. Back is unavailable until a previous release exists.
If tracked sites exist but live content is missing, deploy refuses. Restore `content/current` before another deploy.
This refusal prevents an incomplete existing fleet from being treated as an empty server.
Existing pre-cutover fleets can still create tenants from their real shared content folder.

## Content deploys

Use the site repository's `tools/deploy` to build and transfer committed content.
The server command accepts an already staged release:

```bash
wo multitenancy deploy <release>
wo multitenancy deploy <release> --code-only
```

The default safe path:

1. Enable maintenance on all tracked stores, with the loopback bypass closed.
2. Lock cron and drain active work.
3. Dump every tenant database locally. Run the database backup family if backups are configured.
4. Write the managed `wo-login-log.php` into the staged release and set ownership.
5. Switch `content/current`, clear page caches, reset opcache, and gracefully reload tenant PHP-FPM services followed by nginx.
6. Run WooCommerce database updates when its plugin version changes or a tenant database differs from WooCommerce's expected database version (the greater of the loaded plugin version and its last database-update callback version).
7. Apply the baseline when its bytes or the Object Cache Pro version changed.
8. Open the loopback bypass and check stores while public traffic remains blocked.
9. Close the bypass, remove maintenance, release cron locks, and prune old releases.

A failed safe deploy can also trigger apply on the next safe deploy.
If backups are not initialized, WordOps retains local dumps and warns that it skipped the remote database backup.
Configured but disabled or failed backups stop deployment.

Checks cover the home page, one published product, cart, checkout, My Account, login, and REST API.
They check HTTP results, critical-error text, WooCommerce database versions against WooCommerce's expected database version, new fatal log entries, and fleet health.
Each store must have a published product.
Every page request sends `X-Requested-With: XMLHttpRequest` to bypass the managed FastCGI cache.
A cached home-page 200 alone is not recovery: an error on any of the seven pages fails the checks.
These checks do not prove that payments work. Test checkout separately after payment-related changes.

A safe failure before the switch removes newly added maintenance gates and leaves the live release unchanged.
A safe failure after the switch keeps maintenance active. Fix forward or choose database recovery.
WordOps does not automatically restore databases.

### Code-only path

Use `--code-only` only after reviewing changed code for database effects.
The server requires identical plugin/theme version maps and identical baseline bytes.
Those checks do not prove that code has no database effects.

Code-only deployment takes database dumps while stores stay open.
It skips maintenance, draining, WooCommerce migrations, and baseline apply.
It still prepares the release, switches content, resets caches, and checks stores.
A failure after the switch automatically selects the previous release and checks it again.
Restoring these dumps would lose data written after the restore point.

Multitenancy cache clearing uses `wo clean --all --reload`: it still deletes
FastCGI cache files, flushes Redis, and resets OpCache, but runs `nginx -t`
before gracefully reloading nginx instead of restarting it. FastCGI-only
multitenancy purges use the same `--reload` mode. A failed purge or nginx
configuration test/reload reports failure without attempting a restart.
This keeps listeners open during core updates, core rollbacks, and content
switches; it does not change maintenance gates or the ordinary `wo clean`
restart policy.

Every content switch, including automatic and explicit rollback, gracefully reloads nginx after PHP-FPM and before checking pages.
Reloading FPM alone leaves nginx's per-worker upstream health and cached FastCGI connections untouched.
The standard two-socket upstream has no shared `zone` or explicit failure overrides, so nginx defaults to `max_fails=1` and `fail_timeout=10s`.
A transport failure during FPM replacement can leave a worker reporting `no live upstreams` even after the old files are back.
The nginx reload starts fresh workers while old workers finish existing requests; it does not stop listeners or change maintenance gates.
It uses the existing configuration-checked reload path and fails the operation if reload fails, rather than waiting out the failure timeout or accepting a cached home page.
The safe path still drains before switching, and its maintenance gates stay closed until the normal checked resume.
Code-only rollback retains all seven public page checks and the failed deployment history result; switching the link back alone is not a successful health check.
Rollback keeps its log marker **before** the switch and service reloads, so restored-release startup failures remain visible.

Only recovery checks exclude a late fatal whose PHP header gives a source file inside the exact release being switched away from.
Message mentions and stack frames do not establish the origin; unattributed, core, restored-release, and `content/current` failures still fail recovery.
Normal deployment checks exclude nothing. Recovery still checks all seven pages, WooCommerce database versions, and fleet health.

### Switch content back

```bash
wo multitenancy deploy --back
wo multitenancy deploy --back --force
```

The command uses `content/history.jsonl` to find the previous release.
After a code-only deploy, it switches files and checks the fleet without maintenance.
After a safe deploy, it refuses unless you supply `--force`.
It prints the restore point's per-store `wp db import` commands.

Safe-path back enables maintenance, drains work, switches files, and checks the fleet.
It keeps maintenance active, including when checks fail after the switch.
Restore the chosen databases and check stores before reopening traffic:

```bash
wo multitenancy maintenance --all --disable
```

Switching files does not reverse database migrations, options, roles, plugin activation, or orders.
Back does not automatically run apply or import dumps.

## Tenant and core commands

Every command below starts with `wo multitenancy`.

| Command | Behavior |
| --- | --- |
| `create <domain> [flags]` | Require live content on the new layout, create a tenant, and apply baseline plugins, theme, and options. |
| `apply [--dry-run] [--prune] [--verbose]` | Apply the live baseline to enabled tenants. Return nonzero if any site fails. |
| `baseline` | Print the live baseline JSON. |
| `status` | Show core release, deployed content commit, infrastructure checks, sites, disk use, and baseline. |
| `validate` | Check baseline JSON and listed plugin/theme files. Show deployed commit and pending core migrations. |
| `health [--json] [--site=<domain>]` | Check infrastructure, services, content state, and deployed commit. Exit 0 healthy, 2 degraded, or 1 unhealthy. |
| `list` | List tracked tenants and their PHP, cache, SSL, and enabled state. |
| `update [--force]` | Update core only. Test new core after switching, unless forced. |
| `rollback [--force]` | Select previous core. It does not restore content or databases. |
| `delete <domain> [--force]` | Delete a tenant and its tracking row. |
| `remove [--force]` | Remove shared infrastructure. Refuse while tenants remain unless forced. |

Apply does not download code, change releases, track baseline revisions, or rewrite the login logger.
It repairs tenant Redis database assignment, synchronizes cron, and applies cache configuration.
Use `apply --dry-run --prune` before deactivating plugins absent from the baseline.
Apply does not recreate tenant core links or the generated front controller.

### Create options

```bash
wo multitenancy create example.com --php84 --wpfc
wo multitenancy create ssl.example.com --php84 --wpfc -le
```

PHP flags range from `--php74` through `--php84`.
Cache flags include `--wpfc`, `--wpredis`, `--wprocket`, `--wpce`, and `--wpsc`.
Prefer `--wpfc`. The bundled nginx build has a known failure on the Redis page-cache path.
`--wpredis` therefore requires `--force`.
Other options include `--admin-user`, `--admin-email`, `-le`/`--letsencrypt`, `--hsts`, and `--dns[=dns_cf]`.
Without flags, PHP uses configuration and page caching is disabled.
SSL failure leaves the tenant on HTTP with a warning.
There is no command to convert standalone sites to shared core.

### Core update safety

Update stages core and compares `$wp_db_version`. A lower or unreadable schema marker aborts the update.
Equal schema versions use the fast path. The canary runs after the core switch.
For a higher schema, update enables maintenance, drains work, and takes tenant database dumps before switching.
It then runs supervised per-tenant `wp core update-db` and tests new core through a loopback bypass.
`--force` skips the canary, not the schema safety steps.

`config/pending-db-upgrades.json` records unfinished migrations.
The next update recovers these migrations before starting another update.
Failed migrations keep affected stores gated and produce a nonzero result.
Successful migrations can release their update-owned gates. Pre-existing maintenance remains unchanged.
Post-switch failure does not automatically roll back core.
Core rollback changes files only. Restore database dumps separately when recovery requires them.

## Shared configuration and maintenance

```bash
wo multitenancy shared-config --action edit
wo multitenancy maintenance --enable --all
wo multitenancy maintenance --disable --site=example.com
```

Shared-config editing uses `$EDITOR` or `vi`, retains ten backups, checks PHP syntax, and reloads services.
A syntax error restores the backup.
For temporary recovery, define `WO_BYPASS_SHARED_CONFIG` as true in the affected tenant's config.
Remove that bypass after repairing the shared file.

Generated shared configuration disables loopback WP-Cron.
WordOps maintains `/etc/cron.d/wo-multitenancy` with maintenance-aware, locked, every-minute tenant jobs.
Each job has a deterministic delay of 0–59 seconds.
Create, delete, rename, and non-dry-run apply regenerate these entries.
Maintenance operations open existing `/tmp/wo-cron-<domain>.lock` files read-only, without creation flags, so locks owned by `www-data` work with Linux `protected_regular`.
Missing lock files are created exclusively; a concurrent creator is handled by reopening read-only. Symlinks are never followed, and exclusive locks stay held until the operation releases them.

Deploy owns the generated `wo-login-log.php` MU-plugin.
It logs password failures outside `wp-login.php` to journald for the `wo-wordpress-auth` fail2ban jail.
The nginx access-log jail handles `wp-login.php`. Empty username or password submissions do not enter the MU-plugin log.
Keep mutable files in tenant uploads or databases, not shared plugin or theme folders.

Interactive commands log to `/var/log/wo/wordops.log`.
Scheduled backup output also goes to `/var/log/wo/backup.log`.

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

File backups and fleet safety captures include the real live content release and `content/current`, not only a content symlink.
The release includes its baseline and `RELEASE` metadata.
They omit `/var/www/shared/.git` and do not archive every old content release.
Before cutover, they capture the real shared `wp-content` folder.
Fleet restore restores the selected release and current link, then recreates the two fixed links.

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

The `[backup]` values are non-secret. R2 keys and `RESTIC_PASSWORD` stay in `/etc/wo/backup.env`.

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
4. **Initialize multi-tenancy before restoring an empty replacement server.**
   Init creates core, configuration, and fixed links, but no live content release.
   Do not run it after restoring global state.

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
     --include /var/www/shared/content \
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

`BaselineApplicator.apply_baseline_to_site` in `wo/cli/plugins/multitenancy_functions.py`
applies plugins, options, cache configuration, and the theme through WP-CLI.
It does not call `create_shared_symlinks`.
Therefore apply does not recreate tenant core links or `index.php`.
The explicit relink loop in step 7 remains necessary.

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
