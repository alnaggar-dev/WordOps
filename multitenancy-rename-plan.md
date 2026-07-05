## Context

Add a real `wo multitenancy rename <old-domain> <new-domain>` command for shared-core WordPress tenants. The command changes the tenant's primary domain in-place: it preserves the existing WordPress database, uploads, shared-core symlinks, cache type, PHP version, and baseline tracking while replacing domain-keyed WordOps, nginx, WordPress URL, wp-config, SSL, and cache metadata. `wo site update --alias` is explicitly not used because `site_update.py` defines `--alias` as a redirect target and sets `stype='alias'`, not as a tenant/domain rename.

The implementation choice is in-place rename, not create/migrate/delete. `wo multitenancy create` creates a fresh database and WordPress install; rename must preserve a production tenant's existing data.

## Approach

### 1. Expose `wo multitenancy rename <old-domain> <new-domain>`

Edit `wo/cli/plugins/multitenancy.py`.

- In `WOMultitenancyController.Meta.arguments`, add a second optional positional immediately after the existing `site_name` positional:

  ```python
  (['newsite_name'],
      dict(help='New website domain name for rename', nargs='?')),
  ```

  Use `newsite_name` because `wo/cli/plugins/site_clone.py` already uses `site_name` + `newsite_name` for two-domain commands. Insert it before `plugin_slug` and `theme_slug`; one-argument commands still parse their first value as `site_name`, which is how existing baseline commands already work.

- Add the exposed command next to `delete`:

  ```python
  @expose(help="Rename a multitenancy site's primary domain")
  def rename(self):
      """Rename a shared-core tenant domain in place."""
      return self._rename_impl()
  ```

- Add `_rename_impl(self)` below `rename()`. It orchestrates validation, rollback, and helper calls only. Reusable DB, nginx, wp-config, SSL, and WP-CLI operations go in the helper modules below.

- Every preflight failure in `_rename_impl` must call `Log.error(...)` and then immediately `return False`. Existing tests patch `Log.error`, while production `Log.error` exits by default; explicit returns keep tests and non-exiting helper paths correct.

- Validate inputs in this exact order:
  1. Require both `pargs.site_name` and `pargs.newsite_name`; otherwise log `Usage: wo multitenancy rename <old-domain> <new-domain>` and return false.
  2. Normalize both domains with `WODomain.validate(self, value)`.
  3. Reject `old_domain == new_domain` with `Source and target domains are identical`.
  4. Require `MTDatabase.is_initialized(self)`; message `Multi-tenancy not initialized`.
  5. Require `getSiteInfo(self, old_domain)`; message `Site {old_domain} not found in WordOps database`.
  6. Require a `MultitenancySite` row for `old_domain` using the same direct `db_session` + `MultitenancySite` query pattern as `_delete_impl`; message `Site {old_domain} not found in multitenancy tracking`.
  7. Reject an existing WordOps site at `new_domain` via `check_domain_exists(self, new_domain)`; message `Site {new_domain} already exists`.
  8. Reject existing multitenancy tracking at `new_domain` via `MTDatabase.is_shared_site(self, new_domain)`; message `Site {new_domain} already exists in multitenancy tracking`.
  9. Reject target path conflicts before mutation. Use `os.path.lexists(path) or os.path.exists(path)` for every conflict check so dangling symlinks are caught. Check `/var/www/{new_domain}`, `/etc/nginx/sites-available/{new_domain}`, `/etc/nginx/sites-enabled/{new_domain}`, and `/etc/nginx/conf.d/force-ssl-{new_domain}.conf`.
  10. Require the old site root and wp-config: `old_root = mt_site.site_path or site_info.site_path or f'/var/www/{old_domain}'`; require `old_root` and `{old_root}/htdocs/wp-config.php` to exist.

- Before mutation, compute and store:
  - `old_root`, `old_htdocs = f'{old_root}/htdocs'`, `new_root = f'/var/www/{new_domain}'`, `new_htdocs = f'{new_root}/htdocs'`.
  - `cache_type = mt_site.cache_type or site_info.cache_type or 'basic'`.
  - `php_version = mt_site.php_version or site_info.php_version or '8.4'`.
  - `old_ssl = bool(getattr(mt_site, 'is_ssl', False) or getattr(site_info, 'is_ssl', False))`.
  - `ssl_requested = old_ssl or bool(getattr(pargs, 'letsencrypt', False))`.
  - `tracked_old_redis_prefix = getattr(mt_site, 'redis_prefix', None)`.
  - `new_redis_prefix = MTDatabase.generate_redis_prefix(self, new_domain)`.
  - `scheme = 'https' if ssl_requested else 'http'`.
  - `timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')`.
  - `backup_dir = f'/var/www/.wo-rename-{old_domain}-to-{new_domain}-{timestamp}'`; compute this path before mutation, but create the directory only in the backup step after confirmation/SSL preflight.

- If `pargs.force` is false, prompt exactly like delete does: warn `This will rename site: {old_domain} -> {new_domain}`, read `Continue? [y/N]:`, and return false unless the answer is exactly `y` after `.strip().lower()`.

- Do not call `wo site update --alias`, `setupdatabase`, `MTFunctions.install_wordpress`, `addNewSite`, or `MTDatabase.add_shared_site` from the rename path. Those paths create or redirect sites; they do not rename an existing tenant.

### 2. Add recoverable WordOps site-table rename

Edit `wo/cli/plugins/sitedb.py`.

