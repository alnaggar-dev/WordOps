"""WordOps Multi-tenancy Plugin
Enables WordPress multi-tenancy with shared core files for efficient management.
"""

import os
import json
import shutil
from datetime import datetime
from cement.core.controller import CementBaseController, expose
from wo.cli.plugins.site_functions import (
    check_domain_exists, setupdatabase, setwebrootpermissions,
    site_package_check
)
from wo.cli.plugins.sitedb import (
    addNewSite, getSiteInfo, renameSiteInfo,
    updateSiteInfo
)
from wo.core.domainvalidate import WODomain
from wo.core.fileutils import WOFileUtils
from wo.core.git import WOGit
from wo.core.logging import Log
from wo.cli.plugins.multitenancy_functions import (
    MTFunctions, SharedInfrastructure, ReleaseManager, BaselineApplicator,
    create_shared_config_file, edit_shared_config,
)
from wo.cli.plugins.multitenancy_db import MTDatabase
from wo.cli.plugins.multitenancy_health import HealthChecker, render_text as render_health_text
from wo.cli.plugins.multitenancy_backup_functions import (
    fleet_operation, write_tombstone, repair_backup_cron
)
from wo.cli.plugins.multitenancy_content import (
    current_release as current_content_release, is_cut_over, read_release_info,
)


def wo_multitenancy_hook(app):
    """Hook to initialize multitenancy database tables"""
    from wo.core.database import init_db
    import wo.cli.plugins.models
    init_db(app)
    # Initialize multitenancy database tables
    # Pass a context that matches Log.* expectations (has `.app`)
    try:
        from types import SimpleNamespace
        ctx = SimpleNamespace(app=app)
    except Exception:
        class _Ctx:
            pass
        ctx = _Ctx()
        ctx.app = app
    MTDatabase.initialize_tables(ctx)


def _pending_upgrades_path(shared_root):
    return f"{shared_root}/config/pending-db-upgrades.json"


def _write_pending_upgrades(shared_root, payload):
    """Atomically persist the pending-db-upgrades ledger."""
    path = _pending_upgrades_path(shared_root)
    tmp = f"{path}.tmp"
    with open(tmp, 'w') as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def _remove_pending_upgrade_domain(app, shared_root, domain):
    """Drop one migrated domain from the ledger; delete it when empty."""
    path = _pending_upgrades_path(shared_root)
    try:
        with open(path) as fh:
            payload = json.load(fh)
        domains = [d for d in payload.get('domains', []) if d != domain]
        if domains:
            payload['domains'] = domains
            _write_pending_upgrades(shared_root, payload)
        else:
            os.remove(path)
    except (OSError, ValueError) as e:
        # Ledger bookkeeping must never kill post-flip migration work.
        Log.warn(app, f"Could not update pending-db-upgrades ledger: {e}")


def _reject_extra_positionals(controller, pargs, fields=('newsite_name',)):
    """Reject stray positional tokens swallowed by optional positionals.

    `create foo.com —le` (em dash) parks '—le' in newsite_name and silently
    creates the site without SSL; surface it as an argument error instead.
    """
    extras = [
        tok for tok in (getattr(pargs, f, None) for f in fields)
        if isinstance(tok, str) and tok
    ]
    if extras:
        hint = ''
        if any(tok.startswith(('\u2014', '\u2013')) for tok in extras):
            hint = (" (a Unicode dash was pasted; use ASCII '--', "
                    "e.g. --le)")
        Log.error(controller,
                  f"unrecognized arguments: {' '.join(extras)}{hint}")


class _UpdateAbort(Exception):
    """Stop an update when a safety check fails."""


