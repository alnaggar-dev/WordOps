"""WordOps Multi-tenancy Plugin
Enables WordPress multi-tenancy with shared core files for efficient management.
"""

import os
import sys
import json
import shutil
import subprocess
from datetime import datetime
from cement.core.controller import CementBaseController, expose
from wo.cli.plugins.site_functions import (
    check_domain_exists, setupdatabase, setwebrootpermissions,
    site_package_check, sitebackup, pre_run_checks
)
from wo.cli.plugins.sitedb import (
    addNewSite, deleteSiteInfo, getAllsites, getSiteInfo, updateSiteInfo
)
from wo.core.domainvalidate import WODomain
from wo.core.fileutils import WOFileUtils
from wo.core.git import WOGit
from wo.core.logging import Log
from wo.core.services import WOService
from wo.core.shellexec import WOShellExec, CommandExecutionError
from wo.core.sslutils import SSL
from wo.core.variables import WOVar
from wo.core.acme import WOAcme
from wo.cli.plugins.multitenancy_functions import (
    MTFunctions, SharedInfrastructure, ReleaseManager, BaselineApplicator, SharedConfig
)
from wo.cli.plugins.multitenancy_db import MTDatabase


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
                dict(help='WordPress admin username', default='admin')),
            (['plugin_slug'], dict(help='Plugin or theme slug', nargs='?')),
            (['theme_slug'], dict(help='Theme slug', nargs='?')),
            (['--apply-now'], dict(help='Apply changes immediately', action='store_true')),
            (['--set-default'], dict(help='Set as default theme', action='store_true')),
            # Phase 3: GitHub and URL download support
            (['--github'], dict(help='GitHub repository (user/repo)', dest='github')),
            (['--branch'], dict(help='GitHub branch name', dest='branch')),
            (['--tag'], dict(help='GitHub tag/release name', dest='tag')),
            (['--url'], dict(help='Direct download URL', dest='url')),
            # Phase 3: Rollback support
            (['--to-version'], dict(help='Baseline version to rollback to', type=int, dest='to_version')),
            (['--to-commit'], dict(help='Git commit hash to rollback to', dest='to_commit')),
        ]
        usage = "wo multitenancy <command> [options]"

    @expose(hide=True)
    def default(self):
        """Default command"""
        self.app.args.print_help()

    @expose(help="Initialize WordPress multi-tenancy shared infrastructure")
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
        
        # Create shared infrastructure
        infra = SharedInfrastructure(self, shared_root)
        
        try:
            # Create directory structure
            Log.info(self, "Creating shared directory structure...")
            infra.create_directory_structure()
            
            # Download WordPress core
            Log.info(self, "Downloading WordPress core...")
            release_name = infra.download_wordpress_core()
            
            # Seed plugins and themes
            Log.info(self, "Seeding plugins and themes...")
            infra.seed_plugins_and_themes(config)
            
            # Create baseline configuration
            Log.info(self, "Creating baseline configuration...")
            infra.create_baseline_config(config)
            
            # Create MU-plugin
            Log.info(self, "Creating MU-plugin for baseline enforcement...")
            infra.create_mu_plugin()
            
            # Phase 1: Create shared configuration file
            Log.info(self, "Creating shared configuration file...")
            if SharedConfig.create_shared_config_file(self, shared_root):
                Log.info(self, "   ✅ Shared config file created")
            else:
                Log.warn(self, "   ⚠️  Could not create shared config file")
            
            # Phase 1: Initialize dedicated Git repository for shared config
            Log.info(self, "Setting up git tracking for shared config...")
            config_dir = f"{shared_root}/config"
            if SharedConfig.initialize_config_git(self, shared_root):
                Log.info(self, "   ✅ Shared config Git tracking initialized")
            else:
                Log.warn(self, "   Git tracking not available (git not installed)")
            
            # Initialize git tracking
            Log.info(self, "Setting up git tracking for baseline...")
            if infra.initialize_git_tracking():
                Log.info(self, "   ✅ Git tracking initialized")
            else:
                Log.warn(self, "   Git tracking not available (git not installed)")
            
            # Make release current
            Log.info(self, "Activating release...")
            infra.switch_release(release_name)
            
            # Set permissions
            Log.info(self, "Setting permissions...")
            infra.set_permissions()

            # Auto-cleanup old releases
            Log.info(self, "Cleaning up old releases...")
            release_manager = ReleaseManager(self, shared_root)
            keep_releases = int(config.get('keep_releases', 3))
            release_manager.cleanup_old_releases(keep_releases)

            # Update database
            MTDatabase.save_config(self, {
                'shared_root': shared_root,
                'current_release': release_name,
                'baseline_version': 1
            })
            
            Log.info(self, "✅ Multi-tenancy infrastructure initialized successfully!")
            Log.info(self, f"   Shared root: {shared_root}")
            Log.info(self, f"   Current release: {release_name}")
            
        except Exception as e:
            Log.error(self, f"Failed to initialize multi-tenancy: {str(e)}")

    @expose(help="Create a WordPress site using shared core")
    def create(self):
        """Create a new site with shared WordPress core"""
        pargs = self.app.pargs
        
        if not pargs.site_name:
            try:
                while not pargs.site_name:
                    pargs.site_name = input('Enter site name : ').strip()
            except IOError:
                Log.error(self, 'Could not input site name')
        
        # Validate domain
        wo_domain = WODomain.validate(self, pargs.site_name)

        # Enhanced argument validation
        if hasattr(pargs, 'letsencrypt') and pargs.letsencrypt:
            # Check if the argument contains em dash instead of double hyphen
            site_name_arg = getattr(pargs, 'site_name', '')
            if '—' in ' '.join(sys.argv):  # Check for em dash in command line
                Log.error(self, "Invalid argument syntax detected!")
                Log.error(self, "Did you use '—le' (em dash) instead of '--le' (double hyphen)?")
                Log.error(self, "Correct syntax: wo multitenancy create example.com --php83 --wpfc --le")
                return

        # Check if site exists
        if check_domain_exists(self, wo_domain):
            Log.error(self, f"Site {wo_domain} already exists")
        
        # Check if multi-tenancy is initialized
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized. Run: wo multitenancy init")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        # Determine PHP version
        php_version = MTFunctions.get_php_version(self, pargs)
        
        # Determine cache type
        cache_type = MTFunctions.get_cache_type(self, pargs)
        
        Log.info(self, f"Creating shared WordPress site: {wo_domain}")
        Log.info(self, f"   PHP version: {php_version}")
        Log.info(self, f"   Cache type: {cache_type}")
        
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
                Log.error(self, "Failed to create database")
            
            db_name = db_data['wo_db_name']
            db_user = db_data['wo_db_user']
            db_pass = db_data['wo_db_pass']
            db_host = db_data['wo_db_host']
            
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
            Log.debug(self, f"Redis prefix for {wo_domain}: {redis_prefix}")
            
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
                redis_prefix=redis_prefix  # Pass explicit prefix for consistency
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
            Log.info(self, "Applying baseline configuration...")
            MTFunctions.apply_baseline(self, wo_domain, site_htdocs, config)
            
            # Set permissions
            Log.info(self, "Setting permissions...")
            setwebrootpermissions(self, site_htdocs)
            
            # Test nginx configuration before enabling site
            if not MTFunctions.validate_nginx_config(self, log_errors=True):
                Log.error(self, "Nginx configuration validation failed before enabling site")
                raise Exception("Invalid nginx configuration")

            # Enable site in nginx first (without SSL)
            WOFileUtils.create_symlink(self, [
                f"/etc/nginx/sites-available/{wo_domain}",
                f"/etc/nginx/sites-enabled/{wo_domain}"
            ])

            # Test nginx configuration after enabling site
            if not MTFunctions.validate_nginx_config(self, log_errors=True):
                Log.error(self, "Nginx configuration validation failed after enabling site")
                # Remove the symlink we just created
                if os.path.exists(f"/etc/nginx/sites-enabled/{wo_domain}"):
                    os.remove(f"/etc/nginx/sites-enabled/{wo_domain}")
                raise Exception("Nginx configuration invalid after enabling site")

            # Reload nginx using our enhanced function
            try:
                if not MTFunctions.safe_nginx_reload(self, wo_domain):
                    Log.error(self, "Failed to reload nginx with enhanced diagnostics")
                    raise Exception("Nginx reload failed")
                else:
                    Log.debug(self, "Nginx reloaded successfully")
            except Exception as reload_error:
                Log.error(self, f"Nginx reload error: {reload_error}")
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
                'redis_prefix': redis_prefix  # Phase 2: Store Redis prefix with site data
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
            
            # Clear cache
            MTFunctions.clear_cache(self, wo_domain, cache_type)
            
            # Git commit
            WOGit.add(self, ["/etc/nginx"], 
                     msg=f"Created shared WordPress site: {wo_domain}")
            
            # Display success message
            admin_pass = MTFunctions.get_admin_password(self, wo_domain)
            site_url = f"https://{wo_domain}" if pargs.letsencrypt else f"http://{wo_domain}"
            
            Log.info(self, "")
            Log.info(self, "🎉 WordPress site created successfully!")
            Log.info(self, f"   URL: {site_url}")
            Log.info(self, f"   Admin URL: {site_url}/wp-admin")
            Log.info(self, f"   Admin user: {pargs.admin_user}")
            Log.info(self, f"   Admin password: {admin_pass}")
            Log.info(self, "")
            
        except Exception as e:
            Log.error(self, f"Failed to create site: {str(e)}")
            # Cleanup failed site creation
            try:
                Log.info(self, "Attempting cleanup of partially created site...")
                MTFunctions.cleanup_failed_site(self, wo_domain, site_root)
                Log.info(self, "Cleanup completed")
            except Exception as cleanup_error:
                Log.warn(self, f"Cleanup failed: {cleanup_error}")
            return False

    @expose(help="Update WordPress core and plugins for all shared sites")
    def update(self):
        """Update shared WordPress infrastructure"""
        pargs = self.app.pargs
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        Log.info(self, "Updating WordPress multi-tenancy infrastructure...")
        
        # Get list of shared sites
        shared_sites = MTDatabase.get_shared_sites(self)
        if not shared_sites:
            Log.info(self, "No shared sites found")
            return
        
        Log.info(self, f"Found {len(shared_sites)} shared sites")
        
        # Create new release
        infra = SharedInfrastructure(self, shared_root)
        release_manager = ReleaseManager(self, shared_root)
        
        try:
            # Create new release
            Log.info(self, "Creating new WordPress release...")
            new_release = infra.download_wordpress_core()
            
            # Update plugins and themes
            Log.info(self, "Updating plugins and themes...")
            infra.update_plugins_and_themes(config)
            
            # Test with canary site if available
            if shared_sites and not pargs.force:
                canary = shared_sites[0]
                Log.info(self, f"Testing with canary site: {canary['domain']}")
                if not MTFunctions.test_site(self, canary['domain']):
                    Log.warn(self, "Canary test failed. Use --force to proceed anyway.")
                    return
            
            # Backup current state
            Log.info(self, "Backing up current release...")
            release_manager.backup_current()
            
            # Switch to new release
            Log.info(self, "Switching to new release...")
            infra.switch_release(new_release)
            
            # Clear all caches globally (fast - ~2 seconds for any number of sites)
            MTFunctions.clear_all_caches(self)
            
            # Update database
            MTDatabase.update_release(self, new_release)
            
            # Cleanup old releases
            Log.info(self, "Cleaning up old releases...")
            release_manager.cleanup_old_releases(int(config.get('keep_releases', 3)))
            
            # Update baseline version to trigger reapplication
            MTDatabase.increment_baseline_version(self)
            
            Log.info(self, "✅ Update completed successfully!")
            Log.info(self, f"   New release: {new_release}")
            Log.info(self, f"   Updated {len(shared_sites)} sites")
            
        except Exception as e:
            Log.error(self, f"Update failed: {str(e)}")
            Log.info(self, "Run 'wo multitenancy rollback' to revert")

    @expose(help="Rollback to previous WordPress release")
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
            current_release = MTDatabase.get_current_release(self)
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
            
            # Update database
            MTDatabase.update_release(self, previous_release)
            
            Log.info(self, "✅ Rollback completed successfully!")
            Log.info(self, f"   Now running: {previous_release}")
            
        except Exception as e:
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
        baseline_version = MTDatabase.get_baseline_version(self)
        
        Log.info(self, "")
        Log.info(self, "=== WordPress Multi-tenancy Status ===")
        Log.info(self, "")
        
        # Infrastructure status
        Log.info(self, "INFRASTRUCTURE:")
        Log.info(self, f"  Shared root: {shared_root}")
        Log.info(self, f"  Current release: {current_release}")
        Log.info(self, f"  Baseline version: {baseline_version}")
        
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
        if os.path.exists(f"{shared_root}/config/baseline.json"):
            with open(f"{shared_root}/config/baseline.json", 'r') as f:
                baseline = json.load(f)
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
        Log.info(self, "-" * 80)
        Log.info(self, f"{'Domain':<30} {'PHP':<8} {'Cache':<12} {'SSL':<5} {'Status':<10}")
        Log.info(self, "-" * 80)
        
        for site in shared_sites:
            domain = site['domain']
            php = site.get('php_version', 'unknown')
            cache = site.get('cache_type', 'none')
            ssl = "Yes" if site.get('is_ssl', False) else "No"
            enabled = "Enabled" if site.get('is_enabled', True) else "Disabled"
            
            Log.info(self, f"{domain:<30} {php:<8} {cache:<12} {ssl:<5} {enabled:<10}")
        
        Log.info(self, "-" * 80)
        Log.info(self, f"Total: {len(shared_sites)} sites")
        Log.info(self, "")

    @expose(help="Manage baseline configuration for shared sites")
    def baseline(self):
        """Manage baseline plugins and themes"""
        pargs = self.app.pargs
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"
        
        # Show current baseline
        if os.path.exists(baseline_file):
            with open(baseline_file, 'r') as f:
                baseline = json.load(f)
            
            Log.info(self, "Current Baseline Configuration:")
            Log.info(self, f"  Version: {baseline.get('version', 1)}")
            Log.info(self, f"  Plugins: {', '.join(baseline.get('plugins', []))}")
            Log.info(self, f"  Theme: {baseline.get('theme', 'unknown')}")
            Log.info(self, "")
            Log.info(self, "To update baseline:")
            Log.info(self, "  1. Edit: /etc/wo/plugins.d/multitenancy.conf")
            Log.info(self, "  2. Run: wo multitenancy baseline --update")
        else:
            Log.error(self, "Baseline configuration not found")

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
        
        baseline_version = baseline.get('version', 0)
        Log.info(self, f"   Current version: {baseline_version}")
        Log.info(self, "")
        
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
        if theme_slug:
            Log.info(self, "Theme Validation:")
            theme_dir = f"{shared_root}/wp-content/themes/{theme_slug}"
            
            if os.path.exists(theme_dir):
                Log.info(self, f"   ✅ {theme_slug}")
            else:
                Log.warn(self, f"   ❌ {theme_slug} - NOT FOUND ON DISK")
            
            Log.info(self, "")
        
        # 4. Check site baseline versions
        from wo.core.database import db_session
        from wo.cli.plugins.multitenancy_db import MultitenancySite
        
        session = db_session
        sites = session.query(MultitenancySite).filter_by(is_enabled=True).all()
        production_sites = [s for s in sites if not getattr(s, 'is_staging', False)]
        
        outdated_sites = []
        for site in production_sites:
            site_version = getattr(site, 'baseline_version', 0)
            if site_version < baseline_version:
                outdated_sites.append((site.domain, site_version))
        
        if outdated_sites:
            Log.warn(self, f"⚠️  {len(outdated_sites)} site(s) behind baseline:")
            for domain, version in outdated_sites[:10]:  # Show first 10
                Log.warn(self, f"   - {domain} (version {version}, should be {baseline_version})")
            
            if len(outdated_sites) > 10:
                Log.warn(self, f"   ... and {len(outdated_sites) - 10} more")
            
            Log.info(self, "")
            Log.info(self, "   Run: wo multitenancy baseline apply")
        else:
            Log.info(self, f"✅ All {len(production_sites)} production sites up to date")
            Log.info(self, "")
        
        # 5. Check quarantined sites
        quarantined = MTDatabase.get_quarantined_sites(self)
        
        if quarantined:
            Log.warn(self, f"⚠️  {len(quarantined)} quarantined site(s):")
            for site in quarantined:
                Log.warn(self, f"   - {site['domain']}")
                Log.warn(self, f"     Reason: {site['quarantine_reason']}")
                if site.get('quarantine_date'):
                    Log.warn(self, f"     Date: {site['quarantine_date']}")
            
            Log.info(self, "")
            Log.info(self, "   Fix issues manually, then remove quarantine:")
            Log.info(self, "   wo multitenancy baseline unquarantine <domain>")
            Log.info(self, "")
        
        # 6. Summary
        Log.info(self, "=" * 60)
        
        if missing_plugins:
            Log.error(self, f"❌ VALIDATION FAILED: {len(missing_plugins)} plugin(s) missing from disk")
            Log.error(self, "   This will cause activation failures!")
            Log.error(self, "   Fix: Install missing plugins or remove from baseline")
        elif outdated_sites or quarantined:
            Log.warn(self, "⚠️  ATTENTION NEEDED: Some sites require updates")
        else:
            Log.info(self, "✅ VALIDATION PASSED: Baseline is healthy")
        
        Log.info(self, "=" * 60)




    @expose(help="Create staging site for testing baseline changes")
    def staging(self):
        """Create or manage staging site"""
        pargs = self.app.pargs
        domain = pargs.site_name
        
        if not domain:
            Log.error(self, "Usage: wo multitenancy staging <domain>")
            return
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
            return
        
        # Check if staging site already exists
        existing = MTDatabase.get_staging_site(self)
        if existing:
            Log.error(self, f"Staging site already exists: {existing['domain']}")
            Log.error(self, "Delete it first: wo site delete {existing['domain']}")
            return
        
        Log.info(self, f"Creating staging site: {domain}")
        Log.info(self, "This will create a regular site and mark it as staging...")
        Log.info(self, "")
        
        # Create site using existing create command logic
        # Simply call the create method with shared flag
        original_site_name = pargs.site_name
        pargs.shared = True
        
        try:
            self.create()
            
            # Mark as staging in database
            from wo.core.database import db_session
            from wo.cli.plugins.multitenancy_db import MultitenancySite
            
            session = db_session
            site = session.query(MultitenancySite).filter_by(domain=domain).first()
            if site:
                site.is_staging = True
                site.updated_at = datetime.now()
                session.commit()
                
                Log.info(self, "")
                Log.info(self, "✅ Marked as staging site")
                Log.info(self, "")
                Log.info(self, "Use this site to test baseline changes before production.")
                Log.info(self, "Test with: wo multitenancy baseline add-plugin <plugin> --apply-now")
            else:
                Log.warn(self, "Site created but could not mark as staging")
                
        except Exception as e:
            Log.error(self, f"Failed to create staging site: {e}")
        finally:
            pargs.site_name = original_site_name
    @expose(help="Delete a multitenancy site and its tracking")
    def delete(self):
        """Delete a site from multitenancy system"""
        pargs = self.app.pargs
        domain = pargs.site_name
        
        if not domain:
            Log.error(self, "Usage: wo multitenancy delete <domain>")
            return
        
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
        
        is_staging = getattr(site, 'is_staging', False)
        
        # Confirm deletion
        if not pargs.force:
            site_type = "STAGING" if is_staging else "PRODUCTION"
            Log.warn(self, f"This will delete {site_type} site: {domain}")
            confirm = input("Continue? [y/N]: ").strip().lower()
            if confirm != 'y':
                Log.info(self, "Aborted")
                return
        
        Log.info(self, f"Deleting site: {domain}")
        
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
                return
            
            Log.info(self, "✅ Site files and database deleted")
            
        except Exception as e:
            Log.error(self, f"Error deleting site: {e}")
            return
        
        # Remove from multitenancy tracking
        try:
            session.delete(site)
            session.commit()
            Log.info(self, "✅ Removed from multitenancy tracking")
            
            if is_staging:
                Log.info(self, "")
                Log.info(self, "Staging site deleted. You can create a new one with:")
                Log.info(self, "  wo multitenancy staging <domain>")
            
        except Exception as e:
            Log.error(self, f"Error removing from tracking: {e}")
            Log.warn(self, "Site deleted but tracking entry remains")
            Log.warn(self, f"Manually clean up with: sqlite3 /var/lib/wo/dbase.db \"DELETE FROM multitenancy_sites WHERE domain = '{domain}';\"")


    @expose(help="Add plugin to baseline")
    def add_plugin(self):
        """
        Add a plugin to the baseline configuration
        
        This command downloads a plugin from WordPress.org, GitHub, or a direct URL,
        adds it to the baseline configuration, and optionally applies it to all sites.
        
        Usage:
            wo multitenancy baseline add-plugin <slug>                    # From WordPress.org
            wo multitenancy baseline add-plugin <slug> --github=user/repo # From GitHub
            wo multitenancy baseline add-plugin <slug> --url=https://...  # From direct URL
            wo multitenancy baseline add-plugin <slug> --apply-now        # Apply immediately
        """
        pargs = self.app.pargs
        plugin_slug = pargs.plugin_slug or pargs.site_name  # Use site_name as positional arg
        apply_now = pargs.apply_now
        
        # Phase 3: Get source-specific arguments
        github_repo = pargs.github
        branch = pargs.branch
        tag = pargs.tag
        url = pargs.url
        
        # Validate arguments
        if not plugin_slug:
            Log.error(self, "Plugin slug is required")
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized. Run: wo multitenancy init")
        
        # Validate: only one source method allowed
        source_count = sum([bool(github_repo), bool(url)])
        if source_count > 1:
            Log.error(self, "Specify only one source: --github OR --url (default is WordPress.org)")
        
        # Validate: branch/tag only valid with GitHub
        if (branch or tag) and not github_repo:
            Log.error(self, "--branch and --tag can only be used with --github")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        # Determine and display source
        if github_repo:
            source_info = f"from GitHub: {github_repo}"
            if tag:
                source_info += f" (tag: {tag})"
            elif branch:
                source_info += f" (branch: {branch})"
        elif url:
            source_info = f"from URL: {url}"
        else:
            source_info = "from WordPress.org"
        
        Log.info(self, f"Adding plugin: {plugin_slug} {source_info}")
        
        # Download plugin using appropriate method
        infra = SharedInfrastructure(self, shared_root)
        
        if github_repo:
            # Download from GitHub
            success = infra.download_plugin_from_github(
                github_repo, 
                plugin_slug, 
                branch=branch, 
                tag=tag
            )
        elif url:
            # Download from direct URL
            success = infra.download_plugin_from_url(url, plugin_slug)
        else:
            # Download from WordPress.org (default)
            infra.download_plugin(plugin_slug)
            success = True  # download_plugin doesn't return bool, check directory instead
        
        # Verify plugin was downloaded
        plugin_dir = f"{shared_root}/wp-content/plugins/{plugin_slug}"
        if not os.path.exists(plugin_dir):
            Log.error(self, f"Failed to download plugin: {plugin_slug}")
            Log.error(self, "")
            Log.error(self, "Possible causes:")
            Log.error(self, "  - Plugin doesn't exist at the source")
            Log.error(self, "  - Network connectivity issue")
            Log.error(self, "  - Invalid GitHub repo or URL")
            Log.error(self, "  - Disk space full")
            return
        
        Log.info(self, f"✅ Downloaded {plugin_slug}")
        
        # Update baseline.json
        baseline_file = f"{shared_root}/config/baseline.json"
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)
        
        # Check if already in baseline
        if plugin_slug in baseline.get('plugins', []):
            Log.error(self, f"Plugin {plugin_slug} already in baseline")
        
        # Increment version and add plugin
        old_version = baseline.get('version', 1)
        new_version = old_version + 1
        
        baseline['version'] = new_version
        baseline['generated'] = datetime.now().isoformat()
        baseline['plugins'].append(plugin_slug)
        
        # Write updated baseline
        with open(baseline_file, 'w') as f:
            json.dump(baseline, f, indent=2)
        
        Log.info(self, f"✅ Updated baseline.json (v{old_version} → v{new_version})")
        
        # Git commit
        commit_msg = f"Baseline v{new_version}: Added plugin {plugin_slug}"
        if infra.git_commit_baseline(commit_msg):
            Log.info(self, f"✅ Git: {commit_msg}")
        
        # Apply to sites if requested
        if apply_now:
            Log.info(self, "")
            Log.info(self, "Applying to all sites...")
            BaselineApplicator.apply_baseline_to_sites(self, config, new_version)
        else:
            Log.info(self, "")
            Log.info(self, "Plugin added to baseline.")
            Log.info(self, "Sites will pick up changes on next admin visit,")
            Log.info(self, "or run: wo multitenancy baseline apply")


    @expose(help="Add theme to baseline")
    def add_theme(self):
        """
        Add a theme to the baseline configuration
        
        This command downloads a theme from WordPress.org, GitHub, or a direct URL,
        adds it to the baseline configuration, and optionally sets it as the default theme.
        
        Usage:
            wo multitenancy baseline add-theme <slug>                    # From WordPress.org
            wo multitenancy baseline add-theme <slug> --github=user/repo # From GitHub
            wo multitenancy baseline add-theme <slug> --url=https://...  # From direct URL
            wo multitenancy baseline add-theme <slug> --set-default      # Set as default
            wo multitenancy baseline add-theme <slug> --apply-now        # Apply immediately
        """
        pargs = self.app.pargs
        theme_slug = pargs.theme_slug or pargs.site_name  # Use site_name as positional arg
        set_default = pargs.set_default
        apply_now = pargs.apply_now
        
        # Phase 3: Get source-specific arguments
        github_repo = pargs.github
        branch = pargs.branch
        tag = pargs.tag
        url = pargs.url
        
        # Validate arguments
        if not theme_slug:
            Log.error(self, "Theme slug is required")
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        # Validate: only one source method allowed
        source_count = sum([bool(github_repo), bool(url)])
        if source_count > 1:
            Log.error(self, "Specify only one source: --github OR --url (default is WordPress.org)")
        
        # Validate: branch/tag only valid with GitHub
        if (branch or tag) and not github_repo:
            Log.error(self, "--branch and --tag can only be used with --github")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        # Determine and display source
        if github_repo:
            source_info = f"from GitHub: {github_repo}"
            if tag:
                source_info += f" (tag: {tag})"
            elif branch:
                source_info += f" (branch: {branch})"
        elif url:
            source_info = f"from URL: {url}"
        else:
            source_info = "from WordPress.org"
        
        Log.info(self, f"Adding theme: {theme_slug} {source_info}")
        
        # Download theme using appropriate method
        infra = SharedInfrastructure(self, shared_root)
        
        if github_repo:
            # Download from GitHub
            success = infra.download_theme_from_github(
                github_repo, 
                theme_slug, 
                branch=branch, 
                tag=tag
            )
        elif url:
            # Download from direct URL
            success = infra.download_theme_from_url(url, theme_slug)
        else:
            # Download from WordPress.org (default)
            infra.download_theme(theme_slug)
            success = True  # download_theme doesn't return bool, check directory instead
        
        # Verify theme was downloaded
        theme_dir = f"{shared_root}/wp-content/themes/{theme_slug}"
        if not os.path.exists(theme_dir):
            Log.error(self, f"Failed to download theme: {theme_slug}")
            Log.error(self, "")
            Log.error(self, "Possible causes:")
            Log.error(self, "  - Theme doesn't exist at the source")
            Log.error(self, "  - Network connectivity issue")
            Log.error(self, "  - Invalid GitHub repo or URL")
            Log.error(self, "  - Disk space full")
            return
        
        Log.info(self, f"✅ Downloaded {theme_slug}")
        
        # Update baseline.json
        baseline_file = f"{shared_root}/config/baseline.json"
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)
        
        old_version = baseline.get('version', 1)
        new_version = old_version + 1
        
        baseline['version'] = new_version
        baseline['generated'] = datetime.now().isoformat()
        
        if set_default:
            old_theme = baseline.get('theme', 'none')
            baseline['theme'] = theme_slug
            Log.info(self, f"✅ Set as default theme (was: {old_theme})")
        
        # Write updated baseline
        with open(baseline_file, 'w') as f:
            json.dump(baseline, f, indent=2)
        
        Log.info(self, f"✅ Updated baseline.json (v{old_version} → v{new_version})")
        
        # Git commit
        if set_default:
            commit_msg = f"Baseline v{new_version}: Set default theme to {theme_slug}"
        else:
            commit_msg = f"Baseline v{new_version}: Added theme {theme_slug}"
        
        if infra.git_commit_baseline(commit_msg):
            Log.info(self, f"✅ Git: {commit_msg}")
        
        # Apply to sites if requested
        if apply_now and set_default:
            Log.info(self, "")
            Log.info(self, "Applying to all sites...")
            BaselineApplicator.apply_baseline_to_sites(self, config, new_version)
        else:
            Log.info(self, "")
            Log.info(self, "Theme added to baseline.")
            if set_default:
                Log.info(self, "Sites will use this theme on next admin visit,")
                Log.info(self, "or run: wo multitenancy baseline apply")


    @expose(help="Remove plugin from baseline")
    def remove_plugin(self):
        """Remove a plugin from the baseline"""
        pargs = self.app.pargs
        plugin_slug = pargs.plugin_slug or pargs.site_name  # Use site_name as positional arg
        apply_now = pargs.apply_now
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        Log.info(self, f"Removing plugin: {plugin_slug}")
        
        # Update baseline.json
        baseline_file = f"{shared_root}/config/baseline.json"
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)
        
        # Check if plugin is in baseline
        if plugin_slug not in baseline.get('plugins', []):
            Log.error(self, f"Plugin {plugin_slug} not in baseline")
        
        old_version = baseline.get('version', 1)
        new_version = old_version + 1
        
        baseline['version'] = new_version
        baseline['generated'] = datetime.now().isoformat()
        baseline['plugins'].remove(plugin_slug)
        
        # Write updated baseline
        with open(baseline_file, 'w') as f:
            json.dump(baseline, f, indent=2)
        
        Log.info(self, f"✅ Updated baseline.json (v{old_version} → v{new_version})")
        
        # Git commit
        infra = SharedInfrastructure(self, shared_root)
        commit_msg = f"Baseline v{new_version}: Removed plugin {plugin_slug}"
        if infra.git_commit_baseline(commit_msg):
            Log.info(self, f"✅ Git: {commit_msg}")
        
        Log.info(self, "")
        Log.info(self, f"Plugin {plugin_slug} removed from baseline.")
        Log.info(self, "Note: Plugin files kept for potential rollback.")
        
        if apply_now:
            Log.warn(self, "Immediate deactivation not yet implemented in Phase 1")
            Log.info(self, "Sites will stop using plugin on next baseline sync")

    @expose(help="Remove theme from baseline")

    @expose(help="Update plugin from its original source")
    def update_plugin(self):
        """
        Update a plugin from its original source (WordPress.org, GitHub, or URL)
        
        This command re-downloads a plugin from the source specified in baseline.json,
        allowing you to get the latest version while maintaining source information.
        
        Usage:
            wo multitenancy baseline update-plugin <slug>
        """
        pargs = self.app.pargs
        plugin_slug = pargs.plugin_slug or pargs.site_name  # Use site_name as positional arg
        
        if not plugin_slug:
            Log.error(self, "Plugin slug is required")
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized. Run: wo multitenancy init")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        Log.info(self, f"Updating plugin: {plugin_slug}")
        
        # Update plugin using SharedInfrastructure method
        infra = SharedInfrastructure(self, shared_root)
        success = infra.update_plugin(plugin_slug)
        
        if success:
            Log.info(self, "")
            Log.info(self, f"✅ Plugin {plugin_slug} updated successfully")
            Log.info(self, "")
            Log.info(self, "Next steps:")
            Log.info(self, "  • Test changes in staging site")
            Log.info(self, f"  • Apply to all sites: wo multitenancy baseline apply")
        else:
            Log.error(self, f"Failed to update plugin {plugin_slug}")
            Log.error(self, "Check the error messages above for details")

    @expose(help="Update theme from its original source")
    def update_theme(self):
        """
        Update the theme from its original source (WordPress.org, GitHub, or URL)
        
        This command re-downloads the theme from the source specified in baseline.json.
        
        Usage:
            wo multitenancy baseline update-theme
        """
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized. Run: wo multitenancy init")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        # Get theme name from baseline
        baseline_file = f"{shared_root}/config/baseline.json"
        try:
            with open(baseline_file, 'r') as f:
                baseline = json.load(f)
            theme_name = baseline.get('theme')
            if not theme_name:
                Log.error(self, "No theme configured in baseline")
        except FileNotFoundError:
            Log.error(self, "Baseline configuration not found")
        except json.JSONDecodeError:
            Log.error(self, "Failed to parse baseline.json")
        
        Log.info(self, f"Updating theme: {theme_name}")
        
        # Update theme using SharedInfrastructure method
        infra = SharedInfrastructure(self, shared_root)
        success = infra.update_theme()
        
        if success:
            Log.info(self, "")
            Log.info(self, f"✅ Theme {theme_name} updated successfully")
            Log.info(self, "")
            Log.info(self, "Next steps:")
            Log.info(self, "  • Test changes in staging site")
            Log.info(self, f"  • Apply to all sites: wo multitenancy baseline apply")
        else:
            Log.error(self, f"Failed to update theme {theme_name}")
            Log.error(self, "Check the error messages above for details")

    def remove_theme(self):
        """Remove a theme from baseline default"""
        pargs = self.app.pargs
        theme_slug = pargs.theme_slug or pargs.site_name  # Use site_name as positional arg
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        # Update baseline.json
        baseline_file = f"{shared_root}/config/baseline.json"
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)
        
        # Check if this is the current default theme
        current_theme = baseline.get('theme')
        if current_theme != theme_slug:
            Log.error(self, f"Theme {theme_slug} is not the current default theme (current: {current_theme})")
        
        Log.warn(self, f"Removing default theme: {theme_slug}")
        Log.warn(self, "You should set a new default theme first!")
        Log.error(self, "Use: wo multitenancy baseline add-theme <new-theme> --set-default")

    @expose(help="Apply current baseline to all sites")
    def apply(self):
        """Apply baseline to all sites immediately"""
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"
        
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)
        
        baseline_version = baseline.get('version', 1)
        
        Log.info(self, f"Applying baseline v{baseline_version} to all sites...")
        BaselineApplicator.apply_baseline_to_sites(self, config, baseline_version)

    @expose(help="Remove multi-tenancy infrastructure (dangerous)")
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


    # ==========================================
    # PHASE 3: Additional Baseline Commands
    # ==========================================
    
    @expose(help="Set default theme for all sites")
    def set_theme(self):
        """
        Set the default theme in baseline configuration
        
        This standalone command sets a theme as the default for all sites.
        The theme must already exist in the shared themes directory.
        
        Usage:
            wo multitenancy baseline set-theme <slug>
            wo multitenancy baseline set-theme <slug> --apply-now
        """
        pargs = self.app.pargs
        theme_slug = pargs.theme_slug or pargs.site_name
        apply_now = pargs.apply_now
        
        if not theme_slug:
            Log.error(self, "Theme slug is required")
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        # Validate theme exists on disk
        theme_dir = f"{shared_root}/wp-content/themes/{theme_slug}"
        if not os.path.exists(theme_dir):
            Log.error(self, f"Theme not found: {theme_slug}")
            Log.error(self, f"Add it first with: wo multitenancy baseline add-theme {theme_slug}")
        
        Log.info(self, f"Setting default theme: {theme_slug}")
        
        # Update baseline.json
        baseline_file = f"{shared_root}/config/baseline.json"
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)
        
        old_theme = baseline.get('theme', 'none')
        old_version = baseline.get('version', 1)
        new_version = old_version + 1
        
        # Update baseline with new theme
        baseline['version'] = new_version
        baseline['theme'] = theme_slug
        baseline['generated'] = datetime.now().isoformat()
        
        # Write updated baseline
        with open(baseline_file, 'w') as f:
            json.dump(baseline, f, indent=2)
        
        Log.info(self, f"✅ Updated baseline.json (v{old_version} → v{new_version})")
        Log.info(self, f"   Theme: {old_theme} → {theme_slug}")
        
        # Git commit
        infra = SharedInfrastructure(self, shared_root)
        commit_msg = f"Baseline v{new_version}: Set default theme to {theme_slug}"
        if infra.git_commit_baseline(commit_msg):
            Log.info(self, f"✅ Git: {commit_msg}")
        
        # Apply to sites if requested
        if apply_now:
            Log.info(self, "")
            Log.info(self, "Applying to all sites...")
            BaselineApplicator.apply_baseline_to_sites(self, config, new_version)
        else:
            Log.info(self, "")
            Log.info(self, "Theme set in baseline.")
            Log.info(self, "Sites will use this theme on next admin visit,")
            Log.info(self, "or run: wo multitenancy baseline apply")
    
    @expose(help="Show baseline change history")
    def history(self):
        """
        Display git log of baseline configuration changes
        
        This command shows the version history of baseline.json,
        including what was changed and when.
        
        Usage:
            wo multitenancy baseline history
        """
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        git_dir = f"{shared_root}/.git"
        if not os.path.exists(git_dir):
            Log.error(self, "Git tracking not initialized")
            Log.error(self, "History is only available for systems initialized with git support")
        
        Log.info(self, "Baseline Change History:")
        Log.info(self, "=" * 60)
        
        try:
            # Get git log for baseline.json (last 20 commits)
            result = subprocess.run(
                ['git', 'log', '--oneline', '--decorate', '-20', 
                 'config/baseline.json'],
                cwd=shared_root,
                capture_output=True,
                text=True,
                check=True
            )
            
            if result.stdout:
                # Display the git log output
                Log.info(self, result.stdout.strip())
            else:
                Log.info(self, "No history yet")
            
            Log.info(self, "=" * 60)
            Log.info(self, "")
            Log.info(self, "View full history: cd /var/www/shared && git log config/baseline.json")
            Log.info(self, "View specific commit: git show <commit-hash>")
            Log.info(self, "Compare versions: git diff <commit1> <commit2> config/baseline.json")
            
        except subprocess.CalledProcessError as e:
            Log.error(self, f"Failed to get history: {e}")
        except Exception as e:
            Log.error(self, f"Error accessing git history: {e}")
    
    @expose(help="Rollback baseline to previous version")
    def baseline_rollback(self):
        """
        Rollback baseline configuration to a previous version
        
        This command reverts baseline.json to a previous version using git.
        You can rollback by version number or git commit hash.
        
        IMPORTANT: This is different from 'wo multitenancy rollback' which 
        rolls back WordPress core files.
        
        Usage:
            wo multitenancy baseline baseline-rollback --to-version=5
            wo multitenancy baseline baseline-rollback --to-commit=abc123
            wo multitenancy baseline baseline-rollback --to-version=5 --apply-now
        """
        pargs = self.app.pargs
        to_version = pargs.to_version
        to_commit = pargs.to_commit
        apply_now = pargs.apply_now
        
        # Validate arguments
        if not to_version and not to_commit:
            Log.error(self, "Specify --to-version=N or --to-commit=HASH")
            Log.error(self, "")
            Log.error(self, "Examples:")
            Log.error(self, "  wo multitenancy baseline baseline-rollback --to-version=5")
            Log.error(self, "  wo multitenancy baseline baseline-rollback --to-commit=abc123")
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"
        
        # Check git exists
        git_dir = f"{shared_root}/.git"
        if not os.path.exists(git_dir):
            Log.error(self, "Git tracking not initialized")
            Log.error(self, "Cannot rollback without git history")
        
        # Read current baseline
        with open(baseline_file, 'r') as f:
            current = json.load(f)
        
        current_version = current.get('version', 0)
        
        Log.warn(self, "Baseline Rollback")
        Log.warn(self, f"Current version: {current_version}")
        Log.warn(self, "")
        
        try:
            # If version specified, find the corresponding commit
            if to_version:
                # Search git log for the version
                result = subprocess.run(
                    ['git', 'log', '--all', '--grep', f'Baseline v{to_version}:', 
                     '--format=%H', '-1', 'config/baseline.json'],
                    cwd=shared_root,
                    capture_output=True,
                    text=True,
                    check=True
                )
                
                to_commit = result.stdout.strip()
                
                if not to_commit:
                    Log.error(self, f"Version {to_version} not found in git history")
                    Log.error(self, "")
                    Log.error(self, "View available versions:")
                    Log.error(self, "  wo multitenancy baseline history")
            
            # Show what we're rolling back to
            result = subprocess.run(
                ['git', 'show', '--stat', '--oneline', to_commit, '--', 'config/baseline.json'],
                cwd=shared_root,
                capture_output=True,
                text=True,
                check=True
            )
            
            Log.info(self, "Rolling back to:")
            # Show first few lines of git show output
            output_lines = result.stdout.split('\n')[:10]
            for line in output_lines:
                Log.info(self, f"  {line}")
            
            if len(result.stdout.split('\n')) > 10:
                Log.info(self, "  ...")
            
            Log.info(self, "")
            
            # Confirm with user (unless forced)
            if not pargs.force:
                confirm = input("Proceed with rollback? [y/N]: ").strip().lower()
                if confirm != 'y':
                    Log.info(self, "Rollback cancelled")
                    return
            
            # Perform rollback by checking out the specific file from that commit
            subprocess.run(
                ['git', 'checkout', to_commit, '--', 'config/baseline.json'],
                cwd=shared_root,
                capture_output=True,
                check=True
            )
            
            # Read the rolled-back baseline
            with open(baseline_file, 'r') as f:
                rolled_back = json.load(f)
            
            rollback_version = rolled_back.get('version', 0)
            
            # Create a new commit documenting the rollback
            subprocess.run(
                ['git', 'add', 'config/baseline.json'],
                cwd=shared_root,
                capture_output=True
            )
            subprocess.run(
                ['git', 'commit', '-m', 
                 f'Rollback: Restored baseline to v{rollback_version}'],
                cwd=shared_root,
                capture_output=True
            )
            
            Log.info(self, f"✅ Rolled back to version {rollback_version}")
            
            # Apply if requested
            if apply_now:
                Log.info(self, "")
                Log.info(self, "Applying rollback to all sites...")
                BaselineApplicator.apply_baseline_to_sites(self, config, rollback_version)
            else:
                Log.info(self, "")
                Log.info(self, "Baseline rolled back in configuration.")
                Log.info(self, f"Sites are still on v{current_version}.")
                Log.info(self, "Run: wo multitenancy baseline apply")
            
        except subprocess.CalledProcessError as e:
            Log.error(self, f"Rollback failed: {e}")
            Log.error(self, "The baseline.json may be in an inconsistent state")
            Log.error(self, "You can restore it with: cd /var/www/shared && git checkout HEAD config/baseline.json")
        except Exception as e:
            Log.error(self, f"Error during rollback: {e}")
    
    @expose(help="Remove quarantine status and retry baseline")
    def unquarantine(self):
        """
        Unquarantine a site and retry baseline application
        
        This command removes the quarantine flag from a site and attempts
        to apply the current baseline again. Use this after fixing issues
        that caused the site to be quarantined.
        
        Usage:
            wo multitenancy baseline unquarantine <domain>
        """
        pargs = self.app.pargs
        domain = pargs.site_name
        
        if not domain:
            Log.error(self, "Domain is required")
            Log.error(self, "Usage: wo multitenancy baseline unquarantine <domain>")
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized")
        
        # Check if site exists and is quarantined
        from wo.core.database import db_session
        from wo.cli.plugins.multitenancy_db import MultitenancySite
        session = db_session
        
        site = session.query(MultitenancySite).filter_by(domain=domain).first()
        
        if not site:
            Log.error(self, f"Site not found: {domain}")
            Log.error(self, "Check spelling or run: wo multitenancy list")
        
        if not site.is_quarantined:
            Log.info(self, f"Site {domain} is not quarantined")
            Log.info(self, "No action needed.")
            return
        
        # Show quarantine details
        Log.info(self, f"Unquarantining: {domain}")
        Log.info(self, f"Previous error: {site.quarantine_reason}")
        Log.info(self, f"Quarantined on: {site.quarantine_date}")
        Log.info(self, "")
        
        # Remove quarantine status
        MTDatabase.unquarantine_site(self, domain)
        Log.info(self, "✅ Quarantine status removed")
        
        # Get current baseline
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"
        
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)
        
        baseline_version = baseline.get('version', 0)
        
        # Retry baseline application
        Log.info(self, f"Retrying baseline v{baseline_version}...")
        Log.info(self, "")
        
        result = BaselineApplicator.apply_baseline_to_site(
            self,
            domain,
            site.site_path,
            baseline
        )
        
        if result['success']:
            # Update version in database
            site.baseline_version = baseline_version
            session.commit()
            
            Log.info(self, "")
            Log.info(self, f"✅ Successfully applied baseline to {domain}")
            Log.info(self, "Site is now up to date and no longer quarantined")
        else:
            # Re-quarantine with new error
            MTDatabase.mark_site_quarantined(self, domain, result['error'])
            Log.error(self, "")
            Log.error(self, f"Failed again: {result['error']}")
            Log.error(self, "Site has been re-quarantined")
            Log.error(self, "")
            Log.error(self, "Troubleshooting steps:")
            Log.error(self, f"  1. Check the site is accessible: curl -I http://{domain}")
            Log.error(self, f"  2. Check WP-CLI works: wp --info --path={site.site_path}")
            Log.error(self, f"  3. Check plugin files exist in /var/www/shared/wp-content/plugins/")
            Log.error(self, "  4. Check site error logs for more details")



def load(app):
    """Load the multi-tenancy plugin"""
    app.handler.register(WOMultitenancyController)
    app.hook.register('post_setup', wo_multitenancy_hook)