- Add this helper after `updateSiteInfo` and before `deleteSiteInfo`:

  ```python
  def renameSiteInfo(self, old_site, new_site, site_path=None, ssl=None):
      """Rename a site record in the WordOps application database."""
  ```

- Exact behavior:
  1. Query `old = SiteDB.query.filter(SiteDB.sitename == old_site).first()`.
  2. Query `existing = SiteDB.query.filter(SiteDB.sitename == new_site).first()`.
  3. If `old` is missing, call `Log.error(self, f"{old_site} does not exist in database", exit=False)` and return `False`.
  4. If `existing` is present, call `Log.error(self, f"{new_site} already exists in database", exit=False)` and return `False`.
  5. Set `old.sitename = new_site`.
  6. If `site_path` is not `None`, set `old.site_path = site_path`.
  7. If `ssl` is not `None`, set `old.is_ssl = ssl`.
  8. Commit and return `True`.
  9. On any exception, call `db_session.rollback()`, `Log.debug(self, "{0}".format(e))`, `Log.error(self, "Unable to rename site in application database.", exit=False)`, and return `False`.

- Update the `wo/cli/plugins/multitenancy.py` import from `wo.cli.plugins.sitedb` to include `renameSiteInfo`.

- This helper must not change DB credentials, cache type, storage fields, PHP version, or `created_on`. Existing `updateSiteInfo` changes `created_on`; do not reuse that behavior for rename.

### 3. Add recoverable multitenancy tracking rename

Edit `wo/cli/plugins/multitenancy_db.py`.

- Add this static method on `MTDatabase` immediately after `update_site_baseline`:

  ```python
  @staticmethod
  def rename_shared_site_domain(app, old_domain, new_domain, site_path=None, redis_prefix=None, is_ssl=None):
      """Rename a shared-site tracking row without changing its identity."""
  ```

- Exact behavior:
  1. `session = db_session`.
  2. Query `site = session.query(MultitenancySite).filter_by(domain=old_domain).first()`.
  3. Query `existing = session.query(MultitenancySite).filter_by(domain=new_domain).first()`.
  4. If `site` is missing, call `Log.error(app, f"Site not found in database: {old_domain}", exit=False)` and return `False`.
  5. If `existing` is present and is not the same row, call `Log.error(app, f"Domain already tracked: {new_domain}", exit=False)` and return `False`.
  6. Set `site.domain = new_domain`.
  7. Set `site.site_path = site_path or f'/var/www/{new_domain}'`.
  8. If `redis_prefix` is not `None`, set `site.redis_prefix = redis_prefix`.
  9. If `is_ssl` is not `None`, set `site.is_ssl = is_ssl`.
  10. Set `site.updated_at = datetime.now()`.
  11. Commit, `Log.debug(app, f"Renamed shared site: {old_domain} -> {new_domain}")`, and return `True`.
  12. On exception, call `session.rollback()`, `Log.error(app, f"Failed to rename shared site {old_domain}: {e}", exit=False)`, and return `False`.

- Do not implement this as remove+add. The existing row owns baseline version, enabled status, shared release, cache type, PHP version, and Redis prefix history; rename preserves those fields unless explicitly listed above.

### 4. Add recoverable MTFunctions helpers

Edit `wo/cli/plugins/multitenancy_functions.py`.

Add `import re` near the top if it is not already global. Keep existing create/delete helpers unchanged for existing callers. Add new rename-specific helpers instead of changing existing helpers that call `Log.error` with the default exiting behavior.

#### `rewrite_wp_config_for_rename`

Add near `generate_wp_config`:

```python
@staticmethod
def rewrite_wp_config_for_rename(app, site_root, old_domain, new_domain, new_redis_prefix, tracked_old_redis_prefix=None):
    """Rewrite only domain-derived wp-config.php values for a tenant rename.

    Returns the old Redis prefix found in wp-config.php, or None on failure.
    """
```

Exact behavior:

1. `wp_config_path = f"{site_root}/htdocs/wp-config.php"`.
2. If missing, `Log.error(app, f"wp-config.php not found: {wp_config_path}", exit=False)` and return `None`.
3. Read as text.
4. Extract the active Redis prefix with `re.search(r"'prefix'\s*=>\s*'([^']+)'", content)`. If no match, log `wp-config.php does not contain a Redis prefix; refusing unsafe rename` with `exit=False` and return `None`.
5. Set `resolved_old_prefix = match.group(1)`. If `tracked_old_redis_prefix` is truthy and differs from `resolved_old_prefix`, `Log.warn(app, f"Tracked Redis prefix {tracked_old_redis_prefix} differs from wp-config.php prefix {resolved_old_prefix}; using wp-config.php value")`.
6. Replace exactly the first Redis prefix assignment with `new_redis_prefix` using `re.sub(r"('prefix'\s*=>\s*)'[^']+'", "\\1'{}'".format(new_redis_prefix), content, count=1)`.
7. Replace `://{old_domain}/wp-content` with `://{new_domain}/wp-content`.
8. Replace `WordPress Configuration for {old_domain}` with `WordPress Configuration for {new_domain}` if present.
9. Write the file, `os.chmod(wp_config_path, 0o640)`, log debug, and return `resolved_old_prefix`.
10. On exception, log `Failed to update wp-config.php for rename: {e}` with `exit=False` and return `None`.

Do not call `generate_wp_config` during rename; it would regenerate salts and discard the existing tenant's DB credentials from the current file.