class WOMultitenancyController(CementBaseController):
    """WordOps Multi-tenancy Controller"""
    
    class Meta:
        label = 'multitenancy'
        stacked_on = 'base'
        stacked_type = 'nested'
        description = 'Manage WordPress multi-tenancy with shared core files'
        arguments = [
            (['site_name'],
                dict(help='Website domain name', nargs='?')),
            (['newsite_name'],
                dict(help='New website domain name for rename', nargs='?')),
            (['--force'],
                dict(help='Force operation without confirmations', action='store_true')),
            (['--shared'],
                dict(help='Create site using shared WordPress core', action='store_true')),
            (['--php74'], dict(help='Use PHP 7.4', action='store_true')),
            (['--php80'], dict(help='Use PHP 8.0', action='store_true')),
            (['--php81'], dict(help='Use PHP 8.1', action='store_true')),
            (['--php82'], dict(help='Use PHP 8.2', action='store_true')),
            (['--php83'], dict(help='Use PHP 8.3', action='store_true')),
            (['--php84'], dict(help='Use PHP 8.4', action='store_true')),
            (['--wpfc'], dict(help='WordPress with FastCGI cache', action='store_true')),
            (['--wpredis'], dict(help='WordPress with Redis cache', action='store_true')),
            (['--wprocket'], dict(help='WordPress with WP Rocket', action='store_true')),
            (['--wpce'], dict(help='WordPress with Cache Enabler', action='store_true')),
            (['--wpsc'], dict(help='WordPress with WP Super Cache', action='store_true')),
            (['--letsencrypt', '-le'],
                dict(help='Configure Let\'s Encrypt SSL', nargs='?', const='on')),
            (['--hsts'], dict(help='Enable HSTS', action='store_true')),
            (['--dns'], dict(help='DNS API provider for wildcard SSL', nargs='?', const='dns_cf')),
            (['--admin-email'],
                dict(help='WordPress admin email', default='')),
            (['--admin-user'],
                dict(help='WordPress admin username', default='SuperDuper')),
            (['--action'], dict(help="Shared-config action (only 'edit' supported)", dest='config_action')),
            (['--dry-run'], dict(help='Preview changes without applying', action='store_true', dest='dry_run')),
            (['--prune'], dict(help='Deactivate plugins not listed in baseline during apply', action='store_true', dest='prune')),
            # DevOps improvements: cross-cutting flags
            (['--json'], dict(help='Emit machine-readable JSON output', action='store_true', dest='json_output')),
            (['--enable'], dict(help='Enable maintenance mode', action='store_true', dest='enable_flag')),
            (['--disable'], dict(help='Disable maintenance mode', action='store_true', dest='disable_flag')),
            (['--site'], dict(help='Target site domain for health/maintenance', dest='site_filter')),
            (['--all'], dict(help='Apply operation to every shared site', action='store_true', dest='all_flag')),
            (['--message'], dict(help='Maintenance message shown to visitors', dest='message')),
            (['--verbose'], dict(help='Verbose per-site output', action='store_true', dest='verbose')),
        ]
        usage = "wo multitenancy <command> [options]"
    CRON_SYNC_FAILURE_HINT = "cron sync failed; run `wo multitenancy apply` to regenerate /etc/cron.d/wo-multitenancy"

    def _sync_wp_cron_or_fail(self):
        if not MTFunctions.sync_wp_cron_entries(self):
            Log.error(self, self.CRON_SYNC_FAILURE_HINT)


    @expose(hide=True)
    def default(self):
        """Default command"""
        self.app.args.print_help()

    @expose(help="Initialize WordPress multi-tenancy shared infrastructure")
    @fleet_operation('init')
    def init(self):
        """Initialize shared WordPress infrastructure"""
        pargs = self.app.pargs
        
        Log.info(self, "Initializing WordPress multi-tenancy infrastructure...")
        
        # Check if already initialized
        if MTDatabase.is_initialized(self):
            if not pargs.force:
                Log.error(self, "Multi-tenancy already initialized. Use --force to reinitialize.")
            else:
                Log.warn(self, "Reinitializing multi-tenancy infrastructure...")
        
        # Load configuration
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        if not MTFunctions.preflight_shared_config(self, shared_root):
            Log.error(self, "Aborted: wp-config-shared.php failed preflight")
        
        # Create shared infrastructure
        infra = SharedInfrastructure(self, shared_root)
        
        try:
            # Create directory structure
            Log.info(self, "Creating shared directory structure...")
            infra.create_directory_structure()
            
            # Download WordPress core
            Log.info(self, "Downloading WordPress core...")
            release_name = infra.download_wordpress_core(config.get('wp_version'))
            
            # Phase 1: Create shared configuration file
            Log.info(self, "Creating shared configuration file...")
            if create_shared_config_file(self, shared_root):
                Log.info(self, "   ✅ Shared config file created")
            else:
                Log.warn(self, "   ⚠️  Could not create shared config file")
            
            # Make release current
            Log.info(self, "Activating release...")
            infra.switch_release(release_name)
            if not MTDatabase.update_release(self, release_name):
                Log.warn(self, "Could not record release in tracking DB")
            
            # Set permissions
            Log.info(self, "Setting permissions...")
            infra.set_permissions()

            # Auto-cleanup old releases
            Log.info(self, "Cleaning up old releases...")
            release_manager = ReleaseManager(self, shared_root)
            keep_releases = int(config.get('keep_releases', 3))
            release_manager.cleanup_old_releases(keep_releases)

            # Update database
            if not MTDatabase.save_config(self, {
                'shared_root': shared_root,
                'current_release': release_name,
            }):
                Log.error(self, "Failed to persist multitenancy "
                                "configuration to the tracking database")
            
            Log.info(self, "Multi-tenancy infrastructure initialized successfully!")
            Log.info(self, f"   Shared root: {shared_root}")
            Log.info(self, f"   Current release: {release_name}")
            
        except Exception as e:
            Log.error(self, f"Failed to initialize multi-tenancy: {str(e)}")

    @expose(help="Create a WordPress site using shared core")
    @fleet_operation('create')
    def create(self):
        """Create a new site with shared WordPress core"""
        return self._create_impl()

    def _create_impl(self):
        pargs = self.app.pargs
        _reject_extra_positionals(self, pargs)

        if not pargs.site_name:
            try:
                while not pargs.site_name:
                    pargs.site_name = input('Enter site name : ').strip()
            except IOError:
                Log.error(self, 'Could not input site name')
        
        # Validate domain
        wo_domain = WODomain.validate(self, pargs.site_name)
        if not MTFunctions.valid_tenant_domain(wo_domain):
            Log.error(self, f"Invalid domain name: {wo_domain!r}")


        # Check if site exists
        if check_domain_exists(self, wo_domain):
            Log.error(self, f"Site {wo_domain} already exists")
        
        # Check if multi-tenancy is initialized
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized. Run: wo multitenancy init")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        old_content = (
            not is_cut_over(shared_root)
            and os.path.isdir(os.path.join(shared_root, 'wp-content'))
        )
        if not current_content_release(shared_root) and not old_content:
            Log.error(self, "No content release is live. "
                            "Run tools/deploy from the site repo first.")
            return
        if not MTFunctions.preflight_shared_config(self, shared_root):
            Log.error(self, "Aborted: wp-config-shared.php failed preflight")

        # Determine PHP version
        php_version = MTFunctions.get_php_version(self, pargs)

        # Determine cache type
        cache_type = MTFunctions.get_cache_type(self, pargs)
        if cache_type == 'wpredis':
            if not pargs.force:
                Log.error(self,
                          "--wpredis is disabled: nginx-wo 1.30.4 workers "
                          "segfault (signal 11) on the srcache/redis2 "
                          "page-cache path, taking down all tenants. Use "
                          "--wpfc instead, or pass --force to override.")
            Log.warn(self, "--wpredis forced: nginx workers may segfault on "
                           "every page view until the nginx build is fixed")

        # Ensure required stack packages (nginx, PHP-FPM, MariaDB, WP-CLI,
        # redis) are installed, exactly like `wo site create` does.
        # site_package_check reads pargs flags the multitenancy parser
        # doesn't define, so default them first.
        for flag in ('php85', 'ngxblocker'):
            if not hasattr(pargs, flag):
                setattr(pargs, flag, False)
        # Every shared-core site gets the Object Cache Pro drop-in, so Redis
        # is a hard dependency regardless of the selected page-cache type.
        # site_package_check only installs redis when pargs.wpredis is set,
        # so force it for the check and restore the user's flag afterwards.
        saved_wpredis = pargs.wpredis
        pargs.wpredis = True
        try:
            site_package_check(self, 'wp')
        finally:
            pargs.wpredis = saved_wpredis

        Log.info(self, f"Creating shared WordPress site: {wo_domain}")
        Log.info(self, f"   PHP version: {php_version}")
        Log.info(self, f"   Cache type: {cache_type}")

        # Initialized before the try so the failure handler can drop whatever
        # setupdatabase managed to create before the exception.
        db_name = db_user = db_grant_host = None

        try:
            # Create site directory structure
            site_root = f"/var/www/{wo_domain}"
            site_htdocs = f"{site_root}/htdocs"
            
            Log.info(self, "Creating site directory structure...")
            MTFunctions.create_site_directories(self, wo_domain, site_root, site_htdocs)
            
            # Create database
            Log.info(self, "Setting up database...")
            db_data = {
                'site_name': wo_domain,
                'webroot': site_root,
            }
            db_data = setupdatabase(self, db_data)
            if not db_data or not all(k in db_data for k in ['wo_db_name', 'wo_db_user', 'wo_db_pass', 'wo_db_host']):
                raise Exception("Failed to create database")
            
            db_name = db_data['wo_db_name']
            db_user = db_data['wo_db_user']
            db_pass = db_data['wo_db_pass']
            db_host = db_data['wo_db_host']
            # DROP USER host for cleanup; distinct from the connection host.
            db_grant_host = db_data.get('wo_mysql_grant_host', 'localhost')
            
            # Create symlinks to shared infrastructure
            Log.info(self, "Linking to shared WordPress core...")
            MTFunctions.create_shared_symlinks(self, site_htdocs, shared_root)
            
            # ================================================================
            # PHASE 2: Generate unique Redis prefix for cache isolation
            # ================================================================
            # Each site gets a unique Redis prefix to prevent cache collisions
            # in the shared Redis instance. The prefix is:
            # 1. Generated with collision detection (adds hash if needed)
            # 2. Written to wp-config.php for Redis Object Cache Pro
            # 3. Stored in database via site_data for tracking and debugging
            Log.info(self, "Generating Redis cache prefix...")
            redis_prefix = MTDatabase.generate_redis_prefix(self, wo_domain)
            redis_db = MTDatabase.allocate_redis_db(self, wo_domain)
            MTFunctions.ensure_redis_databases(self, redis_db + 1)
            Log.debug(self, f"Redis prefix for {wo_domain}: {redis_prefix} (db {redis_db})")

            # A previous site with this exact domain leaves FastCGI page-cache
            # entries and Redis object-cache keys behind (delete does not purge
            # them, and both key on the domain). Clear them now so this fresh
            # site never serves the old incarnation's cached pages/options.
            MTFunctions.purge_site_cache(self, wo_domain, redis_prefix, redis_db)
            
            # ================================================================
            # Generate wp-config.php with Redis prefix and shared config
            # ================================================================
            # Creates site-specific wp-config.php that includes:
            # - Unique Redis prefix (for cache isolation)
            # - Shared configuration include (fleet-wide settings)
            # - Site-specific database credentials
            # - Site-specific authentication salts
            Log.info(self, "Generating wp-config.php...")
            MTFunctions.generate_wp_config(
                self, site_root, wo_domain,
                db_name, db_user, db_pass, db_host,
                redis_prefix=redis_prefix,  # Pass explicit prefix for consistency
                redis_db=redis_db,
                shared_root=shared_root,
            )
            
            # Generate nginx configuration
            Log.info(self, "Configuring nginx...")
            nginx_conf = MTFunctions.generate_nginx_config(
                self, wo_domain, php_version, cache_type, site_root
            )
            
            # Install WordPress
            Log.info(self, "Installing WordPress...")
            MTFunctions.install_wordpress(
                self, wo_domain, site_htdocs,
                pargs.admin_user, pargs.admin_email or config.get('admin_email', 'admin@example.com')
            )
            
            # Apply baseline configuration
            baseline_path = f"{shared_root}/config/baseline.json"
            baseline = None
            baseline_complete = True
            if os.path.exists(baseline_path):
                try:
                    with open(baseline_path, 'r') as baseline_file:
                        baseline = json.load(baseline_file)
                    if not baseline.get('theme'):
                        Log.warn(
                            self,
                            "Site has no baseline theme. Set it in the site repo."
                        )
                    Log.info(self, "Applying baseline configuration...")
                    baseline_result = BaselineApplicator.apply_baseline_to_site(
                        self, wo_domain, site_htdocs, baseline, prune=False,
                        cache_type=cache_type
                    )
                    if not baseline_result.get('success'):
                        Log.warn(
                            self,
                            f"Baseline configuration failed for {wo_domain}: "
                            f"{baseline_result.get('error')}"
                        )
                        baseline_complete = False
                    elif baseline_result.get('skipped_plugins'):
                        Log.warn(
                            self,
                            f"Baseline applied for {wo_domain} but skipped "
                            f"plugins: "
                            f"{', '.join(baseline_result['skipped_plugins'])}"
                        )
                        baseline_complete = False
                except Exception as e:
                    baseline = None
                    baseline_complete = False
                    Log.warn(
                        self,
                        f"Could not read baseline.json; skipping baseline activation: {e}"
                    )
            else:
                Log.warn(self, "baseline.json missing; skipping baseline activation")
                baseline_complete = False
            
            # Re-assert the permalink after baseline (install_wordpress already
            # wrote it to the DB): baseline enables Object Cache Pro with
            # --skip-flush, so writing the option now routes through the active
            # object cache and overwrites any stale permalink_structure cached
            # under this tenant's Redis prefix. Unconditional, so sites created
            # without a baseline refresh correctly too.
            MTFunctions.set_permalink_structure(self, wo_domain, site_htdocs)

            # Set permissions
            Log.info(self, "Setting permissions...")
            setwebrootpermissions(self, site_htdocs)
            
            # Test nginx configuration before enabling site
            if not MTFunctions.validate_nginx_config_recoverable(self, log_errors=True):
                raise Exception("Nginx configuration validation failed "
                                "before enabling site")

            # Enable site in nginx first (without SSL)
            WOFileUtils.create_symlink(self, [
                f"/etc/nginx/sites-available/{wo_domain}",
                f"/etc/nginx/sites-enabled/{wo_domain}"
            ])

            # Test nginx configuration after enabling site
            if not MTFunctions.validate_nginx_config_recoverable(self, log_errors=True):
                # Remove the symlink we just created before failing
                if os.path.exists(f"/etc/nginx/sites-enabled/{wo_domain}"):
                    os.remove(f"/etc/nginx/sites-enabled/{wo_domain}")
                raise Exception("Nginx configuration invalid after enabling site")

            # Reload nginx using our enhanced function
            try:
                if not MTFunctions.safe_nginx_reload(self, wo_domain):
                    raise Exception("Failed to reload nginx "
                                    "(see diagnostics above)")
                else:
                    Log.debug(self, "Nginx reloaded successfully")
            except Exception as reload_error:
                Log.error(self, f"Nginx reload error: {reload_error}",
                          exit=False)
                # Try to disable the site and reload to restore working state
                if os.path.exists(f"/etc/nginx/sites-enabled/{wo_domain}"):
                    os.remove(f"/etc/nginx/sites-enabled/{wo_domain}")
                    Log.info(self, "Disabled problematic site configuration")
                    # Try to reload nginx again after disabling the site
                    MTFunctions.safe_nginx_reload(self, wo_domain)
                raise Exception("Failed to reload nginx after site creation")
            
            # Add to database (WordOps core DB and plugin DB)
            site_data = {
                'domain': wo_domain,
                'site_type': 'wp',
                'cache_type': cache_type,
                'site_path': site_root,
                'php_version': php_version,
                'is_shared': True,
                'is_ssl': False,  # Will be updated after SSL setup
                'shared_release': MTDatabase.get_current_release(self),
                'redis_prefix': redis_prefix,  # Phase 2: Store Redis prefix with site data
                'redis_db': redis_db,  # Dedicated Redis database (OCP FLUSHDB isolation)
            }
            addNewSite(
                self,
                wo_domain,
                'wp',
                cache_type,
                site_root,
                enabled=True,
                ssl=False,  # Will be updated after SSL setup
                fs='ext4',
                db='mysql',
                db_name=db_name,
                db_user=db_user,
                db_password=db_pass,
                db_host=db_host,
                php_version=php_version
            )
            MTDatabase.add_shared_site(self, wo_domain, site_data)
            
            # Configure SSL if requested
            if pargs.letsencrypt:
                Log.info(self, "Configuring Let's Encrypt SSL...")
                ssl_success = MTFunctions.setup_ssl(self, wo_domain, pargs)
                if ssl_success:
                    # Update database with SSL status
                    updateSiteInfo(self, wo_domain, ssl=True)
                    site_data['is_ssl'] = True
                    MTDatabase.add_shared_site(self, wo_domain, site_data)
                    # Reload nginx again after SSL using our robust function
                    if not MTFunctions.safe_nginx_reload(self, wo_domain):
                        Log.warn(self, "Failed to reload nginx after SSL setup")
                    else:
                        Log.debug(self, "Nginx reloaded successfully after SSL deployment")
                else:
                    Log.warn(self, f"SSL setup failed; {wo_domain} "
                                   "stays on HTTP")

            # Git commit
            WOGit.add(self, ["/etc/nginx"], 
                     msg=f"Created shared WordPress site: {wo_domain}")
            
            # Display success message
            admin_pass = MTFunctions.get_admin_password(self, wo_domain)
            site_url = f"https://{wo_domain}" if pargs.letsencrypt else f"http://{wo_domain}"

            banner_status = "success" if baseline_complete else "partial"
            Log.info(self, f"site_created target={wo_domain} result={banner_status}")

            Log.info(self, "")
            if baseline_complete:
                Log.info(self, "🎉 WordPress site created successfully!")
            else:
                Log.info(
                    self,
                    "⚠️  WordPress site created, but baseline configuration "
                    "did not fully apply."
                )
                Log.info(
                    self,
                    "   Review the warnings above, then run "
                    "'wo multitenancy apply' to retry."
                )
            Log.info(self, f"   URL: {site_url}")
            Log.info(self, f"   Admin URL: {site_url}/wp-admin")
            Log.info(self, f"   Admin user: {pargs.admin_user}")
            Log.info(self, f"   Admin password: {admin_pass}")
            self._sync_wp_cron_or_fail()
            Log.info(self, "")

        except Exception as e:
            Log.info(self, f"site_create_failed target={wo_domain} result=failure")
            # Cleanup must run before Log.error: Log.error exits the process,
            # so anything after it in this handler is unreachable.
            try:
                Log.info(self, "Attempting cleanup of partially created site...")
                MTFunctions.cleanup_failed_site(
                    self, wo_domain, site_root,
                    db_name=db_name, db_user=db_user,
                    db_grant_host=db_grant_host)
                Log.info(self, "Cleanup completed")
            except Exception as cleanup_error:
                Log.warn(self, f"Cleanup failed: {cleanup_error}")
            Log.error(self, f"Failed to create site: {str(e)}")
            return False

    def _recover_pending_db_upgrades(self, config, shared_root,
                                     shared_sites):
        """Finish tenant DB migrations an interrupted update left pending."""
        marker_path = _pending_upgrades_path(shared_root)
        if not os.path.exists(marker_path):
            return
        try:
            with open(marker_path) as fh:
                marker = json.load(fh)
        except (OSError, ValueError) as e:
            Log.error(self, "Unreadable pending-db-upgrades ledger "
                            f"{marker_path}: {e}; inspect and remove it "
                            "before updating")
            return
        domains = list(marker.get('domains') or [])
        if not domains:
            try:
                os.remove(marker_path)
            except OSError:
                pass
            return
        Log.warn(self, "Unfinished tenant DB migrations from release "
                       f"{marker.get('release')}: {', '.join(domains)}")
        Log.info(self, "Recovering pending tenant DB migrations...")
        tracked = {
            site.get('domain'): site for site in (shared_sites or [])
        }
        recovery_message = (
            "WordPress database maintenance is in progress. "
            "Please try again shortly."
        )
        sites = []
        for domain in domains:
            site = tracked.get(domain)
            if site is None:
                if not MTFunctions.valid_tenant_domain(domain):
                    Log.error(self, "Invalid domain in pending-db-upgrades "
                                    f"ledger: {domain!r}")
                site = {'domain': domain, 'site_path': f'/var/www/{domain}'}
            sites.append(site)
        created_recovery_gates = []
        for site in sites:
            domain = site['domain']
            if _maintenance_gate_exists(domain):
                continue
            if not _maintenance_enable(
                    self, domain, recovery_message, config):
                Log.error(self, "Could not enable recovery maintenance "
                                f"gate for {domain}")
            created_recovery_gates.append(domain)
        if created_recovery_gates and not MTFunctions.safe_nginx_reload(
                self, created_recovery_gates[-1]):
            Log.error(self, "Could not reload nginx with recovery "
                            "maintenance gates")
        survivors = []
        for site in sites:
            domain = site['domain']
            failures = MTFunctions.run_core_db_upgrades(self, [site])
            if failures:
                survivors.append(domain)
                continue
            _remove_pending_upgrade_domain(self, shared_root, domain)
        ungate_failures = []
        for domain in created_recovery_gates:
            if not _maintenance_disable(self, domain):
                ungate_failures.append(domain)
        if created_recovery_gates and not MTFunctions.safe_nginx_reload(
                self, created_recovery_gates[-1]):
            ungate_failures.append('nginx reload')
        if ungate_failures:
            Log.warn(self, "Recovery ungate incomplete: "
                     + ', '.join(ungate_failures))
        if survivors:
            cmds = '; '.join(
                f"wp core update-db --path=/var/www/{d}/htdocs --allow-root"
                for d in survivors
            )
            Log.error(self, "Pending tenant DB migrations still failing "
                            f"for {', '.join(survivors)} (gates retained). "
                            f"Run manually: {cmds}")
        Log.info(self, "Recovered all pending tenant DB migrations")

    @expose(help="Update WordPress core for all shared sites")
    @fleet_operation('update')
    def update(self):
        """Update shared WordPress infrastructure"""
        pargs = self.app.pargs
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        if not MTFunctions.preflight_shared_config(self, shared_root):
            Log.error(self, "Aborted: wp-config-shared.php failed preflight")
        
        Log.info(self, "Updating WordPress multi-tenancy infrastructure...")

        # Get list of shared sites
        shared_sites = MTDatabase.get_shared_sites(self)

        # Finish tenant migrations a prior interrupted update left pending
        # BEFORE the empty-fleet shortcut: get_shared_sites() returns [] on
        # any DB exception and the ledger exists precisely for that case.
        self._recover_pending_db_upgrades(config, shared_root, shared_sites)

        if not shared_sites:
            Log.info(self, "No shared sites found")
            return

        Log.info(self, f"Found {len(shared_sites)} shared sites")

        # Create new release
        infra = SharedInfrastructure(self, shared_root)
        release_manager = ReleaseManager(self, shared_root)

        core_switched = False
        core_db_bump = False
        db_backup_dir = None
        db_upgrade_failures = []
        gated_domains = []
        preexisting_gated_domains = []
        cron_locks = {}
        maintenance_message = None
        loopback_bypass_may_be_active = False
        new_release = None

        try:
            # Create new release
            Log.info(self, "Creating new WordPress release...")
            new_release = infra.download_wordpress_core(config.get('wp_version'))
            schema_transition = MTFunctions.core_schema_transition(
                self, shared_root, new_release
            )
            if schema_transition == 'unknown':
                raise _UpdateAbort(
                    "Cannot read $wp_db_version from both active and staged "
                    "WordPress cores; update aborted before the core switch"
                )
            if schema_transition == 'downgrade':
                raise _UpdateAbort(
                    "Staged WordPress core has an older database schema; "
                    "automated DB downgrade is unsupported. Restore the "
                    "matching tenant DB dumps and older release manually."
                )
            core_db_bump = schema_transition == 'upgrade'
            if core_db_bump:
                unsafe_domains = [
                    site.get('domain') for site in shared_sites
                    if not MTFunctions.valid_tenant_domain(
                        site.get('domain')
                    )
                ]
                if unsafe_domains:
                    raise _UpdateAbort(
                        "Unsafe tracked tenant domain; update aborted before "
                        f"path construction: {unsafe_domains!r}"
                    )

            # Backup current state
            Log.info(self, "Backing up current release...")
            release_manager.backup_current()
            if core_db_bump:
                old_nginx_worker_pids, nginx_worker_error = (
                    MTFunctions.capture_nginx_worker_pids()
                )
                if nginx_worker_error:
                    raise _UpdateAbort(
                        "Could not capture pre-gate nginx workers: "
                        f"{nginx_worker_error}"
                    )
                maintenance_message = (
                    "WordPress database maintenance is in progress. "
                    "Please try again shortly."
                )
                Log.info(self, "Enabling per-site update maintenance gates...")
                for site in shared_sites:
                    domain = site['domain']
                    if _maintenance_gate_exists(domain):
                        preexisting_gated_domains.append(domain)
                        Log.warn(
                            self,
                            f"Preserving pre-existing maintenance gate for "
                            f"{domain}"
                        )
                        continue
                    gated_domains.append(domain)
                    if not _maintenance_enable(
                            self, domain, maintenance_message, config,
                            loopback_bypass=False):
                        raise _UpdateAbort(
                            f"Could not enable maintenance gate for {domain}"
                        )
                if not gated_domains and not pargs.force:
                    raise _UpdateAbort(
                        "No update-owned gate is available for the loopback "
                        "canary. Existing maintenance gates remain unchanged."
                    )
                if not MTFunctions.safe_nginx_reload(
                        self, shared_sites[-1]['domain']):
                    raise _UpdateAbort(
                        "Could not reload nginx with tenant maintenance gates"
                    )
                cron_locks, cron_lock_failures = (
                    MTFunctions.acquire_tenant_cron_locks(
                        self, shared_sites, timeout=30
                    )
                )
                if cron_lock_failures:
                    raise _UpdateAbort(
                        "Could not drain tenant WP-Cron before release flip: "
                        f"{cron_lock_failures}"
                    )
                if not MTFunctions.sync_wp_cron_entries(self):
                    raise _UpdateAbort(
                        "Could not install maintenance-aware WP-Cron entries"
                    )
                drained, drain_blockers = MTFunctions.drain_tenant_active_work(
                    self,
                    shared_sites,
                    cron_locks,
                    old_nginx_worker_pids,
                    timeout=330,
                    sleeper_horizon=60,
                    poll_interval=2,
                )
                if not drained:
                    raise _UpdateAbort(
                        "Could not drain active tenant work before promotion: "
                        + ', '.join(drain_blockers)
                    )
                Log.info(
                    self,
                    "Tenant traffic and WP-Cron are quiesced; backing up "
                    "tenant databases..."
                )
                backups_ok, backup_failures, db_backup_dir = (
                    MTFunctions.backup_tenant_databases(
                        self,
                        shared_sites,
                        shared_root,
                    )
                )
                Log.info(self, f"Tenant DB backup directory: {db_backup_dir}")
                if not backups_ok:
                    for failure in backup_failures:
                        Log.error(
                            self,
                            f"DB backup failed for {failure['domain']}: "
                            f"{failure['error']}",
                            exit=False,
                        )
                    raise _UpdateAbort(
                        "Quiesced tenant database backup incomplete; "
                        "update aborted"
                    )

            if core_db_bump:
                # Durable per-tenant migration ledger: SQLite failing is the
                # failure mode, so persist which tenants still need
                # `wp core update-db` on the filesystem before the flip.
                try:
                    _write_pending_upgrades(shared_root, {
                        'release': new_release,
                        'created': datetime.now().isoformat(),
                        'domains': [s['domain'] for s in shared_sites],
                    })
                except OSError as e:
                    raise _UpdateAbort(
                        f"Could not write pending-db-upgrades ledger: {e}"
                    )

            # Switch to new release
            Log.info(self, "Switching to new release...")
            infra.switch_release(new_release)
            core_switched = True

            # Clear all caches globally (fast - ~2 seconds for any number of sites)
            MTFunctions.clear_all_caches(self)
            if not MTFunctions.reset_opcache(self):
                Log.warn(self, "Opcache reset incomplete after release flip")

            # Update database
            if not MTDatabase.update_release(self, new_release):
                Log.warn(self, "Could not record new release in tracking DB")
            if not core_db_bump and not pargs.force:
                canary = shared_sites[0]
                Log.info(self, f"Testing new core with canary: {canary['domain']}")
                if not MTFunctions.test_site_locally(self, canary):
                    raise _UpdateAbort(
                        "New core canary failed. Run wo multitenancy rollback "
                        "to restore the previous core."
                    )
            if core_db_bump:
                for site in shared_sites:
                    failures = MTFunctions.run_core_db_upgrades(self, [site])
                    if failures:
                        db_upgrade_failures.extend(failures)
                    else:
                        _remove_pending_upgrade_domain(
                            self, shared_root, site['domain'])

                failed_domains = {
                    failure['domain'] for failure in db_upgrade_failures
                }
                canary_domains = [
                    domain for domain in gated_domains
                    if domain not in failed_domains
                ]
                if not pargs.force and canary_domains:
                    canary_domain = canary_domains[0]
                    canary = next(
                        site for site in shared_sites
                        if site['domain'] == canary_domain
                    )
                    for domain in canary_domains:
                        loopback_bypass_may_be_active = True
                        if not _maintenance_enable(
                                self, domain, maintenance_message, config,
                                loopback_bypass=True):
                            raise _UpdateAbort(
                                "Could not enable loopback bypass for "
                                f"{domain}"
                            )
                    if not MTFunctions.safe_nginx_reload(
                            self, canary_domain):
                        raise _UpdateAbort(
                            "Could not reload nginx with loopback bypass"
                        )

                    Log.info(
                        self,
                        f"Testing gated canary locally: {canary_domain}"
                    )
                    canary_ok = MTFunctions.test_site_locally(self, canary)
                    canary_nginx_pids, nginx_worker_error = (
                        MTFunctions.capture_nginx_worker_pids()
                    )
                    if nginx_worker_error:
                        raise _UpdateAbort(
                            "Could not capture canary nginx workers: "
                            f"{nginx_worker_error}"
                        )
                    close_failures = _close_update_loopback_bypass(
                        self,
                        gated_domains,
                        maintenance_message,
                        config,
                    )
                    if close_failures:
                        raise _UpdateAbort(
                            "Could not close loopback bypass: "
                            + '; '.join(close_failures)
                        )
                    loopback_bypass_may_be_active = False
                    if not canary_ok:
                        raise _UpdateAbort(
                            "Gated local canary failed; update aborted"
                        )
                    drained, drain_blockers = (
                        MTFunctions.drain_tenant_active_work(
                            self,
                            shared_sites,
                            cron_locks,
                            canary_nginx_pids,
                            timeout=330,
                            sleeper_horizon=0,
                            poll_interval=2,
                        )
                    )
                    if not drained:
                        raise _UpdateAbort(
                            "Could not drain loopback canary work before "
                            "maintenance removal: " + ', '.join(drain_blockers)
                        )
                pending_ungate_domains = []
                sites_by_domain = {
                    site['domain']: site for site in shared_sites
                }
                for site in shared_sites:
                    domain = site['domain']
                    if (domain in preexisting_gated_domains
                            or domain in failed_domains):
                        MTFunctions.release_tenant_cron_locks({
                            domain: cron_locks.pop(domain)
                        })
                        continue

                    if _maintenance_disable(self, domain):
                        pending_ungate_domains.append(domain)
                        MTFunctions.release_tenant_cron_locks({
                            domain: cron_locks.pop(domain)
                        })
                        continue

                    # Keep the gate durable when its removal fails.
                    _maintenance_enable(
                        self, domain, maintenance_message, config
                    )
                    db_upgrade_failures.append({
                        'domain': domain,
                        'path': MTFunctions._site_htdocs(site),
                        'error': 'database upgraded but maintenance ungate failed',
                        'gate_failure': True,
                    })
                    MTFunctions.release_tenant_cron_locks({
                        domain: cron_locks.pop(domain)
                    })

                # Apply every successful site's ungate in one nginx reload.
                reload_target = shared_sites[-1]['domain']
                ungate_reload_ok = MTFunctions.safe_nginx_reload(
                    self, reload_target
                )
                if ungate_reload_ok:
                    for domain in pending_ungate_domains:
                        gated_domains.remove(domain)
                else:
                    # Running nginx is still gated. Restore the include files
                    # so a future reload cannot accidentally open these sites.
                    for domain in pending_ungate_domains:
                        _maintenance_enable(
                            self, domain, maintenance_message, config
                        )
                        site = sites_by_domain[domain]
                        db_upgrade_failures.append({
                            'domain': domain,
                            'path': MTFunctions._site_htdocs(site),
                            'error': (
                                'database upgraded but batch maintenance '
                                'ungate reload failed'
                            ),
                            'gate_failure': True,
                        })

                for failure in db_upgrade_failures:
                    Log.error(
                        self,
                        f"Tenant update failed for {failure['domain']}: "
                        f"{failure['error']}",
                        exit=False,
                    )
                    if not failure.get('gate_failure'):
                        Log.warn(
                            self,
                            "Run manually: wp core update-db "
                            f"--path={failure['path']} --allow-root"
                        )
                result = 'partial' if db_upgrade_failures else 'success'
                Log.info(
                    self,
                    f"db_upgrade result={result} "
                    f"failed={len(db_upgrade_failures)} "
                    f"backup_dir={db_backup_dir}"
                )

            # Cleanup old releases
            Log.info(self, "Cleaning up old releases...")
            release_manager.cleanup_old_releases(int(config.get('keep_releases', 3)))

            for domain in preexisting_gated_domains:
                Log.warn(
                    self,
                    f"Pre-existing maintenance gate remains active for "
                    f"{domain}"
                )

            if not db_upgrade_failures:
                Log.info(self, "✅ Update completed successfully!")
                Log.info(self, f"   New release: {new_release}")
                Log.info(self, f"   Updated {len(shared_sites)} sites")
                Log.info(
                    self,
                    f"update_completed target=core result=success "
                    f"release={new_release}"
                )

        except Exception as e:
            cleanup_failures = []
            bypass_closed = True
            if loopback_bypass_may_be_active and gated_domains:
                cleanup_failures = _close_update_loopback_bypass(
                    self, gated_domains, maintenance_message, config)
                bypass_closed = not cleanup_failures
            if cron_locks:
                MTFunctions.release_tenant_cron_locks(cron_locks)
            if gated_domains and not core_switched and bypass_closed:
                cleanup_failures.extend(
                    _release_update_maintenance(self, gated_domains))
            if core_switched and core_db_bump:
                Log.warn(self, "The new core remains active. Maintenance gates "
                               "remain closed. Check the pending migration "
                               "list and tenant database dumps before recovery.")
            message = str(e)
            if cleanup_failures:
                message += "; maintenance cleanup incomplete: " + "; ".join(cleanup_failures)
            Log.info(self, "update_failed target=core result=failure")
            Log.error(self, message)
            return

        finally:
            if new_release and not core_switched:
                staged = f"{shared_root}/releases/{new_release}"
                if (os.path.isdir(staged) and os.path.realpath(staged)
                        != os.path.realpath(f"{shared_root}/current")):
                    shutil.rmtree(staged, ignore_errors=True)
                    Log.info(self, f"Removed staged release {new_release}")

        if db_upgrade_failures:
            failed_domains = ','.join(
                failure['domain'] for failure in db_upgrade_failures
            )
            Log.info(
                self,
                "update_completed target=core result=partial "
                f"failed_db_upgrades={len(db_upgrade_failures)}"
            )
            for domain in gated_domains:
                Log.warn(
                    self,
                    "Site remains maintenance-gated: "
                    f"{domain}. Manual ungate: wo multitenancy maintenance "
                    f"--disable --site={domain}"
                )
            Log.error(
                self,
                "WordPress core is active but DB upgrades failed for "
                f"{failed_domains}; backup_dir={db_backup_dir}. Run the "
                "per-site wp core update-db remediation commands above."
            )
            return

    @expose(help="Rollback to previous WordPress release")
    @fleet_operation('rollback')
    def rollback(self):
        """Rollback to previous release"""
        pargs = self.app.pargs
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        release_manager = ReleaseManager(self, shared_root)
        
        try:
            # Get available releases
            current_release = release_manager.get_current_release()
            previous_release = release_manager.get_previous_release(current_release)
            
            if not previous_release:
                Log.error(self, "No previous release available for rollback")
            
            Log.info(self, f"Current release: {current_release}")
            Log.info(self, f"Rolling back to: {previous_release}")
            
            if not pargs.force:
                confirm = input("This will affect all shared sites. Continue? (y/N): ")
                if confirm.lower() != 'y':
                    Log.info(self, "Rollback cancelled")
                    return
            
            # Perform rollback
            Log.info(self, "Performing rollback...")
            infra = SharedInfrastructure(self, shared_root)
            infra.switch_release(previous_release)

            # Clear all caches globally (fast - ~2 seconds for any number of sites)
            MTFunctions.clear_all_caches(self)
            if not MTFunctions.reset_opcache(self):
                Log.warn(self, "Opcache reset incomplete after rollback")

            # Update database
            if not MTDatabase.update_release(self, previous_release):
                Log.warn(self, "Could not record release in tracking DB")

            Log.info(self, "✅ Rollback completed successfully!")
            Log.info(self, f"   Now running: {previous_release}")
            Log.info(self, f"rollback_triggered target=core result=success to={previous_release}")
            Log.info(
                self,
                "WordPress DB migrations are forward-only. If `wp core "
                "update-db` ran after the pre-upgrade dump, restore tenant "
                f"dumps from {shared_root}/backups/db/<timestamp>/ with "
                "`wp db import`, or validate the old core against the newer "
                "schema."
            )


        except Exception as e:
            Log.info(self, "rollback_failed target=core result=failure")
            Log.error(self, f"Rollback failed: {str(e)}")

    @expose(help="Show status of multi-tenancy infrastructure")
    def status(self):
        """Display multi-tenancy status and health check"""

        if not MTDatabase.is_initialized(self):
            Log.info(self, "❌ Multi-tenancy not initialized")
            Log.info(self, "   Run: wo multitenancy init")
            return

        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')

        # Get current state
        current_release = MTDatabase.get_current_release(self)
        shared_sites = MTDatabase.get_shared_sites(self)
        baseline = None
        try:
            with open(f"{shared_root}/config/baseline.json") as fh:
                baseline = json.load(fh)
        except (OSError, ValueError):
            baseline = None
        content_release = current_content_release(shared_root)
        deployed_commit = (
            read_release_info(content_release).get('commit', 'unknown')
            if content_release else 'old layout, not cut over'
        )

        Log.info(self, "")
        Log.info(self, "=== WordPress Multi-tenancy Status ===")
        Log.info(self, "")
        
        # Infrastructure status
        Log.info(self, "INFRASTRUCTURE:")
        Log.info(self, f"  Shared root: {shared_root}")
        Log.info(self, f"  Current release: {current_release}")
        Log.info(self, f"  Deployed commit: {deployed_commit}")
        
        # Check infrastructure health
        health_checks = MTFunctions.perform_health_check(self, shared_root)
        Log.info(self, "")
        Log.info(self, "HEALTH CHECKS:")
        for check, status in health_checks.items():
            status_icon = "✅" if status else "❌"
            Log.info(self, f"  {status_icon} {check}")
        
        # List releases
        release_manager = ReleaseManager(self, shared_root)
        releases = release_manager.list_releases()
        Log.info(self, "")
        Log.info(self, f"RELEASES: ({len(releases)} total)")
        for i, release in enumerate(releases[:3]):  # Show latest 3
            marker = " (current)" if release == current_release else ""
            Log.info(self, f"  - {release}{marker}")
        
        # Shared sites
        Log.info(self, "")
        Log.info(self, f"SHARED SITES: ({len(shared_sites)} total)")
        if shared_sites:
            for site in shared_sites[:10]:  # Show first 10
                Log.info(self, f"  - {site['domain']} (PHP {site.get('php_version', 'unknown')}, "
                             f"Cache: {site.get('cache_type', 'none')})")
            if len(shared_sites) > 10:
                Log.info(self, f"  ... and {len(shared_sites) - 10} more")
        else:
            Log.info(self, "  No shared sites created yet")
        
        # Disk usage
        Log.info(self, "")
        Log.info(self, "DISK USAGE:")
        disk_usage = MTFunctions.calculate_disk_usage(self, shared_root, shared_sites)
        for key, value in disk_usage.items():
            Log.info(self, f"  {key}: {value}")
        
        # Baseline configuration
        if isinstance(baseline, dict):
            Log.info(self, "")
            Log.info(self, "BASELINE CONFIGURATION:")
            Log.info(self, f"  Plugins: {', '.join(baseline.get('plugins', []))}")
            Log.info(self, f"  Theme: {baseline.get('theme', 'unknown')}")
        
        Log.info(self, "")
        Log.info(self, "=====================================")

    @expose(help="List all sites using shared WordPress core")
    def list(self):
        """List all shared WordPress sites"""

        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")

        shared_sites = MTDatabase.get_shared_sites(self)

        if not shared_sites:
            Log.info(self, "No shared WordPress sites found")
            return

        Log.info(self, "")
        Log.info(self, "Shared WordPress Sites:")
        Log.info(self, "-" * 67)
        Log.info(self, f"{'Domain':<30} {'PHP':<8} {'Cache':<10} {'SSL':<5} {'Status':<10}")
        Log.info(self, "-" * 67)

        for site in shared_sites:
            domain = site['domain']
            php = site.get('php_version', 'unknown')
            cache = site.get('cache_type', 'none')
            ssl = "Yes" if site.get('is_ssl', False) else "No"
            enabled = "Enabled" if site.get('is_enabled', True) else "Disabled"

            Log.info(self, f"{domain:<30} {php:<8} {cache:<10} {ssl:<5} {enabled:<10}")

        Log.info(self, "-" * 67)
        Log.info(self, f"Total: {len(shared_sites)} sites")
        Log.info(self, "")

    @expose(help="Show current baseline configuration")
    def baseline(self):
        """Show current baseline plugins and theme"""

        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")

        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"

        if not os.path.exists(baseline_file):
            Log.error(self, "Baseline configuration not found")
            return

        with open(baseline_file, 'r') as f:
            baseline = json.load(f)

        Log.info(self, json.dumps(baseline, indent=2))

    @expose(help="Validate baseline configuration and site status")
    def validate(self):
        """Validate baseline integrity and site compliance"""
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
            return
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"
        
        Log.info(self, "Validating Baseline Configuration...")
        Log.info(self, "=" * 60)

        # Surface an interrupted update's pending tenant DB migrations.
        marker_path = _pending_upgrades_path(shared_root)
        if os.path.exists(marker_path):
            marker = None
            try:
                with open(marker_path) as fh:
                    marker = json.load(fh)
                pending = ', '.join(marker.get('domains') or []) or '(none)'
            except (OSError, ValueError, AttributeError):
                pending = '(unreadable ledger)'
            release = (marker or {}).get('release') \
                if isinstance(marker, dict) else '?'
            Log.warn(self, "⚠️  Pending tenant DB migrations from release "
                           f"{release}: {pending}")
            Log.warn(self, "   Run: wo multitenancy update (recovers first)")
            Log.info(self, "")
        
        # 1. Check baseline.json exists and is valid JSON
        try:
            with open(baseline_file, 'r') as f:
                baseline = json.load(f)
            Log.info(self, "✅ Baseline JSON valid")
        except FileNotFoundError:
            Log.error(self, "❌ Baseline file not found")
            return
        except json.JSONDecodeError as e:
            Log.error(self, f"❌ Baseline JSON invalid: {e}")
            return
        
        content_release = current_content_release(shared_root)
        deployed_commit = (
            read_release_info(content_release).get('commit', 'unknown')
            if content_release else 'old layout, not cut over'
        )
        Log.info(self, f"Deployed commit: {deployed_commit}")
        
        # 2. Check plugins exist on disk
        plugins = baseline.get('plugins', [])
        Log.info(self, "Plugin Validation:")
        
        missing_plugins = []
        for plugin_slug in plugins:
            plugin_dir = f"{shared_root}/wp-content/plugins/{plugin_slug}"
            
            if os.path.exists(plugin_dir):
                Log.info(self, f"   ✅ {plugin_slug}")
            else:
                Log.warn(self, f"   ❌ {plugin_slug} - NOT FOUND ON DISK")
                missing_plugins.append(plugin_slug)
        
        if not plugins:
            Log.info(self, "   No baseline plugins configured")
        
        Log.info(self, "")
        
        # 3. Check theme exists
        theme_slug = baseline.get('theme')
        missing_theme = False
        if theme_slug:
            Log.info(self, "Theme Validation:")
            theme_dir = f"{shared_root}/wp-content/themes/{theme_slug}"
            
            if os.path.exists(theme_dir):
                Log.info(self, f"   ✅ {theme_slug}")
            else:
                Log.warn(self, f"   ❌ {theme_slug} - NOT FOUND ON DISK")
                missing_theme = True
            
            Log.info(self, "")
        
        # 6. Summary
        Log.info(self, "=" * 60)
        
        if missing_plugins or missing_theme:
            Log.error(self, "VALIDATION FAILED: baseline plugins or theme "
                            "are missing from disk. Deploy the site repo.")
        else:
            Log.info(self, "✅ VALIDATION PASSED: Baseline is healthy")
        
        Log.info(self, "=" * 60)


    @expose(help="Delete a multitenancy site and its tracking")
    @fleet_operation('delete')
    def delete(self):
        """Delete a site from multitenancy system"""
        return self._delete_impl()

    @expose(help="Rename a multitenancy site's primary domain")
    @fleet_operation('rename')
    def rename(self):
        """Rename a shared-core tenant domain in place."""
        result = self._rename_impl()
        if not result:
            self.app.close(1)
        return result

    def _delete_impl(self):
        pargs = self.app.pargs
        domain = pargs.site_name
        
        if not domain:
            Log.error(self, "Usage: wo multitenancy delete <domain>")
            return
        _reject_extra_positionals(self, pargs)
        domain = WODomain.validate(self, domain)
        if not MTFunctions.valid_tenant_domain(domain):
            Log.error(self, f"Invalid domain name: {domain!r}")
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
            return
        
        # Check if site exists in tracking
        from wo.core.database import db_session
        from wo.cli.plugins.multitenancy_db import MultitenancySite
        
        session = db_session
        site = session.query(MultitenancySite).filter_by(domain=domain).first()
        
        if not site:
            Log.error(self, f"Site {domain} not found in multitenancy tracking")
            Log.info(self, "Use: wo site delete {domain} for regular sites")
            return
        
        # Confirm deletion
        if not pargs.force:
            Log.warn(self, f"This will delete site: {domain}")
            confirm = input("Continue? [y/N]: ").strip().lower()
            if confirm != 'y':
                Log.info(self, "Aborted")
                return
        
        Log.info(self, f"Deleting site: {domain}")

        # Preserve the acme.sh certificate store across deletion so a
        # future recreate reuses the certificate instead of burning a
        # Let's Encrypt issuance (tenant domains get recreated often).
        # Hiding the renewal dir makes WOAcme.cert_check() report no
        # cert, so `wo site delete` skips its certificate purge.
        cert_store = f'/etc/letsencrypt/renewal/{domain}_ecc'
        cert_stash = f'{cert_store}.preserved'
        preserve_cert = os.path.isdir(cert_store)
        if preserve_cert:
            Log.debug(self, f"Preserving SSL certificate store for {domain}")
            os.rename(cert_store, cert_stash)

        # Delete the site using regular WO command
        try:
            import subprocess
            result = subprocess.run(
                ['wo', 'site', 'delete', domain, '--no-prompt'],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                Log.error(self, f"Failed to delete site: {result.stderr}")
                Log.info(self, f"site_delete_failed target={domain} result=failure")
                return

            Log.info(self, "✅ Site files and database deleted")

        except Exception as e:
            Log.error(self, f"Error deleting site: {e}")
            Log.info(self, f"site_delete_failed target={domain} result=failure")
            return
        finally:
            if preserve_cert and os.path.isdir(cert_stash):
                os.rename(cert_stash, cert_store)

        # `wo site delete` leaves the FastCGI page cache and Redis object-cache
        # keys behind (both key on the domain); purge them so a future recreate
        # of this domain never inherits stale pages/options.
        MTFunctions.purge_site_cache(
            self, domain, getattr(site, 'redis_prefix', None),
            getattr(site, 'redis_db', None))

        # Evict the deleted tenant's compiled PHP from opcache so a future
        # recreate never executes stale bytecode.
        php_version = str(getattr(site, 'php_version', '') or '')
        php_key = ('php' + php_version.replace('.', '')) if php_version \
            else None
        if not MTFunctions.reset_opcache(self, php_key=php_key):
            Log.warn(self, "Opcache reset failed after site deletion")

        # Remove from multitenancy tracking
        try:
            session.delete(site)
            session.commit()
            if not write_tombstone(domain):
                Log.warn(
                    self,
                    f"Could not write backup tombstone for {domain}; "
                    "deleted-tenant snapshots will not be swept automatically"
                )
            Log.info(self, "✅ Removed from multitenancy tracking")


            Log.info(self, f"site_deleted target={domain} result=success")
            self._sync_wp_cron_or_fail()

        except Exception as e:
            Log.error(self, f"Error removing from tracking: {e}")
            Log.warn(self, "Site deleted but tracking entry remains")
            Log.warn(self, f"Manually clean up with: sqlite3 /var/lib/wo/dbase.db \"DELETE FROM multitenancy_sites WHERE domain = '{domain}';\"")


    def _rename_impl(self):
        pargs = self.app.pargs

        if not pargs.site_name or not pargs.newsite_name:
            Log.error(self, "Usage: wo multitenancy rename <old-domain> <new-domain>", exit=False)
            return False

        old_domain = WODomain.validate(self, pargs.site_name)
        new_domain = WODomain.validate(self, pargs.newsite_name)

        if old_domain == new_domain:
            Log.error(self, "Source and target domains are identical", exit=False)
            return False

        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized", exit=False)
            return False

        site_info = getSiteInfo(self, old_domain)
        if not site_info:
            Log.error(self, f"Site {old_domain} not found in WordOps database", exit=False)
            return False

        # Check if site exists in tracking
        from wo.core.database import db_session
        from wo.cli.plugins.multitenancy_db import MultitenancySite

        session = db_session
        mt_site = session.query(MultitenancySite).filter_by(domain=old_domain).first()

        if not mt_site:
            Log.error(self, f"Site {old_domain} not found in multitenancy tracking", exit=False)
            return False

        if check_domain_exists(self, new_domain):
            Log.error(self, f"Site {new_domain} already exists", exit=False)
            return False

        if MTDatabase.is_shared_site(self, new_domain):
            Log.error(self, f"Site {new_domain} already exists in multitenancy tracking", exit=False)
            return False

        target_paths = [
            f'/var/www/{new_domain}',
            f'/etc/nginx/sites-available/{new_domain}',
            f'/etc/nginx/sites-enabled/{new_domain}',
            f'/etc/nginx/conf.d/force-ssl-{new_domain}.conf',
        ]
        for target_path in target_paths:
            if os.path.lexists(target_path) or os.path.exists(target_path):
                Log.error(self, f"Target path already exists: {target_path}", exit=False)
                return False

        old_root = getattr(mt_site, 'site_path', None) or getattr(site_info, 'site_path', None) or f'/var/www/{old_domain}'
        old_htdocs = f'{old_root}/htdocs'
        old_wp_config = f'{old_htdocs}/wp-config.php'
        if not old_root or not os.path.exists(old_root):
            Log.error(self, f"Site root not found: {old_root}", exit=False)
            return False
        if not os.path.exists(old_wp_config):
            Log.error(self, f"wp-config.php not found: {old_wp_config}", exit=False)
            return False

        new_root = f'/var/www/{new_domain}'
        new_htdocs = f'{new_root}/htdocs'
        cache_type = getattr(mt_site, 'cache_type', None) or getattr(site_info, 'cache_type', None) or 'basic'
        php_version = getattr(mt_site, 'php_version', None) or getattr(site_info, 'php_version', None) or '8.4'
        old_ssl = bool(getattr(mt_site, 'is_ssl', False) or getattr(site_info, 'is_ssl', False))
        ssl_requested = old_ssl or bool(getattr(pargs, 'letsencrypt', False))
        tracked_old_redis_prefix = getattr(mt_site, 'redis_prefix', None)
        new_redis_prefix = MTDatabase.generate_redis_prefix(self, new_domain)
        scheme = 'https' if ssl_requested else 'http'
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_dir = f'/var/www/.wo-rename-{old_domain}-to-{new_domain}-{timestamp}'

        if not pargs.force:
            Log.warn(self, f"This will rename site: {old_domain} -> {new_domain}")
            confirm = input("Continue? [y/N]: ").strip().lower()
            if confirm != 'y':
                Log.info(self, "Aborted")
                return False

        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        if not MTFunctions.preflight_shared_config(self, shared_root):
            return False

        if ssl_requested:
            if not MTFunctions.prepare_ssl_certificate_for_rename(self, new_domain, pargs):
                Log.error(self, f"SSL certificate preparation failed for {new_domain}", exit=False)
                return False

        old_available_path = f'/etc/nginx/sites-available/{old_domain}'
        old_enabled_path = f'/etc/nginx/sites-enabled/{old_domain}'
        old_force_ssl_path = f'/etc/nginx/conf.d/force-ssl-{old_domain}.conf'
        new_available_path = f'/etc/nginx/sites-available/{new_domain}'
        new_enabled_path = f'/etc/nginx/sites-enabled/{new_domain}'
        new_force_ssl_path = f'/etc/nginx/conf.d/force-ssl-{new_domain}.conf'

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

        os.makedirs(backup_dir, mode=0o700, exist_ok=False)
        wp_config_backup = f'{backup_dir}/wp-config.php'
        shutil.copy2(old_wp_config, wp_config_backup)
        os.chmod(wp_config_backup, 0o600)
        rollback['wp_config_backup'] = wp_config_backup

        if os.path.exists(old_available_path):
            old_available_backup = f'{backup_dir}/nginx-site-available-{old_domain}'
            shutil.copy2(old_available_path, old_available_backup)
            rollback['old_available_backup'] = old_available_backup

        if os.path.exists(old_force_ssl_path):
            old_force_ssl_backup = f'{backup_dir}/force-ssl-{old_domain}.conf'
            shutil.copy2(old_force_ssl_path, old_force_ssl_backup)
            rollback['old_force_ssl_backup'] = old_force_ssl_backup

        resolved_old_redis_prefix = None
        ssl_enabled = False

        try:
            if os.path.lexists(old_enabled_path) or os.path.exists(old_enabled_path):
                os.remove(old_enabled_path)
            if os.path.exists(old_force_ssl_path):
                os.remove(old_force_ssl_path)
            if os.path.lexists(old_available_path) or os.path.exists(old_available_path):
                os.remove(old_available_path)

            os.rename(old_root, new_root)
            rollback['root_moved'] = True

            if not MTFunctions.relink_core_files_for_rename(self, new_htdocs):
                raise Exception("Core file symlink relink failed")

            for stale_include_path in (
                    f'{new_root}/conf/nginx/ssl.conf',
                    f'{new_root}/conf/nginx/hsts.conf'):
                if os.path.lexists(stale_include_path) or os.path.exists(stale_include_path):
                    stale_include_backup = f'{stale_include_path}.rename.{timestamp}.bak'
                    os.rename(stale_include_path, stale_include_backup)
                    rollback['stale_include_backups'].append((stale_include_backup, stale_include_path))

            resolved_old_redis_prefix = MTFunctions.rewrite_wp_config_for_rename(
                self, new_root, old_domain, new_domain, new_redis_prefix, tracked_old_redis_prefix)
            if not resolved_old_redis_prefix:
                raise Exception("wp-config rewrite failed")

            nginx_config_path = MTFunctions.write_nginx_config_for_rename(
                self, new_domain, php_version, cache_type, new_root)
            if not nginx_config_path:
                raise Exception("Nginx config generation failed")
            rollback['new_nginx_created'] = True

            if not MTFunctions.enable_nginx_site_for_rename(self, new_domain):
                raise Exception("Nginx site enable failed")
            rollback['new_enabled'] = True

            if not MTFunctions.validate_nginx_config_recoverable(self, log_errors=True):
                raise Exception("Nginx configuration invalid after rename")

            rollback['wp_replaced'] = True
            if not MTFunctions.update_wordpress_domain(self, new_htdocs, old_domain, new_domain, scheme):
                raise Exception("WordPress domain update failed")

            if ssl_requested:
                if not MTFunctions.install_ssl_config_for_rename(self, new_domain, new_root, pargs):
                    raise Exception("SSL setup failed for renamed domain")
                ssl_enabled = True
                for new_ssl_path in (
                        f'/etc/nginx/conf.d/force-ssl-{new_domain}.conf',
                        f'{new_root}/conf/nginx/ssl.conf',
                        f'{new_root}/conf/nginx/hsts.conf'):
                    if os.path.lexists(new_ssl_path) or os.path.exists(new_ssl_path):
                        rollback['new_ssl_paths'].append(new_ssl_path)

            if not renameSiteInfo(self, old_domain, new_domain, site_path=new_root, ssl=ssl_enabled):
                raise Exception("WordOps site database rename failed")
            rollback['sitedb_updated'] = True

            if not MTDatabase.rename_shared_site_domain(
                    self, old_domain, new_domain, site_path=new_root,
                    redis_prefix=new_redis_prefix, is_ssl=ssl_enabled):
                raise Exception("Multitenancy tracking rename failed")
            rollback['mt_updated'] = True


            MTFunctions.purge_site_cache(
                self, old_domain, resolved_old_redis_prefix,
                getattr(mt_site, 'redis_db', None))
            MTFunctions.purge_site_cache(
                self, new_domain, new_redis_prefix,
                getattr(mt_site, 'redis_db', None))

            if not MTFunctions.reload_nginx_recoverable(self, new_domain):
                raise Exception("Nginx reload failed after rename")

            try:
                WOGit.add(self, ["/etc/nginx"], msg=f"Renamed shared WordPress site: {old_domain} -> {new_domain}")
            except Exception as e:
                Log.warn(self, f"Renamed site but failed to update nginx git tracking: {e}")

            shutil.rmtree(backup_dir, ignore_errors=True)

            Log.info(self, f"✅ Renamed site: {old_domain} -> {new_domain}")
            Log.info(self, f"site_renamed source={old_domain} target={new_domain} result=success")
            self._sync_wp_cron_or_fail()
            return True

        except Exception as e:
            try:
                Log.error(self, f"Rename failed: {e}", exit=False)
                Log.info(self, f"site_rename_failed source={old_domain} target={new_domain} result=failure")
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while logging failure: {rollback_error}")

            try:
                if rollback['mt_updated']:
                    if not MTDatabase.rename_shared_site_domain(
                            self, new_domain, old_domain, site_path=old_root,
                            redis_prefix=resolved_old_redis_prefix or tracked_old_redis_prefix,
                            is_ssl=old_ssl):
                        Log.warn(self, "Failed to restore multitenancy tracking during rollback")
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring multitenancy tracking: {rollback_error}")

            try:
                if rollback['sitedb_updated']:
                    if not renameSiteInfo(self, new_domain, old_domain, site_path=old_root, ssl=old_ssl):
                        Log.warn(self, "Failed to restore WordOps site database during rollback")
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring WordOps site database: {rollback_error}")

            try:
                if rollback['wp_replaced']:
                    if not MTFunctions.update_wordpress_domain(
                            self, new_htdocs, new_domain, old_domain,
                            'https' if old_ssl else 'http'):
                        Log.warn(self, "Failed to restore WordPress domain during rollback")
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring WordPress domain: {rollback_error}")

            ssl_cleanup_paths = []
            try:
                ssl_cleanup_paths = list(rollback['new_ssl_paths']) + [
                    f'/etc/nginx/conf.d/force-ssl-{new_domain}.conf',
                    f'{new_root}/conf/nginx/ssl.conf',
                    f'{new_root}/conf/nginx/hsts.conf',
                ]
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while preparing SSL cleanup: {rollback_error}")

            seen_ssl_cleanup_paths = set()
            for ssl_cleanup_path in ssl_cleanup_paths:
                if ssl_cleanup_path in seen_ssl_cleanup_paths:
                    continue
                seen_ssl_cleanup_paths.add(ssl_cleanup_path)
                try:
                    if os.path.lexists(ssl_cleanup_path) or os.path.exists(ssl_cleanup_path):
                        os.remove(ssl_cleanup_path)
                except Exception as rollback_error:
                    Log.warn(self, f"Rollback warning while removing SSL artifact {ssl_cleanup_path}: {rollback_error}")

            try:
                if rollback['wp_config_backup'] and os.path.exists(rollback['wp_config_backup']):
                    active_wp_config = f'{new_htdocs}/wp-config.php' if rollback['root_moved'] else f'{old_htdocs}/wp-config.php'
                    shutil.copy2(rollback['wp_config_backup'], active_wp_config)
                    os.chmod(active_wp_config, 0o640)
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring wp-config.php: {rollback_error}")

            try:
                if rollback['new_enabled']:
                    if os.path.lexists(new_enabled_path) or os.path.exists(new_enabled_path):
                        os.remove(new_enabled_path)
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while removing new nginx symlink: {rollback_error}")

            try:
                if rollback['new_nginx_created']:
                    if os.path.exists(new_available_path):
                        os.remove(new_available_path)
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while removing new nginx config: {rollback_error}")

            try:
                for stale_include_backup, stale_include_path in rollback['stale_include_backups']:
                    if os.path.exists(stale_include_backup):
                        os.rename(stale_include_backup, stale_include_path)
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring stale SSL includes: {rollback_error}")

            try:
                if rollback['root_moved'] and os.path.exists(new_root) and not os.path.exists(old_root):
                    os.rename(new_root, old_root)
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring site root: {rollback_error}")

            try:
                if rollback['old_available_backup'] and os.path.exists(rollback['old_available_backup']):
                    shutil.copy2(rollback['old_available_backup'], old_available_path)
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring old nginx config: {rollback_error}")

            try:
                if rollback['old_enabled'] and (not os.path.exists(old_enabled_path) or not os.path.islink(old_enabled_path)):
                    if os.path.lexists(old_enabled_path) or os.path.exists(old_enabled_path):
                        os.remove(old_enabled_path)
                    os.symlink(f'/etc/nginx/sites-available/{old_domain}', f'/etc/nginx/sites-enabled/{old_domain}')
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring old nginx symlink: {rollback_error}")

            try:
                if rollback['old_force_ssl_backup'] and os.path.exists(rollback['old_force_ssl_backup']):
                    shutil.copy2(rollback['old_force_ssl_backup'], old_force_ssl_path)
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while restoring old force SSL config: {rollback_error}")

            try:
                if not MTFunctions.reload_nginx_recoverable(self, old_domain):
                    Log.warn(self, "Rollback completed but nginx reload failed; run nginx -t")
            except Exception as rollback_error:
                Log.warn(self, f"Rollback warning while reloading nginx: {rollback_error}")

            return False

    @expose(help="Apply current baseline to all sites")
    @fleet_operation('apply')
    def apply(self):
        """Apply baseline to all sites immediately"""
        return self._apply_impl()

    def _apply_impl(self):
        pargs = self.app.pargs
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")

        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        if not MTFunctions.preflight_shared_config(self, shared_root):
            Log.error(self, "Aborted: wp-config-shared.php failed preflight")

        dry_run = bool(getattr(pargs, 'dry_run', False))
        verbose = bool(getattr(pargs, 'verbose', False))
        prune = bool(getattr(pargs, 'prune', False))

        header = "Applying baseline to all sites"
        if dry_run:
            header += ' [DRY RUN]'
        Log.info(self, header + '...')

        # Backfill dedicated Redis databases for tenants created before
        # per-site databases existed. They shared db 0, where Object Cache
        # Pro's FLUSHDB let tenants wipe each other's cache (including OCP's
        # metadata key, causing fleet-wide integrity-flush loops) -- see
        # MTDatabase.allocate_redis_db.
        for mt_site in MTDatabase.get_shared_sites(self):
            if mt_site.get('redis_db'):
                continue
            site_domain = mt_site['domain']
            if dry_run:
                Log.info(self, f"[DRY RUN] Would assign a dedicated Redis database to {site_domain}")
                continue
            site_redis_db = MTDatabase.allocate_redis_db(self, site_domain)
            MTFunctions.ensure_redis_databases(self, site_redis_db + 1)
            if MTFunctions.set_wp_config_redis_db(self, mt_site['site_path'], site_redis_db):
                MTDatabase.add_shared_site(self, site_domain, {'redis_db': site_redis_db})
                MTFunctions.purge_site_cache(
                    self, site_domain, mt_site.get('redis_prefix'), site_redis_db)
                Log.info(self, f"Assigned Redis database {site_redis_db} to {site_domain}")

        result = BaselineApplicator.apply_baseline_to_sites(
            self, config,
            dry_run=dry_run, verbose=verbose, prune=prune,
        )
        cron_sync_ok = True
        if not dry_run:
            cron_sync_ok = MTFunctions.sync_wp_cron_entries(self)
            try:
                repair_backup_cron(self.app)
            except Exception as exc:
                Log.warn(self, f"Could not repair backup cron: {exc}")

        summary = result if isinstance(result, dict) else {}
        summary.setdefault('dry_run', dry_run)
        summary_failed = summary.get('failed', 0)
        if not cron_sync_ok:
            summary_failed += 1
        Log.info(
            self,
            f"baseline_applied target=baseline result={'success' if cron_sync_ok else 'failure'} "
            f"attempted={summary.get('attempted', 0)} "
            f"succeeded={summary.get('succeeded', 0)} "
            f"failed={summary_failed}",
        )
        if not cron_sync_ok:
            Log.error(self, self.CRON_SYNC_FAILURE_HINT)
        if summary.get('failed', 0) > 0:
            Log.error(self, f"{summary.get('failed', 0)} site(s) failed "
                            "baseline apply", False)
            self.app.close(1)

    @expose(help="Remove multi-tenancy infrastructure (dangerous)")
    @fleet_operation('remove')
    def remove(self):
        """Remove multi-tenancy infrastructure"""
        pargs = self.app.pargs
        
        if not MTDatabase.is_initialized(self):
            Log.info(self, "Multi-tenancy not initialized")
            return
        
        shared_sites = MTDatabase.get_shared_sites(self)
        
        if shared_sites and not pargs.force:
            Log.error(self, f"Cannot remove: {len(shared_sites)} sites still using shared core")
            Log.error(self, "Remove or convert all shared sites first")
            return
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        Log.warn(self, "This will remove all shared WordPress infrastructure!")
        if not pargs.force:
            confirm = input("Type 'REMOVE' to confirm: ")
            if confirm != 'REMOVE':
                Log.info(self, "Removal cancelled")
                return
        
        try:
            # Remove shared directory
            if os.path.exists(shared_root):
                shutil.rmtree(shared_root)
                Log.info(self, f"Removed {shared_root}")
            
            # Clean up database
            MTDatabase.cleanup(self)
            Log.info(self, "Cleaned up database")
            
            Log.info(self, "✅ Multi-tenancy infrastructure removed")
            
        except Exception as e:
            Log.error(self, f"Removal failed: {str(e)}")

    @expose(help="Manage shared WordPress configuration")
    @fleet_operation('shared_config')
    def shared_config(self):
        """Edit the shared wp-config (the only supported action)."""
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
            Log.error(self, "Run: wo multitenancy init")
            return
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        if self.app.pargs.config_action == 'edit':
            edit_shared_config(self, shared_root)
        else:
            Log.error(self, "Only '--action edit' is supported.")

    # ==========================================
    # health / maintenance
    # ==========================================

    @expose(help="Health snapshot across shared infra, DB, disk, PHP-FPM, nginx, sites")
    def health(self):
        """Run all registered health checks and render a status envelope."""
        pargs = self.app.pargs
        if not MTDatabase.is_initialized(self):
            payload = {
                'status': 'unhealthy',
                'checks': {
                    'initialized': {
                        'status': 'error',
                        'details': {'error': 'multitenancy not initialized'},
                    }
                },
            }
            if getattr(pargs, 'json_output', False):
                print(json.dumps(payload, default=str))
                self.app.close(1)
            else:
                Log.error(self, "❌ Multi-tenancy not initialized")
            return

        site_filter = pargs.site_filter or (
            pargs.site_name if pargs.site_name else None
        )
        checker = HealthChecker(self).register_defaults(site_filter=site_filter)
        result = checker.run_all()
        if getattr(pargs, 'json_output', False):
            print(json.dumps(result, default=str))
        else:
            Log.info(self, render_health_text(result), log=False)
        # Exit codes for automation: 0 healthy, 2 degraded, 1 unhealthy.
        status = result.get('status') if isinstance(result, dict) else None
        if status == 'unhealthy':
            self.app.close(1)
        elif status == 'degraded':
            self.app.close(2)

    @expose(help="Toggle maintenance mode for one site or every shared site")
    def maintenance(self):
        """Enable or disable an nginx 503 maintenance page."""
        return self._maintenance_impl()

    def _maintenance_impl(self):
        pargs = self.app.pargs
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
            return
        if pargs.enable_flag == pargs.disable_flag:
            Log.error(self, "Specify exactly one of --enable or --disable")
            return
        target_domain = pargs.site_filter or (pargs.site_name if pargs.site_name else None)
        if pargs.all_flag and target_domain:
            Log.error(self, "Use either --all or --site=<domain>, not both")
            return
        if not pargs.all_flag and not target_domain:
            Log.error(self, "Specify --site=<domain> or --all")
            return

        config = MTFunctions.load_config(self)
        sites = MTDatabase.get_shared_sites(self)
        if target_domain:
            sites = [s for s in sites if s.get('domain') == target_domain]
            if not sites:
                Log.error(self, f"Site {target_domain} not found in multitenancy tracking")
                return

        message = pargs.message or (
            'We are performing scheduled maintenance and will be back shortly.'
        )
        results = []
        action_event = 'maintenance_enabled' if pargs.enable_flag else 'maintenance_disabled'
        for site in sites:
            domain = site['domain']
            try:
                if pargs.enable_flag:
                    ok = _maintenance_enable(self, domain, message, config)
                else:
                    ok = _maintenance_disable(self, domain)
                status = 'success' if ok else 'failure'
            except Exception as exc:
                status = 'failure'
                Log.warn(self, f"maintenance toggle failed for {domain}: {exc}")
            results.append({'domain': domain, 'status': status})

        # Reload nginx once after batch changes
        reload_ok = MTFunctions.safe_nginx_reload(self, sites[-1]['domain']) if sites else True

        for item in results:
            ico = '✅' if item['status'] == 'success' else '❌'
            Log.info(self, f"{ico} {item['domain']}: {item['status']}")
            Log.info(self, f"{action_event} target={item['domain']} result={item['status']}")
        Log.info(self, f"Nginx reload: {'ok' if reload_ok else 'failed'}")
        if not reload_ok or any(
                item['status'] != 'success' for item in results):
            self.app.close(1)


# ---------------------------------------------------------------------------
# Module-level helpers for the controller above
# ---------------------------------------------------------------------------

def _maintenance_paths(domain):
    site_root = f'/var/www/{domain}'
    return {
        'site_root': site_root,
        'site_htdocs': f'{site_root}/htdocs',
        'nginx_include_dir': f'{site_root}/conf/nginx',
        'nginx_include_file': f'{site_root}/conf/nginx/multitenancy-maintenance.conf',
        'maintenance_html': f'{site_root}/htdocs/maintenance.html',
    }


def _maintenance_gate_exists(domain):
    """Return whether an operator gate already owns this tenant."""
    return os.path.exists(_maintenance_paths(domain)['nginx_include_file'])


def _maintenance_enable(
        controller, domain, message, config, loopback_bypass=False):
    paths = _maintenance_paths(domain)
    retry_after = 600
    try:
        os.makedirs(paths['nginx_include_dir'], exist_ok=True)
    except Exception:
        pass
    # Cement renderer lives on the app, not the controller.
    renderer = getattr(controller, 'app', controller)
    # Render HTML
    html_data = {
        'site_name': domain,
        'message': message,
        'retry_after_human': _humanize_seconds(retry_after),
    }
    try:
        with open(paths['maintenance_html'], 'w') as fh:
            renderer.render(html_data, 'multitenancy-maintenance-page.mustache', out=fh)
    except Exception as exc:
        Log.warn(controller, f"could not write maintenance page for {domain}: {exc}")
        return False
    # Render nginx include
    nginx_data = {
        'domain': domain,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'retry_after_seconds': retry_after,
        'site_htdocs': paths['site_htdocs'],
        'loopback_bypass': loopback_bypass,
    }
    try:
        with open(paths['nginx_include_file'], 'w') as fh:
            renderer.render(nginx_data, 'multitenancy-maintenance.mustache', out=fh)
    except Exception as exc:
        Log.warn(controller, f"could not write maintenance nginx include for {domain}: {exc}")
        return False
    return True


def _maintenance_disable(controller, domain):
    paths = _maintenance_paths(domain)
    ok = True
    for path in (paths['nginx_include_file'], paths['maintenance_html']):
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as exc:
                Log.warn(controller, f"could not remove {path}: {exc}")
                ok = False
    return ok


def _close_update_loopback_bypass(
        controller, domains, message, config):
    """Render update-owned gates closed and reload nginx once."""
    failures = []
    for domain in domains:
        if not _maintenance_enable(
                controller, domain, message, config,
                loopback_bypass=False):
            failures.append(f"could not close loopback bypass for {domain}")
    if domains and not MTFunctions.safe_nginx_reload(
            controller, domains[-1]):
        failures.append("could not reload nginx after closing loopback bypass")
    return failures


def _release_update_maintenance(controller, domains):
    """Clean pre-flip update gates and return any incomplete cleanup."""
    failures = []
    for domain in domains:
        if not _maintenance_disable(controller, domain):
            failures.append(f"could not remove maintenance gate for {domain}")
    if domains and not MTFunctions.safe_nginx_reload(
            controller, domains[-1]):
        failures.append("could not reload nginx after gate cleanup")
    return failures


def _humanize_seconds(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} minutes" if not sec else f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours} hours"


def load(app):
    """Load the multi-tenancy plugin"""
    app.handler.register(WOMultitenancyController)
    from wo.cli.plugins.multitenancy_backup import WOMultitenancyBackupController
    app.handler.register(WOMultitenancyBackupController)
    app.hook.register('post_setup', wo_multitenancy_hook)
