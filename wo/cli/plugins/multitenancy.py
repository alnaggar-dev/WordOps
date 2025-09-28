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
from wo.core.nginxconfig import generateNginxConf
from wo.core.services import WOService
from wo.core.shellexec import WOShellExec, CommandExecutionError
from wo.core.sslutils import SSL
from wo.core.variables import WOVar
from wo.core.acme import WOAcme
from wo.cli.plugins.multitenancy_functions import (
    MTFunctions, SharedInfrastructure, ReleaseManager
)
from wo.cli.plugins.multitenancy_db import MTDatabase


def wo_multitenancy_hook(app):
    """Hook to initialize multitenancy database tables"""
    from wo.core.database import init_db
    import wo.cli.plugins.models
    init_db(app)
    # Initialize multitenancy database tables
    MTDatabase.initialize_tables(app)


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
            
            # Make release current
            Log.info(self, "Activating release...")
            infra.switch_release(release_name)
            
            # Set permissions
            Log.info(self, "Setting permissions...")
            infra.set_permissions()
            
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
            db_info = setupdatabase(self, wo_domain)
            if not db_info:
                Log.error(self, "Failed to create database")
            
            db_name, db_user, db_pass, db_host = db_info
            
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
            
            # Add to database
            site_data = {
                'domain': wo_domain,
                'site_type': 'wp',
                'cache_type': cache_type,
                'site_path': site_root,
                'php_version': php_version,
                'is_shared': True,
                'shared_release': MTDatabase.get_current_release(self)
            }
            addNewSite(self, site_data)
            MTDatabase.add_shared_site(self, wo_domain, site_data)
            
            # Configure SSL if requested
            if pargs.letsencrypt:
                Log.info(self, "Configuring Let's Encrypt SSL...")
                MTFunctions.setup_ssl(self, wo_domain, pargs)
            
            # Enable site in nginx
            WOFileUtils.create_symlink(self, [
                f"/etc/nginx/sites-available/{wo_domain}",
                f"/etc/nginx/sites-enabled/{wo_domain}"
            ])
            
            # Reload nginx
            if not WOService.reload_service(self, 'nginx'):
                Log.error(self, "Failed to reload nginx")
            
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
            
            # Clear all caches
            Log.info(self, "Clearing caches for all sites...")
            for site in shared_sites:
                MTFunctions.clear_cache(self, site['domain'], site.get('cache_type', 'none'))
            
            # Update database
            MTDatabase.update_release(self, new_release)
            
            # Cleanup old releases
            Log.info(self, "Cleaning up old releases...")
            release_manager.cleanup_old_releases(config.get('keep_releases', 3))
            
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
            
            # Clear all caches
            shared_sites = MTDatabase.get_shared_sites(self)
            Log.info(self, "Clearing caches...")
            for site in shared_sites:
                MTFunctions.clear_cache(self, site['domain'], site.get('cache_type', 'none'))
            
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

    @expose(help="Convert existing WordPress site to shared core")
    def convert(self):
        """Convert an existing WordPress site to use shared core"""
        pargs = self.app.pargs
        
        if not pargs.site_name:
            Log.error(self, "Site name required")
        
        wo_domain = WODomain.validate(self, pargs.site_name)
        
        # Check if site exists
        if not check_domain_exists(self, wo_domain):
            Log.error(self, f"Site {wo_domain} does not exist")
        
        # Check if multi-tenancy is initialized
        if not MTDatabase.is_initialized(self):
            Log.error(self, "Multi-tenancy not initialized. Run: wo multitenancy init")
        
        # Check if already shared
        site_info = getSiteInfo(self, wo_domain)
        if MTDatabase.is_shared_site(self, wo_domain):
            Log.error(self, f"Site {wo_domain} is already using shared core")
        
        config = MTFunctions.load_config(self)
        shared_root = config.get('shared_root', '/var/www/shared')
        
        Log.info(self, f"Converting {wo_domain} to shared WordPress core...")
        
        if not pargs.force:
            Log.warn(self, "This will:")
            Log.warn(self, "  1. Backup your current site")
            Log.warn(self, "  2. Replace WordPress core with shared version")
            Log.warn(self, "  3. Move plugins/themes to shared directories")
            Log.warn(self, "  4. Keep your uploads and site-specific data")
            confirm = input("Continue? (y/N): ")
            if confirm.lower() != 'y':
                Log.info(self, "Conversion cancelled")
                return
        
        site_root = site_info.site_path
        site_htdocs = f"{site_root}/htdocs"
        
        try:
            # Create backup
            Log.info(self, "Creating backup...")
            backup_path = MTFunctions.backup_site(self, wo_domain, site_root)
            Log.info(self, f"Backup created: {backup_path}")
            
            # Move uploads to safe location
            Log.info(self, "Preserving uploads...")
            uploads_temp = f"{site_root}/uploads_temp"
            if os.path.exists(f"{site_htdocs}/wp-content/uploads"):
                shutil.move(f"{site_htdocs}/wp-content/uploads", uploads_temp)
            
            # Remove old WordPress files
            Log.info(self, "Removing old WordPress core...")
            dirs_to_remove = [
                f"{site_htdocs}/wp-admin",
                f"{site_htdocs}/wp-includes",
                f"{site_htdocs}/wp-content/plugins",
                f"{site_htdocs}/wp-content/themes",
                f"{site_htdocs}/wp-content/mu-plugins"
            ]
            for dir_path in dirs_to_remove:
                if os.path.exists(dir_path):
                    shutil.rmtree(dir_path)
            
            # Remove WordPress core files
            for file in os.listdir(site_htdocs):
                if file.startswith('wp-') and file.endswith('.php'):
                    os.remove(f"{site_htdocs}/{file}")
            
            # Create symlinks to shared infrastructure
            Log.info(self, "Linking to shared WordPress core...")
            MTFunctions.create_shared_symlinks(self, site_htdocs, shared_root)
            
            # Restore uploads
            if os.path.exists(uploads_temp):
                os.makedirs(f"{site_htdocs}/wp-content/uploads", exist_ok=True)
                shutil.move(uploads_temp, f"{site_htdocs}/wp-content/uploads")
            
            # Update wp-config.php
            Log.info(self, "Updating wp-config.php...")
            MTFunctions.update_wp_config_for_shared(self, site_root, site_htdocs)
            
            # Set permissions
            setwebrootpermissions(self, site_htdocs)
            
            # Update database
            updateSiteInfo(self, wo_domain, is_shared=True)
            MTDatabase.add_shared_site(self, wo_domain, {
                'domain': wo_domain,
                'site_type': site_info.site_type,
                'cache_type': site_info.cache_type,
                'site_path': site_root,
                'php_version': site_info.php_version,
                'is_shared': True,
                'shared_release': MTDatabase.get_current_release(self)
            })
            
            # Clear cache
            MTFunctions.clear_cache(self, wo_domain, site_info.cache_type)
            
            # Reload nginx
            if not WOService.reload_service(self, 'nginx'):
                Log.warn(self, "Failed to reload nginx")
            
            Log.info(self, "✅ Conversion completed successfully!")
            Log.info(self, f"   Site {wo_domain} now uses shared WordPress core")
            Log.info(self, f"   Backup available at: {backup_path}")
            
        except Exception as e:
            Log.error(self, f"Conversion failed: {str(e)}")
            Log.info(self, "Attempting to restore from backup...")
            # TODO: Implement restore functionality

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


def load(app):
    """Load the multi-tenancy plugin"""
    app.handler.register(WOMultitenancyController)
    app.hook.register('post_setup', wo_multitenancy_hook)