#### `update_wordpress_domain`

Add near `set_permalink_structure`:

```python
@staticmethod
def update_wordpress_domain(app, site_htdocs, old_domain, new_domain, scheme):
    """Update WordPress URLs and serialized domain references after tenant rename."""
```

Exact behavior:

1. `new_url = f'{scheme}://{new_domain}'`.
2. Run exactly these commands with `subprocess.run(..., capture_output=True, text=True, check=True)`:

   ```python
   ['wp', 'option', 'update', 'home', new_url, '--path=' + site_htdocs, '--allow-root']
   ['wp', 'option', 'update', 'siteurl', new_url, '--path=' + site_htdocs, '--allow-root']
   [
       'wp', 'search-replace', old_domain, new_domain,
       '--all-tables-with-prefix',
       '--skip-columns=guid',
       '--precise',
       '--recurse-objects',
       '--path=' + site_htdocs,
       '--allow-root',
   ]
   ```

3. Return `True` on success.
4. On `subprocess.CalledProcessError`, log `Failed to update WordPress domain: {e.stderr}` with `exit=False` and return `False`.

The project consistently invokes WP-CLI with `--allow-root`. The current WP-CLI handbook documents the planned `search-replace` flags: `--skip-columns=guid`, `--all-tables-with-prefix`, `--precise`, and `--recurse-objects`. Do not run `wp cache flush`; tenants share a Redis database, so use tenant-scoped `purge_site_cache` only.

#### Recoverable nginx helpers

Add these helpers near the existing nginx helpers:

```python
@staticmethod
def validate_nginx_config_recoverable(app, log_errors=True):
    """Run nginx -t without exiting the process."""
```

- Run `subprocess.run(['nginx', '-t'], capture_output=True, text=True, timeout=30)`.
- Return `True` for return code 0.
- On nonzero/timeout/exception, use only `Log.warn` or `Log.error(..., exit=False)` and return `False`.

```python
@staticmethod
def write_nginx_config_for_rename(app, domain, php_version, cache_type, site_root):
    """Write a multitenancy nginx vhost for a renamed tenant without exiting."""
```

- Build content with existing `MTFunctions.generate_modular_nginx_config(domain, site_root, php_version, cache_type)`.
- Ensure nginx directories with `MTFunctions.ensure_nginx_directories(app, domain, site_root)`.
- Write `/etc/nginx/sites-available/{domain}` and `os.chmod(..., 0o644)`.
- Validate with `validate_nginx_config_recoverable`.
- Return the nginx config path on success, `None` on failure.
- On exception, log with `Log.error(..., exit=False)` and return `None`.

```python
@staticmethod
def enable_nginx_site_for_rename(app, domain):
    """Enable a renamed nginx site without using default-exiting WOFileUtils."""
```

- `src = f'/etc/nginx/sites-available/{domain}'`, `dst = f'/etc/nginx/sites-enabled/{domain}'`.
- If `os.path.lexists(dst) or os.path.exists(dst)`, log with `exit=False` and return `False`.
- Call `os.symlink(src, dst)`.
- Return `True`; on exception, log with `exit=False` and return `False`.

```python
@staticmethod
def reload_nginx_recoverable(app, domain):
    """Reload nginx without exiting the process."""
```

- Validate with `nginx -t` first.
- Try `systemctl reload nginx`; if it fails, try `nginx -s reload`.
- Return `True` when either reload succeeds.
- On failure/timeout/exception, use `Log.warn` or `Log.error(..., exit=False)` and return `False`.

#### Recoverable SSL helpers for rename

Do not call `MTFunctions.setup_ssl` from rename. It and lower-level helpers can call default-exiting `Log.error`, and rollback must remain reachable after mutation.

Add these helpers near `setup_ssl`:

```python
@staticmethod
def prepare_ssl_certificate_for_rename(app, domain, pargs):
    """Ensure a certificate for the new domain exists before mutating the tenant."""
```

Exact behavior:

1. Import `WOAcme`, `WODomain`, and `WOVar` inside the function.
2. If `/etc/letsencrypt/live/{domain}/fullchain.pem` and `/etc/letsencrypt/live/{domain}/key.pem` both exist, return `True`. If `/etc/letsencrypt/renewal/{domain}_ecc/fullchain.cer` exists, also return `True`; `install_ssl_config_for_rename` can deploy it to the live path.
3. If `/etc/letsencrypt/acme.sh` is missing, log `acme.sh is not installed; cannot prepare SSL for rename` with `exit=False` and return `False`. Do not auto-install acme during rename.
4. Compute acme domains exactly like `setup_ssl`: subdomain gets `[domain]`; apex gets `[domain, f'www.{domain}']`.
5. Build `acmedata` from existing parser flags: default `dns=False`, `acme_dns='dns_cf'`, `dnsalias=False`, `acme_alias=''`, `keylength='ec-384'`; if `pargs.dns` is set, set DNS mode and use that provider unless it is plain `dns_cf`.
6. If not DNS mode and `pargs.force` is false, call `WOAcme.check_dns(app, acme_domains)`. It already returns false without exiting for DNS mismatch; if false, return `False`.
7. For webroot mode, ensure `/var/www/html/.well-known/acme-challenge` exists with `os.makedirs(..., exist_ok=True)` and try `shutil.chown('/var/www/html/.well-known', 'www-data', 'www-data')` plus `os.chmod('/var/www/html/.well-known', 0o750)`; log warnings but continue if chown/chmod fail.
8. Build the acme issue argv as a list, not a shell string:

   ```python
   cmd = ['/etc/letsencrypt/acme.sh', '--config-home', '/etc/letsencrypt/config', '--issue']
   for item in acme_domains:
       cmd.extend(['-d', item])
   if acmedata['dns']:
       cmd.extend(['--dns', acmedata['acme_dns']])
       if acmedata['dnsalias']:
           cmd.extend(['--challenge-alias', acmedata['acme_alias']])
   else:
       cmd.extend(['-w', '/var/www/html'])
   cmd.extend(['-k', acmedata['keylength'], '-f'])
   ```

