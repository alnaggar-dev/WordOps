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
    MTFunctions, SharedInfrastructure, ReleaseManager, BaselineApplicator
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
            
            # Generate wp-config.php
            Log.info(self, "Generating wp-config.php...")
            MTFunctions.generate_wp_config(
                self, site_root, wo_domain,
                db_name, db_user, db_pass, db_host
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
                'shared_release': MTDatabase.get_current_release(self)
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
        """Add a plugin to the baseline"""
        pargs = self.app.pargs
        plugin_slug = pargs.plugin_slug or pargs.site_name  # Use site_name as positional arg
        apply_now = pargs.apply_now
        
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized. Run: wo multitenancy init")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        Log.info(self, f"Adding plugin: {plugin_slug}")
        
        # Download plugin
        infra = SharedInfrastructure(self, shared_root)