9. Run with `subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)`.
10. Return `True` on success; on `CalledProcessError`, timeout, or exception, log stderr/details with `exit=False` and return `False`.

```python
@staticmethod
def install_ssl_config_for_rename(app, domain, site_root, pargs):
    """Deploy prepared certificate files and write nginx SSL includes for a renamed tenant."""
```

Exact behavior:

1. Import `WOVar` inside the function.
2. Create `/etc/letsencrypt/live/{domain}` with `os.makedirs(..., exist_ok=True)`.
3. Run acme install-cert as argv list:

   ```python
   [
       '/etc/letsencrypt/acme.sh', '--config-home', '/etc/letsencrypt/config',
       '--install-cert', '-d', domain, '--ecc',
       '--cert-file', f'{WOVar.wo_ssl_live}/{domain}/cert.pem',
       '--key-file', f'{WOVar.wo_ssl_live}/{domain}/key.pem',
       '--fullchain-file', f'{WOVar.wo_ssl_live}/{domain}/fullchain.pem',
       '--ca-file', f'{WOVar.wo_ssl_live}/{domain}/ca.pem',
       '--reloadcmd', 'nginx -t && service nginx restart',
   ]
   ```

4. Run with `subprocess.run(..., capture_output=True, text=True, timeout=300, check=True)`.
5. Render `{site_root}/conf/nginx/ssl.conf` with `ssl.mustache`, data `{'ssl_live_path': WOVar.wo_ssl_live, 'domain': domain, 'quic': True}`, using `app.app.render((data), 'ssl.mustache', out=file_handle)`. This must overwrite any file at that path because stale old-domain SSL includes were already moved aside.
6. Compute acme domains the same way as `prepare_ssl_certificate_for_rename` and render `/etc/nginx/conf.d/force-ssl-{domain}.conf` with `force-ssl.mustache`, data `{'domains': ' '.join(acme_domains)}`.
7. If `pargs.hsts` is true, write `{site_root}/conf/nginx/hsts.conf` with the same header string used by `SSL.setuphsts`.
8. Validate with `validate_nginx_config_recoverable(app, log_errors=True)` and return its result.
9. On any exception, log `Failed to install SSL config for renamed domain {domain}: {e}` with `exit=False` and return `False`.

### 5. Implement in-place rename orchestration

Edit `_rename_impl` in `wo/cli/plugins/multitenancy.py` using this exact order.

1. Load config and preflight shared config before mutation:

   ```python
   config = MTFunctions.load_config(self)
   shared_root = config.get('shared_root', '/var/www/shared')
   if not MTFunctions.preflight_shared_config(self, shared_root):
       return False
   ```

2. If `ssl_requested` is true, call `MTFunctions.prepare_ssl_certificate_for_rename(self, new_domain, pargs)` before any filesystem/nginx/DB mutation. If it returns false, log `SSL certificate preparation failed for {new_domain}` and return false. Certificate issuance can touch external acme state, but it happens before tenant mutation and therefore does not require tenant rollback.

3. Initialize rollback state:

   ```python
   rollback = {
       'old_enabled': os.path.lexists(f'/etc/nginx/sites-enabled/{old_domain}') or os.path.exists(f'/etc/nginx/sites-enabled/{old_domain}'),
       'old_available_backup': None,
       'old_force_ssl_backup': None,
       'wp_config_backup': None,
       'backup_dir': backup_dir,
       'root_moved': False,
       'new_nginx_created': False,
       'new_enabled': False,
       'wp_replaced': False,
       'sitedb_updated': False,
       'mt_updated': False,
       'stale_include_backups': [],
       'new_ssl_paths': [],
   }
   ```

4. Back up old mutable files into `backup_dir` outside the moved site root:
   - Create `backup_dir` with `os.makedirs(backup_dir, mode=0o700, exist_ok=False)`.
   - Copy `{old_htdocs}/wp-config.php` to `{backup_dir}/wp-config.php`, `os.chmod(..., 0o600)`, and store that path in `rollback['wp_config_backup']`.
   - If `/etc/nginx/sites-available/{old_domain}` exists, copy it to `{backup_dir}/nginx-site-available-{old_domain}` and store in `rollback['old_available_backup']`.
   - If `/etc/nginx/conf.d/force-ssl-{old_domain}.conf` exists, copy it to `{backup_dir}/force-ssl-{old_domain}.conf` and store in `rollback['old_force_ssl_backup']`.

5. Enter a `try` block for all following mutation steps. Do not call default-exiting helpers from this point onward.

6. Disable old nginx files without deleting backups:
   - Remove `/etc/nginx/sites-enabled/{old_domain}` if `os.path.lexists` or `os.path.exists` says it is present.
   - Remove `/etc/nginx/conf.d/force-ssl-{old_domain}.conf` if present.

7. Move the site root with `os.rename(old_root, new_root)` and set `rollback['root_moved'] = True`. Use rename, not copy+delete, to preserve permissions, uploads, `.admin_pass`, symlinks, and cache directories.

8. Move stale domain-specific SSL/HSTS includes out of the moved site root:
   - For each of `{new_root}/conf/nginx/ssl.conf` and `{new_root}/conf/nginx/hsts.conf`, if present, rename it to `{path}.rename.{timestamp}.bak` and append `(backup_path, original_path)` to `rollback['stale_include_backups']`.
   - This prevents old-domain certificate includes from being picked up by the new vhost. It is required because the existing deploy path writes SSL config with `overwrite=False`.

9. Rewrite wp-config and capture the actual old Redis prefix from the active file:
   - Call `resolved_old_redis_prefix = MTFunctions.rewrite_wp_config_for_rename(self, new_root, old_domain, new_domain, new_redis_prefix, tracked_old_redis_prefix)`.
   - If it returns falsey, raise `Exception("wp-config rewrite failed")`.
   - Use `resolved_old_redis_prefix` for old cache purge and rollback, even if it differs from DB tracking.

10. Write and enable the new nginx vhost:
    - Call `MTFunctions.write_nginx_config_for_rename(self, new_domain, php_version, cache_type, new_root)`; if falsey, raise `Exception("Nginx config generation failed")`.
    - Set `rollback['new_nginx_created'] = True`.
    - Call `MTFunctions.enable_nginx_site_for_rename(self, new_domain)`; if false, raise `Exception("Nginx site enable failed")`.
    - Set `rollback['new_enabled'] = True`.
    - Require `MTFunctions.validate_nginx_config_recoverable(self, log_errors=True)`; if false, raise `Exception("Nginx configuration invalid after rename")`.

11. Update WordPress domain references:
    - Set `rollback['wp_replaced'] = True` immediately before calling `MTFunctions.update_wordpress_domain`. This ensures rollback runs if `home` or `siteurl` succeeds and the later `search-replace` command fails.
    - Call `MTFunctions.update_wordpress_domain(self, new_htdocs, old_domain, new_domain, scheme)`; if false, raise `Exception("WordPress domain update failed")`.

12. Install SSL config for the new primary domain only when needed:
    - Initialize `ssl_enabled = False` before this step.
    - If `ssl_requested` is true, call `MTFunctions.install_ssl_config_for_rename(self, new_domain, new_root, pargs)`; if false, raise `Exception("SSL setup failed for renamed domain")`.
    - After a true return, set `ssl_enabled = True` and record any existing new-domain SSL files in `rollback['new_ssl_paths']`: `/etc/nginx/conf.d/force-ssl-{new_domain}.conf`, `{new_root}/conf/nginx/ssl.conf`, and `{new_root}/conf/nginx/hsts.conf` if they exist.
    - If `ssl_requested` is false, keep `ssl_enabled = False`.

13. Update databases only after filesystem, nginx validation, WordPress URL update, and SSL have succeeded:
    - Call `renameSiteInfo(self, old_domain, new_domain, site_path=new_root, ssl=ssl_enabled)`; if false, raise `Exception("WordOps site database rename failed")`; set `rollback['sitedb_updated'] = True`.
    - Call `MTDatabase.rename_shared_site_domain(self, old_domain, new_domain, site_path=new_root, redis_prefix=new_redis_prefix, is_ssl=ssl_enabled)`; if false, raise `Exception("Multitenancy tracking rename failed")`; set `rollback['mt_updated'] = True`.

14. Purge tenant-scoped caches:
    - Call `MTFunctions.purge_site_cache(self, old_domain, resolved_old_redis_prefix)`.
    - Call `MTFunctions.purge_site_cache(self, new_domain, new_redis_prefix)`.
    - Do not call `wp cache flush`, `redis-cli flushdb`, or `redis-cli flushall`.

15. Reload nginx recoverably:
    - Require `MTFunctions.reload_nginx_recoverable(self, new_domain)`; if false, raise `Exception("Nginx reload failed after rename")`.

16. Track nginx config in git as best-effort only after the rename is otherwise successful:
    - Wrap `WOGit.add(self, ["/etc/nginx"], msg=f"Renamed shared WordPress site: {old_domain} -> {new_domain}")` in `try/except`.
    - On failure, `Log.warn(self, f"Renamed site but failed to update nginx git tracking: {e}")` and do not roll back the completed rename.

17. Remove `backup_dir` with `shutil.rmtree(backup_dir, ignore_errors=True)` after success.

18. Log success:

    ```python
    Log.info(self, f"✅ Renamed site: {old_domain} -> {new_domain}")
    Log.info(self, f"site_renamed source={old_domain} target={new_domain} result=success")
    ```

19. Return `True`.

### 6. Roll back failed rename attempts

In the `except` block around mutation steps 6-15, perform best-effort rollback in this order. Each rollback action must have its own `try/except` and log a warning on failure, then continue to the next rollback action.

1. Log `Rename failed: {e}` with `Log.error(self, ..., exit=False)` and `site_rename_failed source={old_domain} target={new_domain} result=failure` with `Log.info`.
2. If `rollback['mt_updated']` is true, call `MTDatabase.rename_shared_site_domain(self, new_domain, old_domain, site_path=old_root, redis_prefix=resolved_old_redis_prefix or tracked_old_redis_prefix, is_ssl=old_ssl)`.
3. If `rollback['sitedb_updated']` is true, call `renameSiteInfo(self, new_domain, old_domain, site_path=old_root, ssl=old_ssl)`.
4. If `rollback['wp_replaced']` is true, call `MTFunctions.update_wordpress_domain(self, new_htdocs, new_domain, old_domain, 'https' if old_ssl else 'http')`. Ignore its return value but warn if it fails.
5. Delete new-domain SSL artifacts before restoring stale old includes: remove every recorded path in `rollback['new_ssl_paths']` if it exists, then also remove `/etc/nginx/conf.d/force-ssl-{new_domain}.conf`, `{new_root}/conf/nginx/ssl.conf`, and `{new_root}/conf/nginx/hsts.conf` if they exist. The stale old include files were moved aside in step 8, so any file at those original paths during rollback is a new/partial SSL artifact and must be removed before restoring backups.
6. If `rollback['wp_config_backup']` exists, restore it to the active wp-config path. If `root_moved` is true, restore to `{new_htdocs}/wp-config.php`; otherwise restore to `{old_htdocs}/wp-config.php`. Use the backup path in `backup_dir`, not a path under the moved site root.
7. If `rollback['new_enabled']` is true, remove `/etc/nginx/sites-enabled/{new_domain}` when `os.path.lexists` or `os.path.exists` says it is present.
8. If `rollback['new_nginx_created']` is true, remove `/etc/nginx/sites-available/{new_domain}` if present.
9. For each `(backup_path, original_path)` in `rollback['stale_include_backups']`, if `backup_path` exists, move it back to `original_path`. Do this before moving the root back.
10. If `rollback['root_moved']` is true and `new_root` exists and `old_root` does not exist, `os.rename(new_root, old_root)`.
11. If `rollback['old_available_backup']` exists, copy it back to `/etc/nginx/sites-available/{old_domain}`.
12. If `rollback['old_enabled']` is true and `/etc/nginx/sites-enabled/{old_domain}` does not exist or is not a symlink, recreate the symlink with direct `os.symlink('/etc/nginx/sites-available/{old_domain}', '/etc/nginx/sites-enabled/{old_domain}')`. Do not use `WOFileUtils.create_symlink` in rollback.
13. If `rollback['old_force_ssl_backup']` exists, copy it back to `/etc/nginx/conf.d/force-ssl-{old_domain}.conf`.
14. Call `MTFunctions.reload_nginx_recoverable(self, old_domain)` once at the end. If it returns false, log `Rollback completed but nginx reload failed; run nginx -t` as a warning.
15. Leave `backup_dir` in place on failure for operator inspection. It contains wp-config credentials, so create it mode `0o700` and chmod copied wp-config backups `0o600`.
16. Return `False`.

Rollback is best-effort after WP-CLI and SSL side effects, but the implementation must attempt every action above. Do not exit at the first rollback failure.

### 7. Add tests in the existing multitenancy test module

Edit `tests/cli/40_test_multitenancy.py`. Do not add a new test file; multitenancy behavior tests already live here.

- Add `class MultitenancyRenameTests(unittest.TestCase):` near `MultitenancyDeleteCacheTests`.

- Add `_run_rename(...)` modeled on `_run_create_impl_with_mocks` and `_run_delete`:
  - Construct `ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)`.
  - Use `pargs = mock.Mock()` with `site_name='old.example.com'`, `newsite_name='new.example.com'`, `force=True`, `letsencrypt=False`, `dns=None`, `hsts=False` unless a test overrides them.
  - Set `ctrl.app = mock.Mock()` and `ctrl.app.pargs = pargs`.
  - Patch `Log.info`, `Log.warn`, `Log.error`, and `Log.debug`.
  - Patch `mt.WODomain.validate` with `side_effect=['old.example.com', 'new.example.com']`.
  - Patch `mt.getSiteInfo` to return a site-info object with `site_path='/var/www/old.example.com'`, `cache_type='wpfc'`, `php_version='8.4'`, and `is_ssl` matching the test.
  - Patch the direct `db_session` query chain used for the old `MultitenancySite` row. Use a separate mocked `MultitenancySite` object with `domain='old.example.com'`, `site_path='/var/www/old.example.com'`, `cache_type='wpfc'`, `php_version='8.4'`, `is_ssl`, and `redis_prefix='old_example_com_'`.
  - Patch `mt.MTDatabase.is_initialized`, `mt.check_domain_exists`, `mt.MTDatabase.is_shared_site`, and `mt.MTDatabase.generate_redis_prefix`.
  - Use path-keyed filesystem fakes, not blanket `os.path.exists=True/False`. Happy path returns true for old root and old wp-config, false for all new target conflicts, and test-specific values for old nginx/SSL files.
  - Patch mutation boundaries: `os.rename`, `os.remove`, `os.symlink`, `shutil.copy2`, `shutil.rmtree`, `MTFunctions.prepare_ssl_certificate_for_rename`, `MTFunctions.rewrite_wp_config_for_rename`, `MTFunctions.write_nginx_config_for_rename`, `MTFunctions.enable_nginx_site_for_rename`, `MTFunctions.validate_nginx_config_recoverable`, `MTFunctions.update_wordpress_domain`, `MTFunctions.install_ssl_config_for_rename`, `MTFunctions.purge_site_cache`, `MTFunctions.reload_nginx_recoverable`, `renameSiteInfo`, `MTDatabase.rename_shared_site_domain`, and `WOGit.add`.
  - Patch `mt.subprocess.run` with a guard side effect that fails if any argv contains `--alias`, starts with `['wo', 'site', 'update']`, equals/starts with `['wp', 'cache', 'flush']`, equals/starts with `['redis-cli', 'flushdb']`, or equals/starts with `['redis-cli', 'flushall']`.

- Required orchestration tests:
  1. `test_rename_requires_two_domains`: missing `newsite_name` logs usage and no backup/move/rewrite/WP/DB helpers are called.
  2. `test_rename_rejects_existing_target`: when `check_domain_exists` returns true for the new domain, no mutation helpers are called.
  3. `test_rename_rejects_untracked_old_site`: when the direct `MultitenancySite` query returns `None`, no mutation helpers are called.
  4. `test_rename_rejects_filesystem_or_nginx_target_conflict_before_mutation`: make exactly one new target conflict path exist; assert no backup, root move, wp-config rewrite, WP update, or DB rename happens.
  5. `test_rename_moves_root_rewrites_wp_updates_databases_and_purges_caches`: happy path asserts partial order with a `mock.Mock()` manager: root move < wp-config rewrite < nginx write/enable/validate < WP domain update < DB helpers < cache purge < nginx reload. Assert exact calls for `os.rename('/var/www/old.example.com', '/var/www/new.example.com')`, `rewrite_wp_config_for_rename(ctrl, '/var/www/new.example.com', 'old.example.com', 'new.example.com', 'new_example_com_', 'old_example_com_')`, `update_wordpress_domain(ctrl, '/var/www/new.example.com/htdocs', 'old.example.com', 'new.example.com', 'http')`, `renameSiteInfo(ctrl, 'old.example.com', 'new.example.com', site_path='/var/www/new.example.com', ssl=False)`, `MTDatabase.rename_shared_site_domain(ctrl, 'old.example.com', 'new.example.com', site_path='/var/www/new.example.com', redis_prefix='new_example_com_', is_ssl=False)`, old/new `purge_site_cache`, and `reload_nginx_recoverable(ctrl, 'new.example.com')`.
  6. `test_rename_ssl_site_prepares_and_installs_ssl_and_records_ssl`: with `old_ssl=True`, assert `prepare_ssl_certificate_for_rename` is called before mutation, `install_ssl_config_for_rename(ctrl, 'new.example.com', '/var/www/new.example.com', pargs)` is called after WP update and before DB helpers, and both DB helpers receive `ssl=True` / `is_ssl=True`.
  7. `test_rename_ssl_setup_failure_rolls_back_before_db_updates`: with `old_ssl=True`, make `install_ssl_config_for_rename` return false. Assert `update_wordpress_domain` was called for old→new with scheme `https`, both DB helpers were not called, root was moved back, old nginx symlink was restored when originally enabled, and `site_rename_failed source=old.example.com target=new.example.com result=failure` was logged.
  8. `test_rename_rolls_back_db_and_wp_when_reload_fails`: allow both DB helpers to succeed, then make `reload_nginx_recoverable` return false. Assert rollback calls include `MTDatabase.rename_shared_site_domain(ctrl, 'new.example.com', 'old.example.com', site_path='/var/www/old.example.com', redis_prefix='old_example_com_', is_ssl=False)`, `renameSiteInfo(ctrl, 'new.example.com', 'old.example.com', site_path='/var/www/old.example.com', ssl=False)`, `MTFunctions.update_wordpress_domain(ctrl, '/var/www/new.example.com/htdocs', 'new.example.com', 'old.example.com', 'http')`, and `os.rename('/var/www/new.example.com', '/var/www/old.example.com')`.
  9. `test_rename_stale_ssl_includes_are_moved_and_restored_on_failure`: make moved-root `ssl.conf` and `hsts.conf` exist. Assert they are renamed to `.rename.<timestamp>.bak` paths before nginx validation. On a later forced failure, assert they move back before root rollback. Patch `datetime.now()` or capture the generated timestamp to avoid brittle time assertions.
  10. `test_rename_does_not_use_alias_or_shared_cache_flush`: rely on the `mt.subprocess.run` guard described above plus positive assertions that root move, WP update, and DB rename happen. Do not patch `WOSiteUpdateController`; runtime call boundaries are the contract.

- Required helper tests:
  1. `test_rewrite_wp_config_for_rename_preserves_db_credentials_and_salts`: write a temp wp-config with DB constants, salts, `WP_CONTENT_URL`, and Redis prefix. Assert DB constants and salt lines are byte-for-byte unchanged, old Redis prefix is returned, exactly the first Redis prefix assignment changes to `new_example_com_`, `WP_CONTENT_URL` changes to the new domain, and `os.chmod(..., 0o640)` is called.
  2. `test_rewrite_wp_config_for_rename_missing_redis_prefix_returns_none_without_rewrite`: assert no file write when the safety gate fails.
  3. `test_update_wordpress_domain_uses_allow_root_and_safe_search_replace_flags`: patch `wo.cli.plugins.multitenancy_functions.subprocess.run`; assert exactly three argv lists in order: home update, siteurl update, search-replace. Each call must use `capture_output=True`, `text=True`, `check=True`; each argv must include `--allow-root`; search-replace must include `--all-tables-with-prefix`, `--skip-columns=guid`, `--precise`, and `--recurse-objects`; no argv is `wp cache flush`.
  4. `test_rename_shared_site_domain_recoverable_errors_and_preserved_fields`: missing-old and target-conflict paths call `Log.error(..., exit=False)` and return false. Success preserves row identity/id, baseline version, enabled state, shared release, site type, cache type, PHP version, and created_at; only `domain`, `site_path`, `redis_prefix`, `is_ssl`, and `updated_at` change.
  5. `test_rename_site_info_updates_sitename_and_path_without_changing_db_credentials`: assert `sitename`, `site_path`, and `is_ssl` change while `created_on`, `site_type`, `cache_type`, `db_name`, `db_user`, `db_password`, `db_host`, and `php_version` stay unchanged.
  6. `test_recoverable_nginx_helpers_do_not_call_exiting_log_error`: patch `Log.error`; force failures in `validate_nginx_config_recoverable`, `write_nginx_config_for_rename`, `enable_nginx_site_for_rename`, and `reload_nginx_recoverable`; assert any `Log.error` call uses `exit=False`.

## Critical files & anchors

- `wo/cli/plugins/multitenancy.py` — `WOMultitenancyController.Meta.arguments` lines 57-112, `_create_impl` lines 217-480, and `_delete_impl` lines 874-967 define parser reuse, create state, direct MT row lookup, confirmation, cache purge, and structured logs.
- `wo/cli/plugins/multitenancy_functions.py` — existing `generate_wp_config` lines 415-580, `generate_modular_nginx_config` lines 654-719, `set_permalink_structure` lines 768-798, `purge_site_cache` lines 801-848, and `setup_ssl` lines 910-1005 define values to reuse or avoid; existing nginx helpers lines 174-330 are not rollback-safe because they call default-exiting `Log.error`.
- `wo/cli/plugins/multitenancy_db.py` — `MultitenancySite` lines 35-52, `add_shared_site` lines 253-286, `update_site_baseline` lines 353-371, and Redis prefix helpers lines 443-613 define tracking fields, transaction style, and prefix behavior.
- `wo/cli/plugins/sitedb.py` plus `wo/cli/plugins/models.py` — `SiteDB.sitename` is unique (`models.py` lines 6-33), and `addNewSite`/`getSiteInfo`/`updateSiteInfo`/`deleteSiteInfo` (`sitedb.py` lines 11-113) show current DB helper style but lack a sitename rename helper.
- `tests/cli/40_test_multitenancy.py` — `MultitenancyTests._run_create_impl_with_mocks` lines 58-131 and `MultitenancyDeleteCacheTests._run_delete` lines 688-721 are the test harness patterns to copy.

## Verification

Run these from the repository root after implementation. No environment variables are required for the unit tests because the planned tests mock filesystem, nginx, WordPress, SSL, and DB side effects.

```bash
python3 -m unittest tests.cli.40_test_multitenancy.MultitenancyRenameTests -v
python3 -m unittest tests.cli.40_test_multitenancy -v
```

Manual smoke on a disposable WordOps host, run as root after the unit tests pass:

```bash
wo multitenancy init
wo multitenancy create old-rename.example.com --php84 --wpfc
wp option update blogname 'Rename Smoke' --path=/var/www/old-rename.example.com/htdocs --allow-root
wo multitenancy rename old-rename.example.com new-rename.example.com --force
wo multitenancy list
wp option get home --path=/var/www/new-rename.example.com/htdocs --allow-root
wp option get siteurl --path=/var/www/new-rename.example.com/htdocs --allow-root
test -d /var/www/new-rename.example.com
test ! -e /var/www/old-rename.example.com
test -e /etc/nginx/sites-enabled/new-rename.example.com
test ! -e /etc/nginx/sites-enabled/old-rename.example.com
nginx -t
```

Expected non-SSL smoke results:

- `wo multitenancy list` shows `new-rename.example.com` and not `old-rename.example.com`.
- `wp option get home` and `wp option get siteurl` both return `http://new-rename.example.com`.
- `/var/www/new-rename.example.com/htdocs/wp-config.php` still contains the original DB credentials and salts, but `WP_CONTENT_URL` references `new-rename.example.com` and the Redis prefix is the new generated prefix.
- `/etc/nginx/sites-enabled/old-rename.example.com` is absent; no old-domain alias/redirect is created by rename.
- `nginx -t` succeeds.

SSL smoke, only when DNS for the new domain already points at the host or DNS validation is configured:

```bash
wo multitenancy create old-ssl-rename.example.com --php84 --wpfc -le
wo multitenancy rename old-ssl-rename.example.com new-ssl-rename.example.com --force -le
wp option get home --path=/var/www/new-ssl-rename.example.com/htdocs --allow-root
nginx -t
```

Expected SSL result: `home` returns `https://new-ssl-rename.example.com`, nginx validates, WordOps and multitenancy DB rows record SSL enabled for the new domain, and `/var/www/new-ssl-rename.example.com/conf/nginx/ssl.conf` plus `/etc/nginx/conf.d/force-ssl-new-ssl-rename.example.com.conf` reference the new domain. If the disposable host exposes port 443 publicly, additionally verify the served certificate with `openssl s_client -servername new-ssl-rename.example.com -connect new-ssl-rename.example.com:443` and expect the new domain certificate.

Rollback check in tests: force `MTFunctions.reload_nginx_recoverable` to return false after both DB helpers succeed. Expected behavior is old root restored, old nginx symlink restored when it existed, SiteDB and multitenancy rows renamed back, WP URLs reversed, and `site_rename_failed source=old.example.com target=new.example.com result=failure` logged.
