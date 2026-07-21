"""WordOps Multi-tenancy Functions Module
Core functions for managing shared WordPress infrastructure.
"""

import os
import re
import json
import shutil
import subprocess
import tempfile
import zlib
import time as _time
import random
import string
import tarfile
import configparser
import glob
import uuid
from urllib.parse import urljoin, urlparse
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed

from datetime import datetime
from wo.core.logging import Log
from wo.core.fileutils import WOFileUtils
from wo.core.shellexec import WOShellExec, CommandExecutionError
from wo.core.variables import WOVar
from wo.core.template import WOTemplate
from wo.core.services import WOService


class MTFunctions:
    """Multi-tenancy utility functions"""
    
    @staticmethod
    def load_config(app):
        """Load multi-tenancy configuration"""
        config_file = '/etc/wo/plugins.d/multitenancy.conf'
        config = configparser.ConfigParser()
        
        # Default configuration
        defaults = {
            'shared_root': '/var/www/shared',
            'keep_releases': '3',
            'php_version': '8.4',
            'wp_version': 'latest',
            'admin_email': 'admin@example.com',
        }
        
        if os.path.exists(config_file):
            config.read(config_file)
            
            # Merge with defaults
            for key, value in defaults.items():
                if not config.has_section('multitenancy'):
                    config.add_section('multitenancy')
                if not config.has_option('multitenancy', key):
                    config.set('multitenancy', key, value)
        else:
            # Create default config
            config.add_section('multitenancy')
            for key, value in defaults.items():
                config.set('multitenancy', key, value)
        
        # Convert to dictionary - include ALL sections
        result = {}
        if config.has_section('multitenancy'):
            result = dict(config.items('multitenancy'))
        
        # Parse list values
        if 'baseline_plugins' in result:
            result['baseline_plugins'] = [p.strip() for p in result['baseline_plugins'].split(',')]
        
        # Add WordPress.org plugin source section. Presence matters: an
        # empty section disables legacy baseline-based WordPress.org seeding.
        if config.has_section('wordpress_plugins'):
            result['wordpress_plugins'] = {
                key: value for key, value in config.items('wordpress_plugins')
            }
        
        # Add WordPress.org theme source section. Presence matters: an
        # empty section disables legacy baseline-based WordPress.org seeding.
        if config.has_section('wordpress_themes'):
            result['wordpress_themes'] = {
                key: value for key, value in config.items('wordpress_themes')
            }
        
        # Add GitHub plugins section
        if config.has_section('github_plugins'):
            github_plugins = {}
            for key, value in config.items('github_plugins'):
                # Skip items that don't look like GitHub repo definitions
                if '/' in value:
                    github_plugins[key] = value
            if github_plugins:
                result['github_plugins'] = github_plugins
        
        # Add GitHub themes section
        if config.has_section('github_themes'):
            github_themes = {}
            for key, value in config.items('github_themes'):
                # Skip items that don't look like GitHub repo definitions
                if '/' in value:
                    github_themes[key] = value
            if github_themes:
                result['github_themes'] = github_themes
        
        # Add URL plugins section
        if config.has_section('url_plugins'):
            url_plugins = {}
            for key, value in config.items('url_plugins'):
                # Check if it looks like a URL
                if value.startswith('https://') and value.endswith('.zip'):
                    url_plugins[key] = value
            if url_plugins:
                result['url_plugins'] = url_plugins
        
        # Add URL themes section
        if config.has_section('url_themes'):
            url_themes = {}
            for key, value in config.items('url_themes'):
                # Check if it looks like a URL
                if value.startswith('https://') and value.endswith('.zip'):
                    url_themes[key] = value
            if url_themes:
                result['url_themes'] = url_themes

        return result
    
    @staticmethod
    def preflight_shared_config(app, shared_root):
        """Refuse to proceed if wp-config-shared.php has a PHP syntax error.

        A syntax error in the shared config breaks every tenant site, so
        init/create/update/apply call this first. Returns True when the file is
        valid, absent (first init), or php is unavailable.
        """
        cfg = f"{shared_root}/config/wp-config-shared.php"
        if not lint_php_file(app, cfg, missing_ok=True):
            Log.error(app, "wp-config-shared.php has a PHP syntax error. "
                           "Fix via: wo multitenancy shared-config "
                           "--action edit", False)
            return False
        return True

    @staticmethod
    def get_php_version(app, pargs):
        """Determine PHP version from arguments"""
        if pargs.php74:
            return '7.4'
        elif pargs.php80:
            return '8.0'
        elif pargs.php81:
            return '8.1'
        elif pargs.php82:
            return '8.2'
        elif pargs.php83:
            return '8.3'
        elif pargs.php84:
            return '8.4'
        else:
            # Get default from config or WordOps default
            config = MTFunctions.load_config(app)
            return config.get('php_version', '8.4')
    
    @staticmethod
    def get_cache_type(app, pargs):
        """Determine cache type from arguments"""
        if pargs.wpfc:
            return 'wpfc'
        elif pargs.wpredis:
            return 'wpredis'
        elif pargs.wprocket:
            return 'wprocket'
        elif pargs.wpce:
            return 'wpce'
        elif pargs.wpsc:
            return 'wpsc'
        else:
            return 'basic'  # No cache

    @staticmethod
    def validate_nginx_config(app, config_file=None, log_errors=True):
        """Validate nginx configuration using nginx -t"""
        try:
            cmd = ['nginx', '-t']
            if log_errors:
                Log.debug(app, "Testing nginx configuration")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                if log_errors:
                    Log.debug(app, "Nginx configuration test passed")
                return True
            else:
                if log_errors:
                    Log.error(app, "Nginx configuration test failed!", exit=False)
                    Log.error(app, f"Error: {result.stderr.strip()}", exit=False)
                    Log.error(app, f"Output: {result.stdout.strip()}", exit=False)
                return False

        except subprocess.TimeoutExpired:
            if log_errors:
                Log.error(app, "Nginx configuration test timed out", exit=False)
            return False
        except Exception as e:
            if log_errors:
                Log.error(app, f"Nginx configuration test error: {e}", exit=False)
            return False

    @staticmethod
    def validate_nginx_config_recoverable(app, log_errors=True):
        """Run nginx -t without exiting the process."""
        try:
            result = subprocess.run(['nginx', '-t'], capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                return True

            if log_errors:
                Log.warn(app, "Nginx configuration test failed")
                Log.warn(app, f"Error: {result.stderr.strip()}")
                Log.warn(app, f"Output: {result.stdout.strip()}")
            return False

        except subprocess.TimeoutExpired:
            if log_errors:
                Log.warn(app, "Nginx configuration test timed out")
            return False
        except Exception as e:
            if log_errors:
                Log.error(app, f"Nginx configuration test error: {e}", exit=False)
            return False

    @staticmethod
    def write_nginx_config_for_rename(app, domain, php_version, cache_type, site_root):
        """Write a multitenancy nginx vhost for a renamed tenant without exiting."""
        try:
            content = MTFunctions.generate_modular_nginx_config(domain, site_root, php_version, cache_type)
            MTFunctions.ensure_nginx_directories(app, domain, site_root)

            nginx_conf = f"/etc/nginx/sites-available/{domain}"
            with open(nginx_conf, 'w') as f:
                f.write(content)
            os.chmod(nginx_conf, 0o644)

            if MTFunctions.validate_nginx_config_recoverable(app):
                return nginx_conf
            return None

        except Exception as e:
            Log.error(app, f"Failed to write nginx config for renamed domain {domain}: {e}", exit=False)
            return None

    @staticmethod
    def enable_nginx_site_for_rename(app, domain):
        """Enable a renamed nginx site without using default-exiting WOFileUtils."""
        try:
            src = f'/etc/nginx/sites-available/{domain}'
            dst = f'/etc/nginx/sites-enabled/{domain}'

            if os.path.lexists(dst) or os.path.exists(dst):
                Log.error(app, f"Nginx enabled site already exists: {dst}", exit=False)
                return False

            os.symlink(src, dst)
            return True

        except Exception as e:
            Log.error(app, f"Failed to enable nginx site for renamed domain {domain}: {e}", exit=False)
            return False

    @staticmethod
    def reload_nginx_recoverable(app, domain):
        """Reload nginx without exiting the process."""
        if not MTFunctions.validate_nginx_config_recoverable(app):
            return False

        try:
            subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True, text=True, timeout=30, check=True)
            return True
        except subprocess.CalledProcessError as e:
            Log.warn(app, f"systemctl reload nginx failed for {domain}: {e.stderr}")
        except subprocess.TimeoutExpired:
            Log.warn(app, f"systemctl reload nginx timed out for {domain}")
        except Exception as e:
            Log.warn(app, f"systemctl reload nginx error for {domain}: {e}")

        try:
            subprocess.run(['nginx', '-s', 'reload'], capture_output=True, text=True, timeout=30, check=True)
            return True
        except subprocess.CalledProcessError as e:
            Log.error(app, f"nginx -s reload failed for {domain}: {e.stderr}", exit=False)
            return False
        except subprocess.TimeoutExpired:
            Log.error(app, f"nginx -s reload timed out for {domain}", exit=False)
            return False
        except Exception as e:
            Log.error(app, f"nginx -s reload error for {domain}: {e}", exit=False)
            return False

    @staticmethod
    def get_php_fpm_socket(php_version):
        """Get correct PHP-FPM socket path for given PHP version"""
        # WordOps uses socket naming convention without dots: php83-fpm, not php8.3-fpm
        php_clean = php_version.replace('.', '')
        return f"php{php_clean}-fpm"

    @staticmethod
    def ensure_nginx_directories(app, domain, site_root):
        """Ensure all directories required by nginx config exist"""
        try:
            # Ensure log directory exists
            logs_dir = f"{site_root}/logs"
            if not os.path.exists(logs_dir):
                os.makedirs(logs_dir, mode=0o755, exist_ok=True)
                Log.debug(app, f"Created logs directory: {logs_dir}")

            # Ensure nginx sites-available directory exists
            sites_available = "/etc/nginx/sites-available"
            if not os.path.exists(sites_available):
                os.makedirs(sites_available, mode=0o755, exist_ok=True)
                Log.debug(app, f"Created sites-available directory: {sites_available}")

            # Ensure nginx sites-enabled directory exists
            sites_enabled = "/etc/nginx/sites-enabled"
            if not os.path.exists(sites_enabled):
                os.makedirs(sites_enabled, mode=0o755, exist_ok=True)
                Log.debug(app, f"Created sites-enabled directory: {sites_enabled}")

            # Check if PHP-FPM socket exists (using WordOps naming convention)
            php_version = "8.4"  # Default, will be overridden by actual version
            php_clean = php_version.replace('.', '')
            php_socket = f"/var/run/php/php{php_clean}-fpm.sock"
            if not os.path.exists(php_socket):
                Log.warn(app, f"PHP-FPM socket not found: {php_socket}")
                Log.warn(app, "You may need to start PHP-FPM service")

        except Exception as e:
            Log.warn(app, f"Error ensuring nginx directories for {domain}: {e}")

    @staticmethod
    def test_nginx_config_file(app, config_file):
        """Test a specific nginx configuration file"""
        try:
            # First check if the file exists and is readable
            if not os.path.exists(config_file):
                Log.error(app, f"Nginx config file does not exist: {config_file}")
                return False

            # Check file permissions
            if not os.access(config_file, os.R_OK):
                Log.error(app, f"Cannot read nginx config file: {config_file}")
                return False

            # Read and log the config content for debugging
            with open(config_file, 'r') as f:
                content = f.read()

            Log.debug(app, f"Testing nginx config file: {config_file}")
            Log.debug(app, f"Config file size: {len(content)} bytes")

            # Use nginx -t to test the configuration
            cmd = ['nginx', '-t']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                Log.debug(app, f"Nginx config file test passed: {config_file}")
                return True
            else:
                Log.error(app, f"Nginx config file test failed: {config_file}")
                Log.error(app, f"Error: {result.stderr}")
                Log.error(app, f"Output: {result.stdout}")
                return False

        except Exception as e:
            Log.error(app, f"Error testing nginx config file {config_file}: {e}")
            return False

    @staticmethod
    def safe_nginx_reload(app, domain):
        """Safely reload nginx with detailed error reporting"""
        try:
            Log.debug(app, f"Attempting nginx reload for {domain}")

            # First test the configuration
            test_cmd = ['nginx', '-t']
            test_result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=30)

            if test_result.returncode != 0:
                Log.error(app, "Nginx configuration test failed before reload:",
                          exit=False)
                Log.error(app, f"Error: {test_result.stderr}", exit=False)
                Log.error(app, f"Output: {test_result.stdout}", exit=False)
                return False

            # Try systemctl reload first
            reload_cmd = ['systemctl', 'reload', 'nginx']
            reload_result = subprocess.run(reload_cmd, capture_output=True, text=True, timeout=30)

            if reload_result.returncode == 0:
                Log.debug(app, f"Nginx reloaded successfully via systemctl for {domain}")
                return True

            # If systemctl reload fails, try nginx -s reload
            Log.warn(app, f"systemctl reload failed, trying nginx -s reload")
            Log.debug(app, f"systemctl error: {reload_result.stderr}")

            signal_cmd = ['nginx', '-s', 'reload']
            signal_result = subprocess.run(signal_cmd, capture_output=True, text=True, timeout=30)

            if signal_result.returncode == 0:
                Log.debug(app, f"Nginx reloaded successfully via signal for {domain}")
                return True

            # Both methods failed
            Log.error(app, f"All nginx reload methods failed for {domain}",
                      exit=False)
            Log.error(app, f"systemctl error: {reload_result.stderr}", exit=False)
            Log.error(app, f"signal error: {signal_result.stderr}", exit=False)
            return False

        except Exception as e:
            Log.error(app, f"Exception during nginx reload for {domain}: {e}",
                      exit=False)
            return False
    
    @staticmethod
    def create_site_directories(app, domain, site_root, site_htdocs):
        """Create site directory structure"""
        directories = [
            site_root,
            site_htdocs,
            f"{site_htdocs}/wp-content",
            f"{site_htdocs}/wp-content/uploads",
            f"{site_htdocs}/wp-content/cache",
            f"{site_htdocs}/wp-content/upgrade",
            f"{site_root}/logs",
            f"{site_root}/conf",
            f"{site_root}/conf/nginx"
        ]
        
        for directory in directories:
            os.makedirs(directory, exist_ok=True)
            Log.debug(app, f"Created directory: {directory}")
    
    @staticmethod
    def create_shared_symlinks(app, site_htdocs, shared_root):
        """Create symlinks to shared WordPress infrastructure"""

        # Symlink to WordPress core
        wp_symlink = f"{site_htdocs}/wp"
        if not os.path.exists(wp_symlink):
            os.symlink(f"{shared_root}/current", wp_symlink)
            Log.debug(app, f"Created symlink: {wp_symlink} -> {shared_root}/current")

        # Symlink shared directories
        shared_dirs = {
            'plugins': f"{shared_root}/wp-content/plugins",
            'themes': f"{shared_root}/wp-content/themes",
            'mu-plugins': f"{shared_root}/wp-content/mu-plugins",
            'languages': f"{shared_root}/wp-content/languages"
        }

        for dir_name, target in shared_dirs.items():
            symlink = f"{site_htdocs}/wp-content/{dir_name}"
            if not os.path.exists(symlink):
                os.symlink(target, symlink)
                Log.debug(app, f"Created symlink: {symlink} -> {target}")

        # Create symlinks for WordPress core files that must be accessible from document root
        # This is required for wp-admin access, login, cron, xmlrpc, etc.
        wp_core_files = {
            'wp-login.php': f"{site_htdocs}/wp/wp-login.php",
            'wp-admin': f"{site_htdocs}/wp/wp-admin",
            'wp-includes': f"{site_htdocs}/wp/wp-includes",
            'wp-cron.php': f"{site_htdocs}/wp/wp-cron.php",
            'xmlrpc.php': f"{site_htdocs}/wp/xmlrpc.php",
            'wp-comments-post.php': f"{site_htdocs}/wp/wp-comments-post.php",
            'wp-settings.php': f"{site_htdocs}/wp/wp-settings.php"
        }

        for link_name, target in wp_core_files.items():
            symlink = f"{site_htdocs}/{link_name}"
            if not os.path.exists(symlink):
                os.symlink(target, symlink)
                Log.debug(app, f"Created symlink: {symlink} -> {target}")

        # Copy index.php from WordPress core
        index_source = f"{shared_root}/current/index.php"
        index_dest = f"{site_htdocs}/index.php"
        if os.path.exists(index_source) and not os.path.exists(index_dest):
            # Create custom index.php that points to shared core
            index_content = """<?php
/**
 * Front to the WordPress application. This file doesn't do anything, but loads
 * wp-blog-header.php which does and tells WordPress to load the theme.
 *
 * @package WordPress
 */

/**
 * Tells WordPress to load the WordPress theme and output it.
 *
 * @var bool
 */
define( 'WP_USE_THEMES', true );

/** Loads the WordPress Environment and Template */
require __DIR__ . '/wp/wp-blog-header.php';
"""
            with open(index_dest, 'w') as f:
                f.write(index_content)
            Log.debug(app, f"Created index.php for {site_htdocs}")

    @staticmethod
    def relink_core_files_for_rename(app, site_htdocs):
        """Recreate per-site WordPress core-file symlinks after a root move.

        create_shared_symlinks() writes these as absolute links rooted at the
        site's own htdocs, so os.rename() to a new domain root leaves them
        dangling and WP-CLI can no longer bootstrap the install. Recreate them
        with relative targets so they resolve regardless of the parent
        directory name, keeping both the rename and its rollback valid.
        Recoverable: returns False on failure instead of exiting.
        """
        core_files = (
            'wp-login.php', 'wp-admin', 'wp-includes', 'wp-cron.php',
            'xmlrpc.php', 'wp-comments-post.php', 'wp-settings.php',
        )
        try:
            for name in core_files:
                link = f"{site_htdocs}/{name}"
                if os.path.lexists(link) and not os.path.islink(link):
                    Log.error(app, f"Refusing to replace non-symlink core path: {link}", exit=False)
                    return False
                tmp_link = f"{link}.rename-tmp"
                if os.path.lexists(tmp_link):
                    os.remove(tmp_link)
                os.symlink(f"wp/{name}", tmp_link)
                os.replace(tmp_link, link)
            return True
        except Exception as e:
            Log.error(app, f"Failed to relink core files for rename: {e}", exit=False)
            return False
    
    @staticmethod
    def generate_wp_config(app, site_root, domain, db_name, db_user, db_pass, db_host, redis_prefix=None, redis_db=0, shared_root='/var/www/shared'):
        """
        Generate wp-config.php for shared WordPress site with shared config include.
        
        This creates a site-specific wp-config.php that includes the fleet-wide
        shared configuration file. The file contains:
        - Site-specific database credentials
        - Site-specific authentication salts (unique per site)
        - Site-specific Redis prefix (for cache isolation)
        - Include statement for shared config (fleet-wide settings)
        
        Args:
            app: WordOps application instance
            site_root: Path to site root directory
            domain: Site domain name
            db_name: Database name
            db_user: Database username
            db_pass: Database password
            db_host: Database host
            redis_prefix: Redis cache prefix (generated if not provided)
        
        Phase 1 Features:
            - Includes /var/www/shared/config/wp-config-shared.php for fleet settings
            - Emergency bypass mechanism via WO_BYPASS_SHARED_CONFIG constant
            - Unique Redis prefix for Redis Object Cache Pro integration
        """
        
        # shared_root lands inside single-quoted PHP strings below; config
        # values never contain quotes, so reject rather than escape.
        if "'" in shared_root:
            Log.error(app, f"Unsafe shared_root path: {shared_root!r}")

        # Generate Redis prefix if not provided
        if not redis_prefix:
            from wo.cli.plugins.multitenancy_db import MTDatabase
            redis_prefix = MTDatabase.generate_redis_prefix(app, domain)
        
        # Get salts from WordPress.org; fall back to local generation when
        # the API is unreachable or returns a malformed body.
        import requests
        salts = None
        try:
            r = requests.get(
                'https://api.wordpress.org/secret-key/1.1/salt/', timeout=10)
            if r.ok and MTFunctions._valid_salts(r.text):
                salts = r.text
        except requests.exceptions.RequestException:
            pass
        if salts is None:
            salts = MTFunctions.generate_salts()
        
        # Following HandPressed pattern: wp-config.php goes IN the webroot (htdocs)
        # This is secure because WordPress blocks direct HTTP access to wp-config.php
        # and it allows the router to load it without permission issues
        
        # Phase 1: Updated wp-config template with shared config include
        # This matches Appendix A from the implementation plan
        wp_config = f'''<?php
/**
 * WordPress Configuration for {domain}
 * Generated by WordOps Multi-tenancy Plugin
 * Phase 1: Includes shared fleet-wide configuration
 */

// ============================================================================
// REDIS OBJECT CACHE PRO - SITE-SPECIFIC CONFIG
// ============================================================================
// Must be defined BEFORE shared config to set site-specific prefix
// This ensures cache isolation between sites in shared Redis instance

define('WP_REDIS_CONFIG', [
    'token' => 'e279430effe043b8c17d3f3c751c4c0846bc70c97f0eaaea766b4079001c',
    'host' => '127.0.0.1',
    'port' => 6379,
    'database' => {redis_db},  // Dedicated per-site database: OCP flushes via FLUSHDB, sharing one db lets tenants wipe each other
    'prefix' => '{redis_prefix}',  // Per-site key prefix (isolation within the db, debuggability)
    'timeout' => 0.5,
    'read_timeout' => 0.5,
    'retry_interval' => 10,
    'maxttl' => 86400,  // 24 hours
    'retries' => 3,
    'backoff' => 'smart',
    'compression' => 'zstd',  // zstd compresses smaller, lz4 faster
    'serializer' => 'igbinary',
    'async_flush' => true,
    'split_alloptions' => true,
    'prefetch' => true,
    'shared' => true,
    'strict' => true,
    'debug' => false,
    'save_commands' => false,
]);

define('WP_REDIS_DISABLED', false);

// ============================================================================
// SHARED FLEET-WIDE CONFIGURATION
// ============================================================================
// Loads security, performance, and cache settings for all sites
// Emergency bypass: uncomment next line to disable shared config
// define('WO_BYPASS_SHARED_CONFIG', true);

if (!defined('WO_BYPASS_SHARED_CONFIG') || WO_BYPASS_SHARED_CONFIG !== true) {{
    if (file_exists('{shared_root}/config/wp-config-shared.php')) {{
        require_once '{shared_root}/config/wp-config-shared.php';
    }}
}}

// ============================================================================
// DATABASE SETTINGS (SITE-SPECIFIC)
// ============================================================================

define('DB_NAME', '{db_name}');
define('DB_USER', '{db_user}');
define('DB_PASSWORD', '{db_pass}');
define('DB_HOST', '{db_host}');
define('DB_CHARSET', 'utf8mb4');
define('DB_COLLATE', '');

// ============================================================================
// AUTHENTICATION KEYS AND SALTS (SITE-SPECIFIC)
// ============================================================================

{salts}

// ============================================================================
// DATABASE TABLE PREFIX
// ============================================================================

$table_prefix = 'wp_';

// ============================================================================
// DIRECTORY PATHS (SITE-SPECIFIC)
// ============================================================================

define('WP_CONTENT_DIR', __DIR__ . '/wp-content');
define('WP_CONTENT_URL', (isset($_SERVER['HTTPS']) && $_SERVER['HTTPS'] === 'on' ? 'https' : 'http') . '://{domain}/wp-content');

if (!defined('ABSPATH')) {{
    define('ABSPATH', __DIR__ . '/wp/');
}}

// ============================================================================
// FILE SYSTEM METHOD
// ============================================================================

define('FS_METHOD', 'direct');

// ============================================================================
// SSL HANDLING
// ============================================================================

if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {{
    $_SERVER['HTTPS'] = 'on';
}}

/* That's all, stop editing! Happy publishing. */

/** Sets up WordPress vars and included files. */
/** WP-CLI loads wp-settings automatically, so skip for CLI */
if (!defined('WP_CLI')) {{
    require_once ABSPATH . 'wp-settings.php';
}}
'''
        
        # Place wp-config.php in htdocs (webroot) like HandPressed does
        # This is secure - WordPress blocks direct access via .htaccess/nginx rules
        wp_config_path = f"{site_root}/htdocs/wp-config.php"
        with open(wp_config_path, 'w') as f:
            f.write(wp_config)
        
        # Set secure permissions (readable by www-data)
        os.chmod(wp_config_path, 0o640)
        Log.debug(app, f"Generated wp-config.php with shared config include at {wp_config_path}")
        Log.debug(app, f"Redis prefix for {domain}: {redis_prefix} (db {redis_db})")

    @staticmethod
    def rewrite_wp_config_for_rename(app, site_root, old_domain, new_domain, new_redis_prefix, tracked_old_redis_prefix=None):
        """Rewrite only domain-derived wp-config.php values for a tenant rename.

        Returns the old Redis prefix found in wp-config.php, or None on failure.
        """
        wp_config_path = f"{site_root}/htdocs/wp-config.php"
        if not os.path.exists(wp_config_path):
            Log.error(app, f"wp-config.php not found: {wp_config_path}", exit=False)
            return None

        try:
            with open(wp_config_path, 'r') as f:
                content = f.read()

            match = re.search(r"'prefix'\s*=>\s*'([^']+)'", content)
            if not match:
                Log.error(app, "wp-config.php does not contain a Redis prefix; refusing unsafe rename", exit=False)
                return None

            resolved_old_prefix = match.group(1)
            if tracked_old_redis_prefix and tracked_old_redis_prefix != resolved_old_prefix:
                Log.warn(app, f"Tracked Redis prefix {tracked_old_redis_prefix} differs from wp-config.php prefix {resolved_old_prefix}; using wp-config.php value")

            content = re.sub(r"('prefix'\s*=>\s*)'[^']+'", "\\1'{}'".format(new_redis_prefix), content, count=1)
            content = content.replace(f"://{old_domain}/wp-content", f"://{new_domain}/wp-content")
            content = content.replace(f"WordPress Configuration for {old_domain}", f"WordPress Configuration for {new_domain}")

            with open(wp_config_path, 'w') as f:
                f.write(content)
            os.chmod(wp_config_path, 0o640)
            Log.debug(app, f"Updated wp-config.php for rename from {old_domain} to {new_domain}")
            return resolved_old_prefix

        except Exception as e:
            Log.error(app, f"Failed to update wp-config.php for rename: {e}", exit=False)
            return None

    @staticmethod
    def _valid_salts(text):
        """Accept only a well-formed WordPress.org salt block: exactly the
        8 expected define('<NAME>', '...'); lines."""
        names = {'AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY',
                 'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT',
                 'NONCE_SALT'}
        lines = [ln for ln in (text or '').strip().splitlines()
                 if ln.strip()]
        if len(lines) != 8:
            return False
        found = set()
        for line in lines:
            m = re.match(r"^define\('([A-Z_]+)',\s*'.*'\);$", line.strip())
            if not m or m.group(1) not in names:
                return False
            found.add(m.group(1))
        return found == names
    
    def generate_salts():
        """Generate WordPress salts"""
        keys = [
            'AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY',
            'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT'
        ]
        
        salts = []
        for key in keys:
            salt = ''.join(random.choices(
                string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{}|;:,.<>?",
                k=64
            ))
            salts.append(f"define( '{key}', '{salt}' );")
        
        return '\n'.join(salts)
    
    @staticmethod
    def generate_nginx_config(app, domain, php_version, cache_type, site_root):
        """Generate nginx configuration using WordOps modular includes.

        This replaces the previous hardcoded approach with WordOps' standard
        include-based configuration, maintaining only multitenant-specific additions.
        """
        nginx_conf = f"/etc/nginx/sites-available/{domain}"

        # Backup existing configuration if it exists
        if os.path.exists(nginx_conf):
            backup_conf = f"{nginx_conf}.backup.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(nginx_conf, backup_conf)
            Log.debug(app, f"Backed up existing nginx config to {backup_conf}")

        # Generate configuration using modular includes
        Log.debug(app, f"Generating modular nginx config for {domain}")
        config_content = MTFunctions.generate_modular_nginx_config(
            domain, site_root, php_version, cache_type
        )

        try:
            # Ensure all directories exist before writing config
            MTFunctions.ensure_nginx_directories(app, domain, site_root)

            with open(nginx_conf, 'w') as f:
                f.write(config_content)
            Log.debug(app, f"Written nginx config to {nginx_conf}")

            # Set proper permissions on the nginx config file
            os.chmod(nginx_conf, 0o644)

            # Validate the generated configuration file specifically
            if MTFunctions.test_nginx_config_file(app, nginx_conf):
                Log.debug(app, f"Nginx config file validated successfully for {domain}")
                return nginx_conf
            else:
                Log.warn(app, f"Generated nginx config file failed validation for {domain}")
                # Log the actual config for debugging
                Log.debug(app, f"Config content:\n{config_content}")
                raise Exception("Nginx config file validation failed")

        except Exception as e:
            Log.error(app, f"Failed to generate or validate nginx config for {domain}: {e}")
            # Restore backup if it exists
            backup_files = [f for f in os.listdir('/etc/nginx/sites-available/')
                           if f.startswith(f"{domain}.backup.")]
            if backup_files:
                latest_backup = sorted(backup_files)[-1]
                backup_path = f"/etc/nginx/sites-available/{latest_backup}"
                shutil.copy2(backup_path, nginx_conf)
                Log.debug(app, f"Restored backup configuration for {domain}")
            raise Exception(f"Nginx configuration generation failed: {e}")
    
    @staticmethod
    def generate_modular_nginx_config(domain, site_root, php_version, cache_type="basic"):
        """Generate nginx configuration using WordOps modular includes.

        This uses WordOps' standard include files instead of hardcoded configuration,
        adding only multitenant-specific directives for the /wp symlink handling.

        Benefits:
        - Consistency with standard WordOps sites
        - Automatic updates when WordOps improves nginx configs
        - All features included (WebP, security, DoS protection, etc.)
        - Minimal code maintenance
        """
        from datetime import datetime

        # Determine PHP upstream name (e.g., php83)
        php_upstream = php_version.replace('.', '')

        # Start building the configuration
        config = f"""# Multitenant Site Configuration
# Domain: {domain}
# PHP Version: {php_version}
# Cache Type: {cache_type}
# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

server {{
    server_name {domain} www.{domain};

    access_log {site_root}/logs/access.log rt_cache;
    error_log {site_root}/logs/error.log;

    root {site_root}/htdocs;
    index index.php index.html index.htm;

    # Multitenant-specific: Handle /wp symlink directory
    # This location block is the ONLY difference from standard WordOps sites
    location /wp/ {{
        try_files $uri $uri/ /wp/index.php?$args;
    }}

"""

        # Include appropriate cache configuration based on cache_type
        if cache_type == "wpfc":
            config += f"    include common/wpfc-php{php_upstream}.conf;\n"
        elif cache_type == "wpredis":
            config += f"    include common/redis-php{php_upstream}.conf;\n"
        elif cache_type == "wpsc":
            config += f"    include common/wpsc-php{php_upstream}.conf;\n"
        elif cache_type == "wprocket":
            config += f"    include common/wprocket-php{php_upstream}.conf;\n"
        elif cache_type == "wpce":
            config += f"    include common/wpce-php{php_upstream}.conf;\n"
        else:
            # Basic WordPress without page caching
            config += f"    include common/php{php_upstream}.conf;\n"

        # Include common WordPress and security configurations
        config += f"""    include common/wpcommon-php{php_upstream}.conf;
    include common/locations-wo.conf;

    # Include SSL and custom configurations
    include {site_root}/conf/nginx/*.conf;
}}
"""

        return config
    
    @staticmethod
    def install_wordpress(app, domain, site_htdocs, admin_user, admin_email):
        """Install WordPress using WP-CLI"""
        
        # Generate random password
        admin_pass = ''.join(random.choices(
            string.ascii_letters + string.digits,
            k=16
        ))
        
        # Store password for later retrieval
        pass_file = f"/var/www/{domain}/.admin_pass"
        with open(pass_file, 'w') as f:
            f.write(admin_pass)
        os.chmod(pass_file, 0o600)
        
        # Determine site URL
        site_url = f"http://{domain}"
        
        # Install WordPress (run from htdocs where wp-config.php shim exists)
        try:
            cmd = [
                'wp', 'core', 'install',
                f'--url={site_url}',
                f'--title={domain}',
                f'--admin_user={admin_user}',
                f'--admin_password={admin_pass}',
                f'--admin_email={admin_email}',
                '--skip-email',
                '--allow-root'
            ]
            
            # Run from htdocs directory where wp-config.php shim is located
            # WP-CLI will find htdocs/wp-config.php (shim) which loads the real config
            result = subprocess.run(cmd, cwd=site_htdocs, capture_output=True, text=True, check=True)
            Log.debug(app, f"WordPress installed for {domain}")
            
        except subprocess.CalledProcessError as e:
            Log.error(app, f"Failed to install WordPress: {e.stderr}", exit=False)
            raise

        # Force the permalink into the DB before the object-cache drop-in
        # exists, so update_option writes the row regardless of any value
        # cached under this tenant's Redis prefix. _create_impl re-runs this
        # after baseline to refresh the cache.
        MTFunctions.set_permalink_structure(app, domain, site_htdocs)
    
    @staticmethod
    def set_permalink_structure(app, domain, site_htdocs):
        """Set the WordPress permalink structure to Post name.

        Run twice per site create, by design:

        * From ``install_wordpress`` before the Object Cache Pro drop-in
          exists, so ``update_option`` compares against the DB (no cache) and
          guarantees the ``permalink_structure`` row is Post name even if a
          stale value is cached under this tenant's Redis prefix.
        * From ``_create_impl`` after baseline enables the drop-in with
          ``--skip-flush``, so the write routes through the now-active object
          cache and overwrites any stale ``permalink_structure`` /
          ``rewrite_rules``. A cache flush is unsafe here: all tenants share one
          Redis database, so flushing would evict every tenant's cache.

        Raises on failure to surface a broken wp-cli setup.
        """
        try:
            subprocess.run(
                [
                    'wp', 'rewrite', 'structure', '/%postname%/',
                    '--path=' + site_htdocs,
                    '--allow-root'
                ],
                capture_output=True, text=True, check=True
            )
            Log.debug(app, f"Permalink structure set to Post name for {domain}")
        except subprocess.CalledProcessError as e:
            Log.error(app, f"Failed to set permalink structure: {e.stderr}",
                      exit=False)
            raise

    @staticmethod
    def update_wordpress_domain(app, site_htdocs, old_domain, new_domain, scheme):
        """Update WordPress URLs and serialized domain references after tenant rename."""
        new_url = f'{scheme}://{new_domain}'
        try:
            subprocess.run(
                ['wp', 'option', 'update', 'home', new_url, '--path=' + site_htdocs, '--allow-root'],
                capture_output=True, text=True, check=True
            )
            subprocess.run(
                ['wp', 'option', 'update', 'siteurl', new_url, '--path=' + site_htdocs, '--allow-root'],
                capture_output=True, text=True, check=True
            )
            subprocess.run(
                [
                    'wp', 'search-replace', old_domain, new_domain,
                    '--all-tables-with-prefix',
                    '--skip-columns=guid',
                    '--precise',
                    '--recurse-objects',
                    '--path=' + site_htdocs,
                    '--allow-root',
                ],
                capture_output=True, text=True, check=True
            )
            return True
        except subprocess.CalledProcessError as e:
            Log.error(app, f"Failed to update WordPress domain: {e.stderr}", exit=False)
            return False

    @staticmethod
    def purge_site_cache(app, domain, redis_prefix=None, redis_db=None):
        """Purge a tenant's stale caches so a (re)created domain never inherits them.

        The nginx FastCGI page cache (keyed by ``$scheme$request_method$host``)
        and the Redis object cache (keyed by the per-site prefix) both survive
        ``wo multitenancy delete`` and key deterministically on the domain.
        Recreating the same domain would otherwise serve the previous
        incarnation's cached pages/options -- e.g. stale plain-permalink HTML
        even when the database is correct. Best-effort and strictly
        tenant-scoped: never flushes the shared Redis database or other tenants'
        page cache.
        """
        import re
        # nginx FastCGI page cache: each cache file stores
        # "KEY: <scheme>GET<host><uri>". Matching GET<host> (page bodies never
        # contain that literal) removes every cached URI for this domain only.
        cache_dir = '/var/run/nginx-cache'
        if os.path.isdir(cache_dir):
            try:
                found = subprocess.run(
                    ['grep', '-rlaE', 'GET(www\\.)?' + re.escape(domain), cache_dir],
                    capture_output=True, text=True, timeout=30
                )
                files = [f for f in found.stdout.splitlines() if f]
                for path in files:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                if files:
                    Log.debug(
                        app, f"Purged {len(files)} FastCGI cache entries for {domain}")
            except Exception as e:
                Log.debug(app, f"FastCGI cache purge for {domain} failed: {e}")
        # Redis object cache: a tenant with a dedicated database is flushed
        # wholesale; legacy tenants on shared db 0 fall back to prefix-scoped
        # key deletion.
        if redis_db:
            try:
                subprocess.run(
                    ['redis-cli', '-n', str(redis_db), 'flushdb', 'async'],
                    capture_output=True, text=True, timeout=30
                )
                Log.debug(app, f"Flushed Redis database {redis_db} for {domain}")
            except Exception as e:
                Log.debug(app, f"Redis flush of db {redis_db} for {domain} failed: {e}")
        elif redis_prefix:
            try:
                scan = subprocess.run(
                    ['redis-cli', '--scan', '--pattern', redis_prefix + '*'],
                    capture_output=True, text=True, timeout=30
                )
                keys = [k for k in scan.stdout.splitlines() if k]
                for i in range(0, len(keys), 500):
                    subprocess.run(
                        ['redis-cli', 'unlink', *keys[i:i + 500]],
                        capture_output=True, text=True, timeout=30
                    )
                if keys:
                    Log.debug(
                        app, f"Purged {len(keys)} Redis object-cache keys for {redis_prefix}")
            except Exception as e:
                Log.debug(app, f"Redis object-cache purge for {redis_prefix} failed: {e}")

    @staticmethod
    def ensure_redis_databases(app, needed):
        """Make sure the Redis server has at least ``needed`` databases.

        Redis defaults to 16 and ``databases`` is not runtime-tunable, so when
        the fleet outgrows it the config file is raised (with headroom) and
        Redis restarted. The restart only drops cached data; tenants rebuild
        their object caches on the next request.
        """
        try:
            current = subprocess.run(
                ['redis-cli', 'config', 'get', 'databases'],
                capture_output=True, text=True, timeout=10
            )
            fields = current.stdout.split()
            configured = int(fields[1]) if len(fields) > 1 else 16
        except Exception as e:
            Log.debug(app, f"Could not read Redis databases setting: {e}")
            return
        if configured >= needed:
            return
        target = max(needed, 256)
        conf = '/etc/redis/redis.conf'
        try:
            with open(conf, 'r') as f:
                content = f.read()
            new_content, count = re.subn(
                r'(?m)^databases\s+\d+', f'databases {target}', content)
            if count == 0:
                new_content = content.rstrip('\n') + f'\ndatabases {target}\n'
            with open(conf, 'w') as f:
                f.write(new_content)
        except Exception as e:
            Log.error(app, f"Failed to raise Redis databases to {target} in {conf}: {e}")
        Log.info(app, f"Raising Redis databases {configured} -> {target} (restarting Redis)")
        if not WOService.restart_service(app, 'redis-server'):
            Log.error(app, "Redis restart failed after raising databases limit")

    @staticmethod
    def set_wp_config_redis_db(app, site_root, redis_db):
        """Point an existing tenant's WP_REDIS_CONFIG at its dedicated database.

        Rewrites the first ``'database' => N,`` entry (only WP_REDIS_CONFIG
        carries one in generated configs). Returns True when wp-config.php now
        holds the new value.
        """
        wp_config = f"{site_root}/htdocs/wp-config.php"
        try:
            with open(wp_config, 'r') as f:
                content = f.read()
            new_content, count = re.subn(
                r"('database'\s*=>\s*)\d+,[^\n]*",
                f"\\g<1>{redis_db},  // Dedicated per-site database: OCP flushes via FLUSHDB, sharing one db lets tenants wipe each other",
                content, count=1)
            if count == 0:
                Log.warn(app, f"No 'database' key found in {wp_config}; skipped")
                return False
            with open(wp_config, 'w') as f:
                f.write(new_content)
            return True
        except Exception as e:
            Log.warn(app, f"Failed to set Redis database in {wp_config}: {e}")
            return False

    @staticmethod
    def get_admin_password(app, domain):
        """Retrieve admin password for a site"""
        pass_file = f"/var/www/{domain}/.admin_pass"
        if os.path.exists(pass_file):
            with open(pass_file, 'r') as f:
                return f.read().strip()
        return "Check /var/www/{domain}/.admin_pass"
    
    @staticmethod
    def ensure_and_activate_theme(app, domain, site_htdocs, theme):
        """Ensure theme exists and activate it"""
        try:
            # First check if theme is available
            check_cmd = [
                'wp', 'theme', 'list', '--field=name', '--format=csv',
                '--allow-root'
            ]
            result = subprocess.run(check_cmd, cwd=site_htdocs, capture_output=True, text=True, timeout=30)

            available_themes = result.stdout.strip().split('\n') if result.stdout else []

            if theme not in available_themes:
                Log.debug(app, f"Theme {theme} not available, attempting to install")
                # Try to install the theme first
                install_cmd = [
                    'wp', 'theme', 'install', theme,
                    '--allow-root'
                ]
                install_result = subprocess.run(install_cmd, cwd=site_htdocs, capture_output=True, text=True, timeout=60)

                if install_result.returncode != 0:
                    Log.debug(app, f"Failed to install theme {theme}: {install_result.stderr}")
                    return False

                Log.debug(app, f"Successfully installed theme {theme}")

            # Now try to activate the theme
            activate_cmd = [
                'wp', 'theme', 'activate', theme,
                '--allow-root'
            ]
            activate_result = subprocess.run(activate_cmd, cwd=site_htdocs, capture_output=True, text=True, timeout=30)

            if activate_result.returncode == 0:
                Log.debug(app, f"Successfully activated theme {theme} for {domain}")
                return True
            else:
                Log.debug(app, f"Failed to activate theme {theme}: {activate_result.stderr}")
                return False

        except Exception as e:
            Log.debug(app, f"Exception while ensuring/activating theme {theme}: {e}")
            return False
    
    @staticmethod
    def setup_ssl(app, domain, pargs):
        """Setup SSL for shared site using WordOps native SSL functions"""
        from wo.core.acme import WOAcme
        from wo.core.sslutils import SSL
        from wo.cli.plugins.sitedb import updateSiteInfo
        from wo.core.domainvalidate import WODomain
        from wo.cli.plugins.site_functions import copyWildcardCert

        try:
            Log.debug(app, f"Starting SSL setup for {domain}")

            # Prepare acme domains list: subdomains get a single-domain
            # certificate, apex domains also cover www
            # (same logic as `wo site create --le`)
            (domain_type, root_domain) = WODomain.getlevel(app, domain)
            if domain_type == 'subdomain':
                Log.debug(app, f"{domain} is a subdomain, "
                          "issuing single-domain certificate")
                acme_domains = [domain]
            else:
                acme_domains = [domain, f'www.{domain}']

            # Prepare acmedata dict as expected by setupletsencrypt
            acmedata = {
                'dns': False,
                'acme_dns': 'dns_cf',
                'dnsalias': False,
                'acme_alias': '',
                'keylength': 'ec-384'
            }

            # Get keylength from config if available
            if hasattr(app.app, 'config') and app.app.config.has_section('letsencrypt'):
                acmedata['keylength'] = app.app.config.get('letsencrypt', 'keylength')

            # Handle DNS validation if requested
            if hasattr(pargs, 'dns') and pargs.dns:
                Log.debug(app, "DNS validation enabled")
                acmedata['dns'] = True
                if pargs.dns != 'dns_cf':
                    Log.debug(app, f"DNS API: {pargs.dns}")
                    acmedata['acme_dns'] = pargs.dns

            # Reuse an existing certificate when possible, mirroring
            # `wo site create --le` (avoids Let's Encrypt duplicate
            # certificate rate limits on site recreation)
            if WOAcme.cert_check(app, domain):
                if getattr(pargs, 'force', False):
                    # --force skips confirmations: reinstall existing cert
                    Log.info(app, f"Reusing existing SSL certificate "
                             f"for {domain}")
                    WOAcme.deploycert(app, domain)
                else:
                    SSL.archivedcertificatehandle(app, domain, acme_domains)
            elif (domain_type == 'subdomain' and
                    SSL.checkwildcardexist(app, root_domain)):
                Log.info(app, f"Using existing wildcard SSL certificate "
                         f"from {root_domain} to secure {domain}")
                copyWildcardCert(app, domain, root_domain)
            else:
                # Verify DNS records point to this server before issuing
                # (bypassed with --force or when using DNS validation)
                if not acmedata['dns'] and not getattr(pargs, 'force', False):
                    if not WOAcme.check_dns(app, acme_domains):
                        Log.warn(app, f"Aborting SSL setup for {domain}")
                        return False

                if not WOAcme.setupletsencrypt(app, acme_domains, acmedata):
                    Log.warn(app, f"Failed to obtain SSL certificates "
                             f"for {domain}")
                    return False
                Log.debug(app, f"Let's Encrypt certificates obtained "
                          f"for {domain}")

                # Deploy certificate files and create ssl.conf with
                # listen 443 directives.
                # Note: deploycert() returns 0 on success, not True
                if WOAcme.deploycert(app, domain) != 0:
                    Log.error(app, f"Failed to deploy SSL certificates "
                              f"for {domain}")
                    return False
                Log.debug(app, f"SSL certificates deployed for {domain}")

            def _remove_ssl_artifacts():
                """Drop just-written SSL config so nginx stays valid."""
                for conf in (f"/var/www/{domain}/conf/nginx/ssl.conf",
                             f"/var/www/{domain}/conf/nginx/hsts.conf",
                             f"/etc/nginx/conf.d/force-ssl-{domain}.conf"):
                    if os.path.exists(conf):
                        try:
                            os.remove(conf)
                        except OSError as e:
                            Log.debug(app, f"Could not remove {conf}: {e}")
                MTFunctions.validate_nginx_config(app)
                MTFunctions.safe_nginx_reload(app, domain)

            # Test nginx configuration before applying SSL changes
            if not MTFunctions.validate_nginx_config(app):
                Log.error(app, f"Nginx configuration invalid after "
                          f"certificate deployment for {domain}", False)
                _remove_ssl_artifacts()
                return False

            # Configure HTTPS redirect
            SSL.httpsredirect(app, domain, acme_domains, redirect=True)
            SSL.siteurlhttps(app, domain)

            # Enable HSTS if requested
            if hasattr(pargs, 'hsts') and pargs.hsts:
                SSL.setuphsts(app, domain)

            # Final validation after all SSL changes
            if not MTFunctions.validate_nginx_config(app):
                Log.error(app, f"Nginx configuration invalid after "
                          f"SSL setup for {domain}", False)
                _remove_ssl_artifacts()
                return False

            # Reload nginx to apply SSL configuration
            if not MTFunctions.safe_nginx_reload(app, domain):
                Log.error(app, f"Failed to reload nginx after "
                          f"SSL setup for {domain}")
                return False

            Log.info(app, f"SSL configured successfully for {domain}")
            return True

        except Exception as e:
            Log.debug(app, f"SSL setup error: {e}")
            Log.warn(app, f"Could not configure SSL for {domain}: {str(e)}")
            return False

    @staticmethod
    def prepare_ssl_certificate_for_rename(app, domain, pargs):
        """Ensure a certificate for the new domain exists before mutating the tenant."""
        from wo.core.acme import WOAcme
        from wo.core.domainvalidate import WODomain
        from wo.core.variables import WOVar

        if (os.path.exists(f"{WOVar.wo_ssl_live}/{domain}/fullchain.pem") and
                os.path.exists(f"{WOVar.wo_ssl_live}/{domain}/key.pem")):
            return True
        if os.path.exists(f"/etc/letsencrypt/renewal/{domain}_ecc/fullchain.cer"):
            return True
        if not os.path.exists('/etc/letsencrypt/acme.sh'):
            Log.error(app, "acme.sh is not installed; cannot prepare SSL for rename", exit=False)
            return False

        try:
            (domain_type, root_domain) = WODomain.getlevel(app, domain)
            if domain_type == 'subdomain':
                acme_domains = [domain]
            else:
                acme_domains = [domain, f'www.{domain}']

            acmedata = {
                'dns': False,
                'acme_dns': 'dns_cf',
                'dnsalias': False,
                'acme_alias': '',
                'keylength': 'ec-384'
            }

            if getattr(pargs, 'dns', None):
                acmedata['dns'] = True
                if pargs.dns != 'dns_cf':
                    acmedata['acme_dns'] = pargs.dns

            if not acmedata['dns'] and not getattr(pargs, 'force', False):
                if not WOAcme.check_dns(app, acme_domains):
                    return False

            if not acmedata['dns']:
                os.makedirs('/var/www/html/.well-known/acme-challenge', exist_ok=True)
                try:
                    shutil.chown('/var/www/html/.well-known', 'www-data', 'www-data')
                    os.chmod('/var/www/html/.well-known', 0o750)
                except Exception as e:
                    Log.warn(app, f"Could not set permissions on ACME webroot: {e}")

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

            subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
            return True
        except subprocess.CalledProcessError as e:
            Log.error(app, f"Failed to prepare SSL certificate for renamed domain {domain}: {e.stderr}", exit=False)
            return False
        except subprocess.TimeoutExpired as e:
            Log.error(app, f"Timed out preparing SSL certificate for renamed domain {domain}: {e}", exit=False)
            return False
        except Exception as e:
            Log.error(app, f"Failed to prepare SSL certificate for renamed domain {domain}: {e}", exit=False)
            return False

    @staticmethod
    def install_ssl_config_for_rename(app, domain, site_root, pargs):
        """Deploy prepared certificate files and write nginx SSL includes for a renamed tenant."""
        from wo.core.domainvalidate import WODomain
        from wo.core.variables import WOVar

        try:
            os.makedirs(f'/etc/letsencrypt/live/{domain}', exist_ok=True)

            cmd = [
                '/etc/letsencrypt/acme.sh', '--config-home', '/etc/letsencrypt/config',
                '--install-cert', '-d', domain, '--ecc',
                '--cert-file', f'{WOVar.wo_ssl_live}/{domain}/cert.pem',
                '--key-file', f'{WOVar.wo_ssl_live}/{domain}/key.pem',
                '--fullchain-file', f'{WOVar.wo_ssl_live}/{domain}/fullchain.pem',
                '--ca-file', f'{WOVar.wo_ssl_live}/{domain}/ca.pem',
                '--reloadcmd', 'nginx -t && service nginx restart',
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)

            data = {'ssl_live_path': WOVar.wo_ssl_live, 'domain': domain, 'quic': True}
            with open(f'{site_root}/conf/nginx/ssl.conf', 'w') as fh:
                app.app.render((data), 'ssl.mustache', out=fh)

            (domain_type, root_domain) = WODomain.getlevel(app, domain)
            if domain_type == 'subdomain':
                acme_domains = [domain]
            else:
                acme_domains = [domain, f'www.{domain}']

            with open(f'/etc/nginx/conf.d/force-ssl-{domain}.conf', 'w') as fh:
                app.app.render(({'domains': ' '.join(acme_domains)}), 'force-ssl.mustache', out=fh)

            if getattr(pargs, 'hsts', False):
                with open(f'{site_root}/conf/nginx/hsts.conf', 'w') as fh:
                    fh.write('more_set_headers "Strict-Transport-Security: max-age=31536000; includeSubDomains; preload";')

            return MTFunctions.validate_nginx_config_recoverable(app, log_errors=True)

        except Exception as e:
            Log.error(app, f"Failed to install SSL config for renamed domain {domain}: {e}", exit=False)
            return False

    @staticmethod
    def cleanup_failed_site(app, domain, site_root,
                            db_name=None, db_user=None, db_grant_host=None):
        """Cleanup partially created site on failure"""
        from wo.cli.plugins.sitedb import deleteSiteInfo, getSiteInfo
        from wo.core.fileutils import WOFileUtils

        Log.debug(app, f"Starting cleanup for failed site: {domain}")

        # Remove nginx configuration files
        nginx_conf = f"/etc/nginx/sites-available/{domain}"
        nginx_enabled = f"/etc/nginx/sites-enabled/{domain}"

        # Remove enabled symlink first
        if os.path.exists(nginx_enabled):
            os.remove(nginx_enabled)
            Log.debug(app, f"Removed nginx enabled symlink: {nginx_enabled}")

        # Remove configuration file and backups
        nginx_files = [
            f for f in os.listdir('/etc/nginx/sites-available/')
            if f == domain or f.startswith(f"{domain}.backup.")
        ]
        for nginx_file in nginx_files:
            nginx_path = f"/etc/nginx/sites-available/{nginx_file}"
            if os.path.exists(nginx_path):
                os.remove(nginx_path)
                Log.debug(app, f"Removed nginx config: {nginx_path}")

        # Remove site directory if it exists
        if os.path.exists(site_root):
            try:
                shutil.rmtree(site_root)
                Log.debug(app, f"Removed site directory: {site_root}")
            except Exception as e:
                Log.debug(app, f"Could not remove site directory {site_root}: {e}")

        # Drop the tenant database and user if setupdatabase got that far.
        # db_grant_host is the DROP USER host (wo_mysql_grant_host), not the
        # WordPress connection host.
        if db_name:
            try:
                from wo.cli.plugins.site_functions import deleteDB
                deleteDB(app, db_name, db_user or 'root',
                         db_grant_host or 'localhost', exit=False)
                Log.debug(app, f"Dropped database {db_name} during cleanup")
            except Exception as e:
                Log.debug(app, f"Could not drop database {db_name}: {e}")

        # Remove from WordOps database if a record exists. deleteSiteInfo
        # exits on a missing record, so guard with getSiteInfo: failures
        # before addNewSite have nothing to remove.
        try:
            if getSiteInfo(app, domain):
                deleteSiteInfo(app, domain)
                Log.debug(app, f"Removed {domain} from WordOps database")
        except Exception as e:
            Log.debug(app, f"Could not remove {domain} from WordOps database: {e}")

        # Remove from multitenancy database if exists
        try:
            from wo.cli.plugins.multitenancy_db import MTDatabase
            MTDatabase.remove_shared_site(app, domain)
            Log.debug(app, f"Removed {domain} from multitenancy database")
        except Exception as e:
            Log.debug(app, f"Could not remove {domain} from multitenancy database: {e}")

        # Reload nginx to remove any references
        try:
            if MTFunctions.validate_nginx_config(app):
                WOService.reload_service(app, 'nginx')
                Log.debug(app, "Reloaded nginx after cleanup")
        except Exception as e:
            Log.debug(app, f"Could not reload nginx after cleanup: {e}")
        Log.debug(app, f"Cleanup completed for {domain}")

    @staticmethod
    def build_tenant_cron_line(domain, splay=None):
        """Build a guarded cron runner that checks maintenance after splay."""
        if not MTFunctions.valid_tenant_domain(domain):
            raise ValueError(f"Unsafe tenant domain: {domain!r}")
        splay = (
            zlib.crc32(domain.encode('utf-8')) % 60
            if splay is None else splay
        )
        gate_dir = f"/var/www/{domain}/conf/nginx"
        gate_file = f"{gate_dir}/multitenancy-maintenance.conf"
        return (
            f"* * * * * www-data sleep {splay} && "
            f"test -x '{gate_dir}' && test ! -e '{gate_file}' && "
            f"cd '/var/www/{domain}/htdocs' && "
            f"flock -n '/tmp/wo-cron-{domain}.lock' /usr/local/bin/wp "
            "cron event run --due-now --quiet >/dev/null 2>&1"
        )

    @staticmethod
    def capture_nginx_worker_pids():
        """Capture the pre-reload nginx worker generation."""
        try:
            result = subprocess.run(
                ['ps', '-eo', 'pid=,args='],
                capture_output=True, text=True, check=False, timeout=10,
            )
            if result.returncode:
                return (set(), 'nginx_workers=unavailable')
            workers = set()
            for line in result.stdout.splitlines():
                if 'nginx: worker process' not in line:
                    continue
                pid_text = line.strip().split(None, 1)[0]
                workers.add(int(pid_text))
            if not workers:
                return (set(), 'nginx_workers=unavailable')
            return (workers, None)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return (set(), 'nginx_workers=unavailable')

    @staticmethod
    def live_nginx_worker_pids(pids):
        """Return captured nginx PIDs that still exist."""
        live = set()
        for pid in pids:
            try:
                os.kill(pid, 0)
                live.add(pid)
            except ProcessLookupError:
                continue
            except PermissionError:
                live.add(pid)
        return live

    @staticmethod
    def probe_active_php_fpm_workers(shared_sites):
        """Return active tenant-serving FPM workers, or a fail-closed error."""
        versions = sorted({
            str(site.get('php_version') or '').strip()
            for site in shared_sites
        })
        if not versions or any(
                not re.fullmatch(r'\d+\.\d+', version)
                for version in versions):
            return (None, None, 'php_fpm_status=unavailable')

        active = 0
        listen_queue = 0
        for version in versions:
            short = version.replace('.', '')
            url = (
                "http://127.0.0.1:22222/fpm/status/"
                f"php{short}?json&full"
            )
            try:
                result = subprocess.run(
                    ['curl', '--silent', '--show-error', '--fail',
                     '--max-time', '5', url],
                    capture_output=True, text=True, check=False, timeout=10,
                )
                if result.returncode:
                    return (
                        None, None,
                        f'php_fpm_status_{version}=unavailable'
                    )
                payload = json.loads(result.stdout)
                processes = payload.get('processes')
                if not isinstance(processes, list):
                    return (
                        None, None,
                        f'php_fpm_status_{version}=unparseable'
                    )
                active += sum(
                    1 for process in processes
                    if str(process.get('state', '')).lower() != 'idle'
                    and not str(
                        process.get('request uri', '')
                    ).startswith(('/status', '/fpm/status/'))
                )
                listen_queue += int(payload.get('listen queue', 0))
            except Exception:
                return (
                    None, None,
                    f'php_fpm_status_{version}=unavailable'
                )
        return (active, listen_queue, None)

    @staticmethod
    def probe_tenant_innodb_transactions(shared_sites):
        """Return open InnoDB transactions for tenant DBs, or an error."""
        databases = []
        for site in shared_sites:
            config_path = os.path.join(
                MTFunctions._site_htdocs(site), 'wp-config.php'
            )
            try:
                with open(config_path, encoding='utf-8') as fh:
                    contents = fh.read()
            except (OSError, UnicodeError):
                return (None, 'tenant_database_names=unavailable')
            match = re.search(
                r"""define\s*\(\s*['"]DB_NAME['"]\s*,\s*['"]([A-Za-z0-9_]+)['"]\s*\)""",
                contents,
            )
            if not match:
                return (None, 'tenant_database_names=unparseable')
            databases.append(match.group(1))

        quoted = ','.join(f"'{name}'" for name in sorted(set(databases)))
        query = (
            "SELECT COUNT(*) FROM information_schema.innodb_trx AS t "
            "JOIN information_schema.processlist AS p "
            "ON p.ID=t.trx_mysql_thread_id "
            f"WHERE p.DB IN ({quoted})"
        )
        try:
            result = subprocess.run(
                ['mysql', '--batch', '--skip-column-names', '-e', query],
                capture_output=True, text=True, check=False, timeout=10,
            )
            if result.returncode:
                return (None, 'innodb_transactions=unavailable')
            return (int(result.stdout.strip()), None)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return (None, 'innodb_transactions=unavailable')

    @staticmethod
    def drain_tenant_active_work(
            app, shared_sites, locks, old_nginx_worker_pids,
            timeout=330, sleeper_horizon=60, poll_interval=2):
        """Drain the pre-gate nginx generation, then FPM/DB backstops."""
        start = _time.monotonic()
        deadline = start + timeout
        sleeper_deadline = start + sleeper_horizon
        blockers = []
        while True:
            blockers = []
            old_workers = MTFunctions.live_nginx_worker_pids(
                old_nginx_worker_pids
            )
            if old_workers:
                blockers.append(
                    "old_nginx_workers="
                    + ','.join(str(pid) for pid in sorted(old_workers))
                )
            else:
                active, listen_queue, php_error = (
                    MTFunctions.probe_active_php_fpm_workers(shared_sites)
                )
                transactions, db_error = (
                    MTFunctions.probe_tenant_innodb_transactions(
                        shared_sites
                    )
                )
                if php_error:
                    blockers.append(php_error)
                else:
                    if listen_queue:
                        blockers.append(
                            f'php_fpm_listen_queue={listen_queue}'
                        )
                    if active:
                        blockers.append(f'php_fpm_active={active}')
                if db_error:
                    blockers.append(db_error)
                elif transactions:
                    blockers.append(
                        f'innodb_transactions={transactions}'
                    )

            now = _time.monotonic()
            if not blockers and now >= sleeper_deadline:
                return (True, [])
            if now >= deadline:
                if now < sleeper_deadline:
                    blockers.append('legacy_cron_sleepers=not_drained')
                return (False, blockers)
            _time.sleep(min(poll_interval, deadline - now))



    @staticmethod
    def sync_wp_cron_entries(app):
        """Regenerate the managed system cron entries for WP-Cron offload."""
        cron_file = '/etc/cron.d/wo-multitenancy'
        try:
            from wo.cli.plugins.multitenancy_db import MTDatabase

            sites = MTDatabase.get_shared_sites(app)
            enabled_domains = []
            invalid_domains = []
            for site in sites:
                domain = site.get('domain')
                if not domain or not site.get('is_enabled', True):
                    continue
                if not MTFunctions.valid_tenant_domain(domain):
                    invalid_domains.append(domain)
                    continue
                enabled_domains.append(domain)
            enabled_domains = sorted(enabled_domains)
            sync_failed = False
            for domain in invalid_domains:
                Log.error(
                    app,
                    f"Invalid domain in multitenancy tracking; skipping WP-Cron entry: {domain}",
                    exit=False,
                )
                sync_failed = True

            if not enabled_domains:
                if sync_failed:
                    return False
                if os.path.exists(cron_file):
                    os.remove(cron_file)
                    Log.debug(app, f"Removed managed WP-Cron file: {cron_file}")
                return True

            lines = [
                "# Managed by WordOps multitenancy. Do not edit; regenerated by wo multitenancy create/delete/apply.",
                "SHELL=/bin/sh",
                "PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            ]
            for domain in enabled_domains:
                lines.append(MTFunctions.build_tenant_cron_line(domain))
            content = "\n".join(lines) + "\n"

            cron_dir = os.path.dirname(cron_file)
            os.makedirs(cron_dir, exist_ok=True)
            fd, tmp_file = tempfile.mkstemp(
                prefix='.wo-multitenancy.', dir=cron_dir, text=True
            )
            try:
                with os.fdopen(fd, 'w') as fh:
                    fh.write(content)
                os.chmod(tmp_file, 0o644)
                os.chown(tmp_file, 0, 0)
                os.replace(tmp_file, cron_file)
            except Exception:
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass
                raise

            Log.debug(
                app,
                f"Synchronized WP-Cron entries for {len(enabled_domains)} site(s)"
            )
            return not sync_failed
        except Exception as e:
            Log.error(app, f"Could not synchronize WP-Cron entries: {e}", exit=False)
            return False

    @staticmethod
    def parse_wordpress_db_version(contents):
        """Return the integer schema version assigned in version.php, or None."""
        match = re.search(
            r"""^\s*\$wp_db_version\s*=\s*(['"]?)(\d+)\1\s*;""",
            contents,
            re.MULTILINE,
        )
        return int(match.group(2)) if match else None

    @staticmethod
    def core_schema_transition(app, shared_root, staged_release):
        """Classify the staged DB schema as upgrade, equal, downgrade, or unknown."""
        version_files = (
            os.path.join(shared_root, 'current', 'wp-includes', 'version.php'),
            os.path.join(
                shared_root, 'releases', staged_release,
                'wp-includes', 'version.php'
            ),
        )
        versions = []
        for version_file in version_files:
            try:
                with open(version_file, encoding='utf-8') as fh:
                    version = MTFunctions.parse_wordpress_db_version(fh.read())
            except (OSError, UnicodeError):
                return 'unknown'
            if version is None:
                return 'unknown'
            versions.append(version)

        active, staged = versions
        if staged > active:
            return 'upgrade'
        if staged < active:
            return 'downgrade'
        return 'equal'

    @staticmethod
    def valid_tenant_domain(domain):
        """Return whether a tracked domain is safe for paths and commands."""
        return bool(
            isinstance(domain, str)
            and '..' not in domain
            and re.fullmatch(
                r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',
                domain,
            )
        )

    @staticmethod
    def _site_htdocs(site):
        """Resolve a tracked tenant row to its existing htdocs path."""
        domain = site.get('domain')
        site_root = site.get('site_path') or os.path.join('/var/www', domain or '')
        return os.path.join(site_root, 'htdocs')


    @staticmethod
    def backup_tenant_databases(app, shared_sites, shared_root):
        """Export every tenant database to a root-only timestamp directory."""
        backup_root = os.path.join(shared_root, 'backups', 'db')
        stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S')
        backup_dir = os.path.join(backup_root, stamp)
        failures = []

        try:
            os.makedirs(backup_root, mode=0o700, exist_ok=True)
            os.chmod(backup_root, 0o700)
            os.makedirs(backup_dir, mode=0o700, exist_ok=True)
            os.chmod(backup_dir, 0o700)
        except OSError as exc:
            return (False, [{'domain': '*', 'error': str(exc)}], backup_dir)

        for site in shared_sites:
            domain = site.get('domain') or ''
            if not MTFunctions.valid_tenant_domain(domain):
                failures.append({
                    'domain': domain or '<unknown>',
                    'error': 'invalid domain for backup filename',
                })
                continue
            dump_path = os.path.join(backup_dir, f'{domain}.sql')
            cmd = [
                'wp', 'db', 'export', dump_path,
                f"--path={MTFunctions._site_htdocs(site)}",
                '--allow-root',
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=False
                )
                if result.returncode:
                    failures.append({
                        'domain': domain,
                        'error': (result.stderr or result.stdout or
                                  f'exit {result.returncode}').strip(),
                    })
            except Exception as exc:
                failures.append({'domain': domain, 'error': str(exc)})
        return (not failures, failures, backup_dir)


    @staticmethod
    def acquire_tenant_cron_locks(app, shared_sites, timeout=30):
        """Drain and exclusively hold the lock respected by managed WP-Cron."""
        acquired = {}
        deadline = _time.monotonic() + timeout
        for site in shared_sites:
            domain = site.get('domain')
            if not MTFunctions.valid_tenant_domain(domain):
                MTFunctions.release_tenant_cron_locks(acquired)
                return ({}, [domain or '<unknown>'])
            path = f'/tmp/wo-cron-{domain}.lock'
            flags = os.O_RDONLY | os.O_CREAT
            if hasattr(os, 'O_NOFOLLOW'):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(path, flags, 0o644)
                handle = os.fdopen(fd, 'r')
            except OSError:
                MTFunctions.release_tenant_cron_locks(acquired)
                return ({}, [domain])

            while True:
                try:
                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    acquired[domain] = handle
                    break
                except BlockingIOError:
                    if _time.monotonic() >= deadline:
                        handle.close()
                        MTFunctions.release_tenant_cron_locks(acquired)
                        return ({}, [domain])
                    _time.sleep(0.1)
                except OSError:
                    handle.close()
                    MTFunctions.release_tenant_cron_locks(acquired)
                    return ({}, [domain])
        return (acquired, [])

    @staticmethod
    def release_tenant_cron_locks(locks):
        """Release and close acquired tenant cron lock handles."""
        for handle in list(locks.values()):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            try:
                handle.close()
            except OSError:
                pass
        locks.clear()


    @staticmethod
    def run_core_db_upgrades(app, shared_sites):
        """Run core schema upgrades for all tenants, aggregating every failure."""
        failures = []
        try:
            sites = list(shared_sites)
        except Exception as exc:
            return [{'domain': '*', 'path': '', 'error': str(exc)}]
        for site in sites:
            domain = site.get('domain') or '<unknown>'
            path = ''
            try:
                path = MTFunctions._site_htdocs(site)
                cmd = [
                    'wp', 'core', 'update-db', f'--path={path}', '--allow-root'
                ]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=False,
                    timeout=300,
                )
                if result.returncode:
                    failures.append({
                        'domain': domain,
                        'path': path,
                        'error': (result.stderr or result.stdout or
                                  f'exit {result.returncode}').strip(),
                    })
            except subprocess.TimeoutExpired:
                failures.append({
                    'domain': domain,
                    'path': path,
                    'error': 'wp core update-db timed out after 300 seconds',
                })
            except Exception as exc:
                failures.append({
                    'domain': domain, 'path': path, 'error': str(exc)
                })
        return failures

    @staticmethod
    def clear_cache(app, domain, cache_type):
        """Clear cache for a site"""

        # Clear nginx cache
        if cache_type in ['wpfc', 'wpredis']:
            try:
                WOShellExec.cmd_exec(app, "wo clean --fastcgi")
            except:
                pass

        # Clear WordPress cache
        try:
            htdocs = f"/var/www/{domain}/htdocs"
            cmd = [
                'wp', 'cache', 'flush',
                '--allow-root'
            ]
            subprocess.run(cmd, cwd=htdocs, capture_output=True, check=False)
        except:
            pass

    @staticmethod
    def clear_all_caches(app):
        """Clear all caches globally using WordOps (FastCGI + Redis + OpCache)
        
        This is fast and efficient because:
        - All sites share the same WordPress core
        - One command clears cache for all sites simultaneously
        - Takes ~2 seconds regardless of site count (1 site or 1000 sites)
        - Clears FastCGI cache, Redis cache, and OpCache
        
        This replaces per-site cache clearing which would take:
        - 24 sites: 23 seconds
        - 50 sites: 48 seconds
        - 100 sites: 96 seconds
        
        With this global approach:
        - Any number of sites: ~2 seconds
        """
        try:
            Log.info(app, "Clearing all caches (FastCGI + Redis + OpCache)...")
            WOShellExec.cmd_exec(app, "wo clean --all")
            Log.debug(app, "All caches cleared successfully")
            return True
        except Exception as e:
            Log.debug(app, f"Cache clear error: {e}")
            # Don't fail the entire operation if cache clear fails
            return False

    @staticmethod
    def reset_opcache(app, php_key=None):
        """Reset PHP opcache via the local admin endpoints.

        Scoped alternative to `wo clean`: hits the 127.0.0.1 opcache-reset
        endpoint for one PHP version (e.g. 'php84') or all installed
        versions. Returns False when any endpoint failed.
        """
        import requests

        keys = [php_key] if php_key else list(WOVar.wo_php_versions.keys())
        ok = True
        for key in keys:
            endpoint = f"/var/www/22222/htdocs/cache/opcache/{key}.php"
            if not os.path.exists(endpoint):
                continue
            try:
                r = requests.get(
                    f"http://127.0.0.1/cache/opcache/{key}.php", timeout=5)
                if r.status_code != 200:
                    Log.warn(app, f"Opcache reset failed for {key} "
                                  f"(HTTP {r.status_code})")
                    ok = False
            except requests.exceptions.RequestException as e:
                Log.warn(app, f"Opcache reset failed for {key}: {e}")
                ok = False
        return ok

    @staticmethod
    def test_site_locally(app, site):
        """Canary a gated tenant through nginx's loopback-only bypass."""
        domain = site.get('domain')
        if not MTFunctions.valid_tenant_domain(domain):
            return False
        scheme = 'https' if site.get('is_ssl') else 'http'
        domain_lower = domain.lower()
        apex = (
            domain_lower[4:]
            if domain_lower.startswith('www.')
            else domain_lower
        )
        allowed_hosts = {apex, f'www.{apex}'}
        variants = sorted(allowed_hosts)
        base_cmd = [
            'curl',
            '--silent',
            '--show-error',
            '--insecure',
            '--proto', '=http,https',
            '--header', 'X-Requested-With: XMLHttpRequest',
            '--max-time', '20',
        ]
        for variant in variants:
            for port in (80, 443):
                base_cmd.extend([
                    '--resolve', f'{variant}:{port}:127.0.0.2',
                ])
        base_cmd.extend([
            '--output', '/dev/null',
            '--write-out', '%{http_code}\t%{redirect_url}',
        ])
        current_url = (
            f'{scheme}://{domain}/?wo_mt_canary={uuid.uuid4().hex}'
        )
        try:
            for redirect_count in range(6):
                result = subprocess.run(
                    base_cmd + [current_url],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=25,
                )
                if result.returncode != 0:
                    return False
                status_text, separator, redirect_url = (
                    result.stdout.rstrip('\r\n').partition('\t')
                )
                if not separator:
                    return False
                status = int(status_text)
                if 200 <= status < 300:
                    return True
                if not 300 <= status < 400:
                    return False
                if not redirect_url or redirect_count == 5:
                    return False
                next_url = urljoin(current_url, redirect_url)
                parsed = urlparse(next_url)
                try:
                    port = parsed.port
                except ValueError:
                    port = -1
                expected_port = (
                    80 if parsed.scheme == 'http' else 443
                )
                if (parsed.scheme not in ('http', 'https')
                        or parsed.hostname not in allowed_hosts
                        or port not in (None, expected_port)):
                    Log.warn(
                        app,
                        f"Canary redirect rejected for {domain}: "
                        f"{redirect_url}",
                    )
                    return False
                current_url = next_url
            return False
        except Exception:
            return False

    @staticmethod
    def perform_health_check(app, shared_root):
        """Perform health check on shared infrastructure"""
        checks = {}
        
        # Check shared directories exist
        checks['Shared root exists'] = os.path.exists(shared_root)
        checks['Current symlink exists'] = os.path.islink(f"{shared_root}/current")
        checks['Plugins directory exists'] = os.path.exists(f"{shared_root}/wp-content/plugins")
        checks['Themes directory exists'] = os.path.exists(f"{shared_root}/wp-content/themes")
        checks['MU-plugins directory exists'] = os.path.exists(f"{shared_root}/wp-content/mu-plugins")
        checks['Baseline config exists'] = os.path.exists(f"{shared_root}/config/baseline.json")
        
        # Check if current symlink points to valid directory
        if os.path.islink(f"{shared_root}/current"):
            target = os.readlink(f"{shared_root}/current")
            checks['Current release valid'] = os.path.exists(target)
        
        return checks
    
    @staticmethod
    def calculate_disk_usage(app, shared_root, shared_sites):
        """Calculate disk usage statistics"""
        usage = {}
        
        # Shared infrastructure size
        if os.path.exists(shared_root):
            try:
                result = subprocess.check_output(
                    ['du', '-sh', shared_root],
                    universal_newlines=True
                )
                usage['Shared infrastructure'] = result.split()[0]
            except:
                usage['Shared infrastructure'] = 'Unknown'
        
        # Total uploads size
        total_uploads = 0
        for site in shared_sites:
            uploads_dir = f"/var/www/{site['domain']}/htdocs/wp-content/uploads"
            if os.path.exists(uploads_dir):
                try:
                    result = subprocess.check_output(
                        ['du', '-s', uploads_dir],
                        universal_newlines=True
                    )
                    total_uploads += int(result.split()[0])
                except:
                    pass
        
        if total_uploads > 0:
            # Convert to human readable
            if total_uploads > 1048576:
                usage['Total uploads'] = f"{total_uploads / 1048576:.1f}G"
            elif total_uploads > 1024:
                usage['Total uploads'] = f"{total_uploads / 1024:.1f}M"
            else:
                usage['Total uploads'] = f"{total_uploads}K"
        
        # Savings calculation
        if shared_sites:
            single_wp_size = 60  # MB approximate
            traditional_size = len(shared_sites) * single_wp_size
            usage['Estimated savings'] = f"~{traditional_size - 60}MB"
        
        return usage


class SharedInfrastructure:
    """Manage shared WordPress infrastructure"""
    
    def __init__(self, app, shared_root):
        self.app = app
        self.shared_root = shared_root
        self.releases_dir = f"{shared_root}/releases"
        self.wp_content_dir = f"{shared_root}/wp-content"
        self.config_dir = f"{shared_root}/config"
    
    def _parse_github_source(self, repo_info):
        """Return durable GitHub source metadata for a repo definition."""
        if not isinstance(repo_info, str):
            return None

        parts = [part.strip() for part in repo_info.split(',')]
        repo = parts[0] if parts else ''
        if '/' not in repo:
            return None

        ref_type = 'default'
        ref = None
        if len(parts) >= 3 and parts[1] in ('branch', 'tag') and parts[2]:
            ref_type = parts[1]
            ref = parts[2]

        return {
            'type': 'github',
            'repo': repo,
            'ref_type': ref_type,
            'ref': ref
        }

    def _wordpress_source(self, version='latest'):
        """Return durable WordPress.org source metadata."""
        return {
            'type': 'wordpress',
            'version': version or 'latest'
        }

    def _load_baseline(self):
        """Read baseline metadata, returning an empty dict when unavailable."""
        baseline_file = f"{self.config_dir}/baseline.json"
        if not os.path.exists(baseline_file):
            Log.debug(self.app, f"Baseline file not found: {baseline_file}")
            return {}

        try:
            with open(baseline_file, 'r') as f:
                return json.load(f)
        except ValueError as e:
            Log.warn(self.app, f"Invalid baseline JSON in {baseline_file}: {e}")
            return {}
        except OSError as e:
            Log.warn(self.app, f"Unable to read baseline file {baseline_file}: {e}")
            return {}

    def _resolve_source(self, kind, slug, config=None, baseline=None):
        """Resolve a source dict for a plugin or theme slug."""
        suffix = 'plugins' if kind == 'plugin' else 'themes'
        if baseline is None:
            baseline = self._load_baseline()
        if config is None:
            config = MTFunctions.load_config(self.app)

        source = (baseline.get('sources') or {}).get(suffix, {}).get(slug)
        if source:
            return source

        github_sources = config.get('github_' + suffix, {})
        if slug in github_sources:
            parsed = self._parse_github_source(github_sources[slug])
            if parsed:
                return parsed

        url_sources = config.get('url_' + suffix, {})
        if slug in url_sources:
            return {
                'type': 'url',
                'url': url_sources[slug]
            }

        wordpress_sources = config.get('wordpress_' + suffix, {})
        if slug in wordpress_sources:
            return self._wordpress_source(wordpress_sources[slug])

        if kind == 'plugin':
            legacy = config.get('baseline_plugins', [])
            if isinstance(legacy, str):
                legacy = [item.strip() for item in legacy.split(',')]
            else:
                legacy = [item.strip() for item in legacy if item]
            if slug in legacy:
                return self._wordpress_source('latest')
        elif config.get('baseline_theme') == slug:
            return self._wordpress_source('latest')

        return None
    
    def create_directory_structure(self):
        """Create shared directory structure"""
        directories = [
            self.shared_root,
            self.releases_dir,
            self.wp_content_dir,
            f"{self.wp_content_dir}/plugins",
            f"{self.wp_content_dir}/themes",
            f"{self.wp_content_dir}/mu-plugins",
            f"{self.wp_content_dir}/languages",
            f"{self.wp_content_dir}/languages/plugins",
            f"{self.wp_content_dir}/languages/themes",
            self.config_dir,
            f"{self.shared_root}/backups"
        ]
        
        for directory in directories:
            os.makedirs(directory, exist_ok=True)
            Log.debug(self.app, f"Created directory: {directory}")
    
    def download_wordpress_core(self, wp_version=None):
        """Download WordPress core"""
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        release_name = f"wp-{timestamp}"
        release_path = f"{self.releases_dir}/{release_name}"
        
        version_arg = (wp_version or '').strip()
        # Download WordPress using WP-CLI
        cmd = [
            'wp', 'core', 'download',
            f'--path={release_path}',
            '--skip-content',  # Don't download default themes/plugins
            '--allow-root'
        ]
        if version_arg and version_arg.lower() != 'latest':
            cmd.append(f'--version={version_arg}')
        
        try:
            if version_arg and version_arg.lower() != 'latest':
                Log.debug(self.app, f"Downloading WordPress {version_arg} to {release_path}")
            else:
                Log.debug(self.app, f"Downloading WordPress to {release_path}")
            subprocess.run(cmd, check=True, capture_output=True)
            
            # Remove wp-content and create symlink to shared
            wp_content_path = f"{release_path}/wp-content"
            if os.path.exists(wp_content_path):
                shutil.rmtree(wp_content_path)
            os.symlink(self.wp_content_dir, wp_content_path)
            
            # Remove sample config
            sample_config = f"{release_path}/wp-config-sample.php"
            if os.path.exists(sample_config):
                os.remove(sample_config)
            
            # Create router wp-config.php (required by WordPress)
            self.create_router_wp_config(release_path)
            
            Log.debug(self.app, f"WordPress downloaded: {release_name}")
            return release_name
            
        except subprocess.CalledProcessError as e:
            # Never leave a partial release dir behind: it would displace a
            # promoted release in retention and confuse rollback.
            shutil.rmtree(release_path, ignore_errors=True)
            Log.error(self.app, f"Failed to download WordPress: {e}")
            raise
    
    def create_router_wp_config(self, release_path):
        """Create a router wp-config.php in shared WordPress core that loads site-specific configs"""
        router_config = '''<?php
/**
 * WordPress Multi-tenancy Router Configuration
 * This file is required by WordPress and loads site-specific configs
 * Following HandPressed pattern: wp-config.php in shared core loads real config
 */

// Get the document root to determine which site is being accessed
$doc_root = $_SERVER['DOCUMENT_ROOT'] ?? '';

// Determine site config path from document root
// Document root is /var/www/DOMAIN/htdocs, so go up one level and into htdocs
if ($doc_root && strpos($doc_root, '/var/www/') === 0) {
    // Extract domain from document root: /var/www/DOMAIN/htdocs
    if (preg_match('#^/var/www/([^/]+)/htdocs#', $doc_root, $matches)) {
        $domain = $matches[1];
        $site_config = $doc_root . '/wp-config.php';
        
        if (file_exists($site_config)) {
            // Load site-specific configuration (defines ABSPATH, constants, etc.)
            require_once $site_config;
            
            // Now load WordPress (required to be in this file for WP-CLI)
            if (defined('ABSPATH')) {
                /** Sets up WordPress vars and included files. */
                require_once ABSPATH . 'wp-settings.php';
            }
            return;
        }
    }
}

// Fallback for WP-CLI context
if (defined('WP_CLI') && WP_CLI) {
    $cwd = getcwd();
    if (preg_match('#^/var/www/([^/]+)/htdocs#', $cwd, $matches)) {
        $site_config = $cwd . '/wp-config.php';
        if (file_exists($site_config)) {
            require_once $site_config;
            // Load WordPress
            if (defined('ABSPATH')) {
                require_once ABSPATH . 'wp-settings.php';
            }
            return;
        }
    }
    WP_CLI::error('Site configuration not found. Run wp commands from: cd /var/www/DOMAIN/htdocs && wp ...');
}

// Error fallback
$error_msg = 'WordPress configuration not found. Document root: ' . htmlspecialchars($doc_root ?? 'unknown');
header('HTTP/1.1 503 Service Temporarily Unavailable');
header('Status: 503 Service Temporarily Unavailable');
die($error_msg);
'''
        
        router_path = f"{release_path}/wp-config.php"
        with open(router_path, 'w') as f:
            f.write(router_config)
        os.chmod(router_path, 0o644)
        Log.debug(self.app, f"Created router wp-config.php in {release_path}")
    
    def seed_plugins_and_themes(self, config, force=False):
        """Download initial plugins and themes.

        Returns a list of human-readable identifiers for items that failed to
        download, so callers can surface them. An empty list means everything
        succeeded (or there was nothing to seed).

        When force=True, plugin/theme directories that already exist are
        re-downloaded and replaced (backing up the previous copy), so
        ``init --force`` refreshes assets already on disk. force=False leaves
        existing assets untouched (the default for a first-time init).
        """
        failures = []

        # Download WordPress.org plugin sources. New configs use
        # [wordpress_plugins]; legacy configs fall back to baseline_plugins.
        if 'wordpress_plugins' in config:
            plugin_versions = dict(config.get('wordpress_plugins', {}))
        else:
            legacy = config.get('baseline_plugins',
                                ['nginx-helper', 'redis-cache'])
            if isinstance(legacy, str):
                legacy = [p.strip() for p in legacy.split(',')]
            plugin_versions = {p: None for p in legacy}
        plugin_versions = {p: v for p, v in plugin_versions.items() if p}

        github_plugins = config.get('github_plugins', {})
        url_plugins = config.get('url_plugins', {})
        for plugin, version in plugin_versions.items():
            if plugin in github_plugins or plugin in url_plugins:
                continue  # provided by a GitHub/URL source below
            if not self.download_plugin(plugin, version=version or 'latest',
                                        force=force):
                failures.append(f"plugin '{plugin}' (WordPress.org)")

        # Download GitHub plugins
        if github_plugins:
            for plugin_slug, repo_info in github_plugins.items():
                if isinstance(repo_info, str):
                    parsed = self._parse_github_source(repo_info)
                    if not parsed:
                        continue

                    github_repo = parsed['repo']
                    branch = parsed['ref'] if parsed['ref_type'] == 'branch' else None
                    tag = parsed['ref'] if parsed['ref_type'] == 'tag' else None

                    if branch:
                        ok = self.download_plugin_from_github(github_repo, plugin_slug, branch=branch, force=force)
                    elif tag:
                        ok = self.download_plugin_from_github(github_repo, plugin_slug, tag=tag, force=force)
                    else:
                        ok = self.download_plugin_from_github(github_repo, plugin_slug, force=force)
                    if not ok:
                        failures.append(f"plugin '{plugin_slug}' (GitHub {github_repo})")

        # Download WordPress.org theme sources. New configs use
        # [wordpress_themes]; legacy configs fall back to baseline_theme.
        github_themes = config.get('github_themes', {})
        url_themes = config.get('url_themes', {})
        if 'wordpress_themes' in config:
            theme_versions = dict(config.get('wordpress_themes', {}))
        else:
            theme_versions = {
                config.get('baseline_theme', 'twentytwentyfour'): None
            }
        theme_versions = {t: v for t, v in theme_versions.items() if t}
        for theme, version in theme_versions.items():
            if theme in github_themes or theme in url_themes:
                continue  # provided by a GitHub/URL source below
            if not self.download_theme(theme, version=version or 'latest',
                                       force=force):
                failures.append(f"theme '{theme}' (WordPress.org)")

        # Download GitHub themes
        if github_themes:
            for theme_slug, repo_info in github_themes.items():
                if isinstance(repo_info, str):
                    parsed = self._parse_github_source(repo_info)
                    if not parsed:
                        continue

                    github_repo = parsed['repo']
                    branch = parsed['ref'] if parsed['ref_type'] == 'branch' else None
                    tag = parsed['ref'] if parsed['ref_type'] == 'tag' else None

                    if branch:
                        ok = self.download_theme_from_github(github_repo, theme_slug, branch=branch, force=force)
                    elif tag:
                        ok = self.download_theme_from_github(github_repo, theme_slug, tag=tag, force=force)
                    else:
                        ok = self.download_theme_from_github(github_repo, theme_slug, force=force)
                    if not ok:
                        failures.append(f"theme '{theme_slug}' (GitHub {github_repo})")

        # Download URL plugins
        if url_plugins:
            for plugin_slug, url in url_plugins.items():
                if isinstance(url, str):
                    if not self.download_plugin_from_url(url, plugin_slug, force=force):
                        failures.append(f"plugin '{plugin_slug}' (URL)")

        # Download URL themes
        if url_themes:
            for theme_slug, url in url_themes.items():
                if isinstance(url, str):
                    if not self.download_theme_from_url(url, theme_slug, force=force):
                        failures.append(f"theme '{theme_slug}' (URL)")

        return failures
    
    def _asset_parent(self, kind):
        """Return the live shared parent dir for plugins or themes."""
        sub = 'plugins' if kind == 'plugin' else 'themes'
        return f"{self.wp_content_dir}/{sub}"

    def _promote_asset(self, kind, slug, staged_dir, force=False, backup_records=None):
        """Move staged_dir into the live shared asset path, backing up an
        existing target when force=True. A promotion record is appended only
        after the staged dir is successfully renamed into the target."""
        target = f"{self._asset_parent(kind)}/{slug}"

        if os.path.exists(target) and not force:
            shutil.rmtree(staged_dir, ignore_errors=True)
            Log.debug(self.app, f"{kind} {slug} already exists, skipping replace")
            return True

        backup = None
        if os.path.exists(target):
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            backup = f"{self.shared_root}/backups/assets/{stamp}/{kind}s/{slug}"
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            try:
                os.rename(target, backup)
            except OSError as e:
                Log.warn(self.app, f"Could not back up existing {kind} {slug}: {e}")
                shutil.rmtree(staged_dir, ignore_errors=True)
                return False

        try:
            os.rename(staged_dir, target)
        except OSError as e:
            Log.warn(self.app, f"Could not promote {kind} {slug}: {e}")
            if os.path.exists(target):
                shutil.rmtree(target, ignore_errors=True)
            if backup is not None:
                try:
                    os.rename(backup, target)
                except OSError as e2:
                    Log.warn(self.app, f"Could not restore backup for {kind} {slug}: {e2}")
            return False

        if backup_records is not None:
            backup_records.append({'kind': kind, 'slug': slug, 'target': target, 'backup': backup})
        return True

    def restore_asset_backups(self, backup_records):
        """Reverse successful _promote_asset() records in reverse order.
        Return True only if every restore succeeded."""
        all_ok = True
        for record in reversed(backup_records or []):
            target = record.get('target')
            backup = record.get('backup')
            try:
                if os.path.islink(target) or os.path.isfile(target):
                    os.unlink(target)
                elif os.path.isdir(target):
                    shutil.rmtree(target)
            except OSError as e:
                Log.warn(self.app, f"Could not remove {target} during restore: {e}")
                all_ok = False
                continue
            if backup is not None:
                if os.path.exists(backup):
                    try:
                        os.rename(backup, target)
                    except OSError as e:
                        Log.warn(self.app, f"Could not restore backup {backup}: {e}")
                        all_ok = False
                else:
                    Log.warn(self.app, f"Backup missing for {target}: {backup}")
                    all_ok = False
        return all_ok

    def prune_asset_backups(self, keep):
        """Keep only the ``keep`` most recent backups of each plugin/theme under
        backups/assets/, deleting older ones and any stamp dirs left empty.

        Backups live at backups/assets/<stamp>/<plugins|themes>/<slug>; the
        stamp (…-%f) sorts chronologically, so retention is per-asset, newest
        first. keep<0 disables pruning; keep=0 removes every backup."""
        if keep is None or keep < 0:
            return
        root = f"{self.shared_root}/backups/assets"
        if not os.path.isdir(root):
            return
        groups = {}
        for stamp in os.listdir(root):
            stamp_dir = os.path.join(root, stamp)
            if not os.path.isdir(stamp_dir):
                continue
            for kinds in os.listdir(stamp_dir):
                kinds_dir = os.path.join(stamp_dir, kinds)
                if not os.path.isdir(kinds_dir):
                    continue
                for slug in os.listdir(kinds_dir):
                    groups.setdefault((kinds, slug), []).append(
                        (stamp, os.path.join(kinds_dir, slug)))
        for entries in groups.values():
            entries.sort(reverse=True)  # newest stamp first
            for _, path in entries[keep:]:
                shutil.rmtree(path, ignore_errors=True)
        # Drop stamp/kind directories left empty by pruning.
        for stamp in os.listdir(root):
            stamp_dir = os.path.join(root, stamp)
            if not os.path.isdir(stamp_dir):
                continue
            for kinds in list(os.listdir(stamp_dir)):
                kinds_dir = os.path.join(stamp_dir, kinds)
                if os.path.isdir(kinds_dir) and not os.listdir(kinds_dir):
                    try:
                        os.rmdir(kinds_dir)
                    except OSError:
                        pass
            if not os.listdir(stamp_dir):
                try:
                    os.rmdir(stamp_dir)
                except OSError:
                    pass

    def _dispatch_download(self, kind, slug, source, force=False, backup_records=None):
        """Call the correct download_* helper for a resolved source dict."""
        stype = (source or {}).get('type')
        if kind == 'plugin':
            if stype == 'wordpress':
                return self.download_plugin(slug, version=source.get('version', 'latest'),
                                            force=force, backup_records=backup_records)
            if stype == 'github':
                kwargs = {}
                if source.get('ref_type') == 'branch' and source.get('ref'):
                    kwargs['branch'] = source['ref']
                elif source.get('ref_type') == 'tag' and source.get('ref'):
                    kwargs['tag'] = source['ref']
                return self.download_plugin_from_github(source['repo'], slug, force=force,
                                                        backup_records=backup_records, **kwargs)
            if stype == 'url':
                return self.download_plugin_from_url(source['url'], slug,
                                                     force=force, backup_records=backup_records)
        else:
            if stype == 'wordpress':
                return self.download_theme(slug, version=source.get('version', 'latest'),
                                           force=force, backup_records=backup_records)
            if stype == 'github':
                kwargs = {}
                if source.get('ref_type') == 'branch' and source.get('ref'):
                    kwargs['branch'] = source['ref']
                elif source.get('ref_type') == 'tag' and source.get('ref'):
                    kwargs['tag'] = source['ref']
                return self.download_theme_from_github(source['repo'], slug, force=force,
                                                       backup_records=backup_records, **kwargs)
            if stype == 'url':
                return self.download_theme_from_url(source['url'], slug,
                                                    force=force, backup_records=backup_records)
        Log.error(self.app, f"Unknown source type for {kind} {slug}: {stype}", exit=False)
        return False

    def download_plugin(self, plugin_slug, version='latest', force=False, backup_records=None):
        """Download a plugin from WordPress.org.

        Returns True on success or if the plugin is already present, False if
        the download or extraction failed.
        """
        plugin_dir = f"{self.wp_content_dir}/plugins/{plugin_slug}"

        if os.path.exists(plugin_dir) and not force:
            return True

        os.makedirs(f"{self.shared_root}/tmp/assets", exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix=f"wo_plugin_{plugin_slug}_", dir=f"{self.shared_root}/tmp/assets")
        try:
            if not version or version == 'latest':
                plugin_url = f"https://downloads.wordpress.org/plugin/{plugin_slug}.latest-stable.zip"
            else:
                plugin_url = f"https://downloads.wordpress.org/plugin/{plugin_slug}.{version}.zip"
            zip_file = f"{temp_dir}/{plugin_slug}.zip"

            download_cmd = ['curl', '-L', '-o', zip_file, plugin_url]
            result = subprocess.run(download_cmd, capture_output=True, text=True, check=False)

            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Plugin download failed for: {plugin_slug}")
                return False

            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            res = subprocess.run(unzip_cmd, capture_output=True, check=False)
            if res.returncode != 0:
                Log.debug(self.app, f"Plugin extraction failed for: {plugin_slug}")
                return False

            extracted_plugin = f"{temp_dir}/{plugin_slug}"
            if not os.path.exists(extracted_plugin):
                Log.debug(self.app, f"Plugin extraction failed for: {plugin_slug}")
                return False

            Log.debug(self.app, f"Downloaded plugin: {plugin_slug}")
            return self._promote_asset('plugin', plugin_slug, extracted_plugin, force=force, backup_records=backup_records)
        except Exception as e:
            Log.debug(self.app, f"Could not download plugin {plugin_slug}: {e}")
            return False
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def download_theme(self, theme_slug, version='latest', force=False, backup_records=None):
        """Download a theme from WordPress.org.

        Returns True on success or if the theme is already present, False if the
        download or extraction failed.
        """
        theme_dir = f"{self.wp_content_dir}/themes/{theme_slug}"

        if os.path.exists(theme_dir) and not force:
            return True

        os.makedirs(f"{self.shared_root}/tmp/assets", exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix=f"wo_theme_{theme_slug}_", dir=f"{self.shared_root}/tmp/assets")
        try:
            Log.debug(self.app, f"Downloading theme: {theme_slug}")

            if not version or version == 'latest':
                theme_url = f"https://downloads.wordpress.org/theme/{theme_slug}.latest-stable.zip"
            else:
                theme_url = f"https://downloads.wordpress.org/theme/{theme_slug}.{version}.zip"
            zip_file = f"{temp_dir}/{theme_slug}.zip"

            download_cmd = ['curl', '-L', '-o', zip_file, theme_url]
            result = subprocess.run(download_cmd, capture_output=True, text=True, check=False)

            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Theme download failed for: {theme_slug}")
                return False

            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            res = subprocess.run(unzip_cmd, capture_output=True, check=False)
            if res.returncode != 0:
                Log.debug(self.app, f"Theme extraction failed for: {theme_slug}")
                return False

            extracted_theme = f"{temp_dir}/{theme_slug}"
            if not os.path.exists(extracted_theme):
                Log.debug(self.app, f"Theme extraction failed for: {theme_slug}")
                return False

            Log.debug(self.app, f"Downloaded theme: {theme_slug}")
            return self._promote_asset('theme', theme_slug, extracted_theme, force=force, backup_records=backup_records)
        except Exception as e:
            Log.debug(self.app, f"Could not download theme {theme_slug}: {e}")
            return False
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


    # ==========================================
    # PHASE 3: GitHub Download Support
    # ==========================================
    
    def _get_github_token(self):
        """Resolve a token for authenticated GitHub downloads (private repos).

        Order: GH_TOKEN / GITHUB_TOKEN environment variables, then the GitHub
        CLI ('gh auth token'). Returns None when no token is available, in
        which case downloads stay unauthenticated (public repos only). Cached
        for the lifetime of this instance.
        """
        if hasattr(self, '_github_token'):
            return self._github_token
        token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not token:
            try:
                proc = subprocess.run(
                    ['gh', 'auth', 'token'],
                    capture_output=True, text=True, timeout=10)
                if proc.returncode == 0:
                    token = proc.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        self._github_token = token or None
        return self._github_token

    def _github_default_branch(self, repo):
        """Resolve a repository's default branch via the GitHub API.

        Returns the branch name or None on any failure; uses the same token
        resolution as _github_curl.
        """
        import requests
        headers = {}
        token = self._get_github_token()
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}",
                             headers=headers, timeout=10)
            if r.ok:
                return r.json().get('default_branch') or None
        except Exception as e:
            Log.debug(self.app, f"GitHub default-branch lookup failed: {e}")
        return None

    def _github_curl(self, url, zip_file):
        """Download `url` to `zip_file` with curl, authenticating with a GitHub
        token when one is available.

        '--fail' (-f) makes curl return non-zero on an HTTP error instead of
        writing the error page into the zip (which would only fail later at
        unzip). The Authorization header is passed via curl's stdin config
        ('-K -') so the token never appears in the process argument list.
        """
        cmd = ['curl', '-fL', '-o', zip_file, url]
        token = self._get_github_token()
        curl_input = None
        if token:
            cmd += ['-K', '-']
            curl_input = f'header = "Authorization: Bearer {token}"\n'
        return subprocess.run(
            cmd, capture_output=True, text=True, input=curl_input)

    def download_plugin_from_github(self, github_repo, plugin_slug, branch=None, tag=None, force=False, backup_records=None):
        """
        Download a plugin from a GitHub repository
        
        This method downloads a plugin from GitHub and extracts it to the shared plugins directory.
        It supports both branch and tag-based downloads, with automatic fallback from main to master.
        
        Args:
            github_repo (str): GitHub repository in 'user/repo' format
            plugin_slug (str): Local directory name for the plugin
            branch (str, optional): Specific branch to download (e.g., 'develop', 'main')
            tag (str, optional): Specific tag/release to download (e.g., 'v1.2.3', '1.0.0')
                                Tags take precedence over branches if both are specified
        
        Returns:
            bool: True if download and extraction successful, False otherwise
        
        Example:
            infra.download_plugin_from_github('user/my-plugin', 'my-plugin', tag='v1.5.0')
        """
        plugin_dir = f"{self.wp_content_dir}/plugins/{plugin_slug}"
        
        if os.path.exists(plugin_dir) and not force:
            Log.debug(self.app, f"Plugin {plugin_slug} already exists, skipping download")
            return True
        
        try:
            # Construct the appropriate GitHub archive URL
            # GitHub archive URLs follow this pattern:
            # - For tags: https://github.com/user/repo/archive/refs/tags/TAG.zip
            # - For branches: https://github.com/user/repo/archive/refs/heads/BRANCH.zip
            if tag:
                # Download specific tag/release version
                url = f"https://github.com/{github_repo}/archive/refs/tags/{tag}.zip"
                Log.debug(self.app, f"Downloading from GitHub tag: {tag}")
            elif branch:
                # Download specific branch
                url = f"https://github.com/{github_repo}/archive/refs/heads/{branch}.zip"
                Log.debug(self.app, f"Downloading from GitHub branch: {branch}")
            else:
                # Resolved below: API default branch, then main -> master.
                url = None
            
            # Create temporary directory for download and extraction
            os.makedirs(f"{self.shared_root}/tmp/assets", exist_ok=True)
            temp_dir = tempfile.mkdtemp(prefix=f"wo_plugin_{plugin_slug}_", dir=f"{self.shared_root}/tmp/assets")
            try:
                zip_file = f"{temp_dir}/{plugin_slug}.zip"
                
                # Download the zip (authenticated when a GitHub token is available)
                if url:
                    result = self._github_curl(url, zip_file)
                else:
                    candidates = []
                    for cand in (self._github_default_branch(github_repo),
                                 'main', 'master'):
                        if cand and cand not in candidates:
                            candidates.append(cand)
                    for cand in candidates:
                        url = (f"https://github.com/{github_repo}"
                               f"/archive/refs/heads/{cand}.zip")
                        Log.debug(self.app,
                                  f"Downloading from GitHub branch: {cand}")
                        result = self._github_curl(url, zip_file)
                        if result.returncode == 0:
                            break
                
                # Verify download was successful
                if result.returncode != 0 or not os.path.exists(zip_file):
                    Log.debug(self.app, f"Failed to download from GitHub: {github_repo}")
                    return False
                
                # Extract the downloaded zip file
                unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
                result = subprocess.run(unzip_cmd, capture_output=True)
                
                if result.returncode != 0:
                    Log.debug(self.app, "Failed to extract GitHub archive")
                    return False
                
                # Find the extracted directory
                # GitHub creates a directory named like: repo-branch or repo-tag
                # We need to find it and rename it to the plugin_slug
                extracted = None
                for item in os.listdir(temp_dir):
                    item_path = f"{temp_dir}/{item}"
                    # Skip __MACOSX and the zip file itself
                    if os.path.isdir(item_path) and item not in ['__MACOSX', plugin_slug]:
                        extracted = item_path
                        break
                
                if not extracted:
                    Log.debug(self.app, "No directory found in GitHub archive")
                    return False
                
                Log.debug(self.app, f"Successfully downloaded plugin from GitHub: {github_repo}")
                return self._promote_asset('plugin', plugin_slug, extracted, force=force, backup_records=backup_records)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            Log.debug(self.app, f"GitHub plugin download failed: {e}")
            return False
    
    def download_plugin_from_url(self, url, plugin_slug, force=False, backup_records=None):
        """
        Download a plugin from a direct URL
        
        This method downloads a plugin zip file from any URL and extracts it.
        Useful for premium plugins, custom builds, or plugins not on WordPress.org/GitHub.
        
        Args:
            url (str): Direct URL to the plugin zip file
            plugin_slug (str): Local directory name for the plugin
        
        Returns:
            bool: True if download and extraction successful, False otherwise
        
        Example:
            infra.download_plugin_from_url('https://example.com/my-plugin.zip', 'my-plugin')
        """
        plugin_dir = f"{self.wp_content_dir}/plugins/{plugin_slug}"
        
        if os.path.exists(plugin_dir) and not force:
            Log.debug(self.app, f"Plugin {plugin_slug} already exists, skipping download")
            return True
        
        os.makedirs(f"{self.shared_root}/tmp/assets", exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix=f"wo_plugin_{plugin_slug}_", dir=f"{self.shared_root}/tmp/assets")
        try:
            zip_file = f"{temp_dir}/{plugin_slug}.zip"
            
            Log.debug(self.app, f"Downloading plugin from URL: {url}")
            
            # Download the file using curl with follow redirects
            download_cmd = ['curl', '-L', '-o', zip_file, url]
            result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # Verify download was successful
            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Failed to download from URL: {url}")
                return False
            
            # Verify it's actually a zip file (check magic bytes)
            with open(zip_file, 'rb') as f:
                magic = f.read(4)
                # ZIP files start with 'PK\x03\x04' or 'PK\x05\x06' (empty archive)
                if not (magic[:2] == b'PK'):
                    Log.debug(self.app, "Downloaded file is not a valid ZIP archive")
                    return False
            
            # Extract the zip file
            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            result = subprocess.run(unzip_cmd, capture_output=True)
            
            if result.returncode != 0:
                Log.debug(self.app, "Failed to extract plugin zip")
                return False
            
            # Find the extracted directory
            # Most plugins extract to a single directory, but some might have different structures
            extracted = None
            for item in os.listdir(temp_dir):
                item_path = f"{temp_dir}/{item}"
                # Skip the zip file and __MACOSX
                if os.path.isdir(item_path) and item not in ['__MACOSX']:
                    extracted = item_path
                    break
            
            if not extracted:
                # Maybe it's a single-file plugin or flat structure
                # In this case, create a staged directory and move all top-level files there
                Log.debug(self.app, "No plugin directory found, checking for flat structure")
                php_files = [f for f in os.listdir(temp_dir) if f.endswith('.php')]
                if php_files:
                    staged = tempfile.mkdtemp(prefix=f"staged_{plugin_slug}_", dir=temp_dir)
                    for item in os.listdir(temp_dir):
                        if item == plugin_slug + '.zip':
                            continue
                        src = f"{temp_dir}/{item}"
                        dst = f"{staged}/{item}"
                        if os.path.isfile(src):
                            shutil.move(src, dst)
                    Log.debug(self.app, f"Downloaded plugin from URL (flat structure)")
                    return self._promote_asset('plugin', plugin_slug, staged, force=force, backup_records=backup_records)
                Log.debug(self.app, "No valid plugin structure found in archive")
                return False
            
            Log.debug(self.app, f"Successfully downloaded plugin from URL")
            return self._promote_asset('plugin', plugin_slug, extracted, force=force, backup_records=backup_records)
        except Exception as e:
            Log.debug(self.app, f"URL plugin download failed: {e}")
            return False
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    def download_theme_from_github(self, github_repo, theme_slug, branch=None, tag=None, force=False, backup_records=None):
        """
        Download a theme from a GitHub repository
        
        Similar to download_plugin_from_github but for themes.
        Downloads and extracts to the shared themes directory.
        
        Args:
            github_repo (str): GitHub repository in 'user/repo' format
            theme_slug (str): Local directory name for the theme
            branch (str, optional): Specific branch to download
            tag (str, optional): Specific tag/release to download (takes precedence)
        
        Returns:
            bool: True if download and extraction successful, False otherwise
        """
        theme_dir = f"{self.wp_content_dir}/themes/{theme_slug}"
        
        if os.path.exists(theme_dir) and not force:
            Log.debug(self.app, f"Theme {theme_slug} already exists, skipping download")
            return True
        
        try:
            # Construct GitHub URL (same logic as plugins)
            if tag:
                url = f"https://github.com/{github_repo}/archive/refs/tags/{tag}.zip"
                Log.debug(self.app, f"Downloading theme from GitHub tag: {tag}")
            elif branch:
                url = f"https://github.com/{github_repo}/archive/refs/heads/{branch}.zip"
                Log.debug(self.app, f"Downloading theme from GitHub branch: {branch}")
            else:
                # Resolved below: API default branch, then main -> master.
                url = None
            
            # Create temporary directory
            os.makedirs(f"{self.shared_root}/tmp/assets", exist_ok=True)
            temp_dir = tempfile.mkdtemp(prefix=f"wo_theme_{theme_slug}_", dir=f"{self.shared_root}/tmp/assets")
            try:
                zip_file = f"{temp_dir}/{theme_slug}.zip"
                
                # Download
                if url:
                    result = self._github_curl(url, zip_file)
                else:
                    candidates = []
                    for cand in (self._github_default_branch(github_repo),
                                 'main', 'master'):
                        if cand and cand not in candidates:
                            candidates.append(cand)
                    for cand in candidates:
                        url = (f"https://github.com/{github_repo}"
                               f"/archive/refs/heads/{cand}.zip")
                        Log.debug(self.app, "Downloading theme from GitHub "
                                            f"branch: {cand}")
                        result = self._github_curl(url, zip_file)
                        if result.returncode == 0:
                            break
                
                # Verify download
                if result.returncode != 0 or not os.path.exists(zip_file):
                    Log.debug(self.app, f"Failed to download theme from GitHub: {github_repo}")
                    return False
                
                # Extract
                unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
                result = subprocess.run(unzip_cmd, capture_output=True)
                
                if result.returncode != 0:
                    Log.debug(self.app, "Failed to extract GitHub theme archive")
                    return False
                
                # Find extracted directory
                extracted = None
                for item in os.listdir(temp_dir):
                    item_path = f"{temp_dir}/{item}"
                    if os.path.isdir(item_path) and item not in ['__MACOSX', theme_slug]:
                        extracted = item_path
                        break
                
                if not extracted:
                    Log.debug(self.app, "No directory found in GitHub theme archive")
                    return False
                
                Log.debug(self.app, f"Successfully downloaded theme from GitHub: {github_repo}")
                return self._promote_asset('theme', theme_slug, extracted, force=force, backup_records=backup_records)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as e:
            Log.debug(self.app, f"GitHub theme download failed: {e}")
            return False
    
    def download_theme_from_url(self, url, theme_slug, force=False, backup_records=None):
        """
        Download a theme from a direct URL
        
        Downloads a theme zip file from any URL and extracts it to shared themes directory.
        
        Args:
            url (str): Direct URL to the theme zip file
            theme_slug (str): Local directory name for the theme
        
        Returns:
            bool: True if download and extraction successful, False otherwise
        """
        theme_dir = f"{self.wp_content_dir}/themes/{theme_slug}"
        
        if os.path.exists(theme_dir) and not force:
            Log.debug(self.app, f"Theme {theme_slug} already exists, skipping download")
            return True
        
        os.makedirs(f"{self.shared_root}/tmp/assets", exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix=f"wo_theme_{theme_slug}_", dir=f"{self.shared_root}/tmp/assets")
        try:
            zip_file = f"{temp_dir}/{theme_slug}.zip"
            
            Log.debug(self.app, f"Downloading theme from URL: {url}")
            
            # Download
            download_cmd = ['curl', '-L', '-o', zip_file, url]
            result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # Verify download
            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Failed to download theme from URL: {url}")
                return False
            
            # Verify it's a zip file
            with open(zip_file, 'rb') as f:
                magic = f.read(4)
                if not (magic[:2] == b'PK'):
                    Log.debug(self.app, "Downloaded file is not a valid ZIP archive")
                    return False
            
            # Extract
            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            result = subprocess.run(unzip_cmd, capture_output=True)
            
            if result.returncode != 0:
                Log.debug(self.app, "Failed to extract theme zip")
                return False
            
            # Find extracted directory
            extracted = None
            for item in os.listdir(temp_dir):
                item_path = f"{temp_dir}/{item}"
                if os.path.isdir(item_path) and item not in ['__MACOSX']:
                    extracted = item_path
                    break
            
            if not extracted:
                Log.debug(self.app, "No theme directory found in archive, checking for flat structure")
                staged = tempfile.mkdtemp(prefix=f"staged_{theme_slug}_", dir=temp_dir)
                moved = False
                for item in os.listdir(temp_dir):
                    if item == theme_slug + '.zip':
                        continue
                    src = f"{temp_dir}/{item}"
                    dst = f"{staged}/{item}"
                    if os.path.isfile(src):
                        shutil.move(src, dst)
                        moved = True
                if moved:
                    Log.debug(self.app, f"Downloaded theme from URL (flat structure)")
                    return self._promote_asset('theme', theme_slug, staged, force=force, backup_records=backup_records)
                Log.debug(self.app, "No valid theme structure found in archive")
                return False
            
            Log.debug(self.app, f"Successfully downloaded theme from URL")
            return self._promote_asset('theme', theme_slug, extracted, force=force, backup_records=backup_records)
        except Exception as e:
            Log.debug(self.app, f"URL theme download failed: {e}")
            return False
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    # ==========================================
    # PHASE 3: Helper Methods
    # ==========================================
    
    @staticmethod
    def get_plugin_version(plugin_dir, plugin_slug):
        """
        Extract version number from plugin's main PHP file
        
        WordPress plugins declare their version in a header comment like:
        * Version: 1.2.3
        
        This method searches for that header and extracts the version.
        
        Args:
            plugin_dir (str): Full path to plugin directory
            plugin_slug (str): Plugin slug (used to find main PHP file)
        
        Returns:
            str: Version string like "(v1.2.3)" or empty string if not found
        """
        try:
            # Common patterns for main plugin file
            candidates = [
                f"{plugin_dir}/{plugin_slug}.php",
                f"{plugin_dir}/index.php",
                f"{plugin_dir}/plugin.php"
            ]
            
            # Try each candidate file
            for candidate in candidates:
                if os.path.exists(candidate):
                    # Read first 2KB of file (headers are always at the top)
                    with open(candidate, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read(2000)
                        
                        # Look for "Version: X.Y.Z" in the plugin header
                        import re
                        match = re.search(r'Version:\s*([\d.]+)', content, re.IGNORECASE)
                        if match:
                            return f"(v{match.group(1)})"
            
            return ""
            
        except Exception:
            return ""

    def create_baseline_config(self, config):
        """Bootstrap baseline configuration file if it does not exist"""
        baseline_file = f"{self.config_dir}/baseline.json"
        if os.path.exists(baseline_file):
            Log.info(
                self.app,
                "baseline.json exists — left untouched; it is the source of truth"
            )
            return True

        # baseline.json tracks plugins activated by default, not every
        # downloaded source. Legacy conf keys seed the first file only; when
        # absent, seed a one-time starting template from source sections.
        source_seeded = False
        if 'baseline_plugins' in config:
            baseline_plugins = config.get('baseline_plugins', [])
            if isinstance(baseline_plugins, str):
                baseline_plugins = [p.strip() for p in baseline_plugins.split(',')]
            all_plugins = [p for p in baseline_plugins if p]
        else:
            all_plugins = []
            seen_plugins = set()
            for section_name in ('wordpress_plugins', 'github_plugins', 'url_plugins'):
                for plugin_slug in config.get(section_name, {}).keys():
                    if plugin_slug and plugin_slug not in seen_plugins:
                        all_plugins.append(plugin_slug)
                        seen_plugins.add(plugin_slug)
            source_seeded = bool(all_plugins)

        if 'baseline_theme' in config:
            theme = config.get('baseline_theme', '')
        else:
            theme = ''
            wordpress_themes = list(config.get('wordpress_themes', {}).keys())
            github_themes = list(config.get('github_themes', {}).keys())
            url_themes = list(config.get('url_themes', {}).keys())
            if wordpress_themes:
                theme = wordpress_themes[0]
            else:
                child_themes = [slug for slug in github_themes if slug.endswith('-child')]
                if child_themes:
                    theme = child_themes[0]
                elif github_themes:
                    theme = github_themes[0]
                elif url_themes:
                    theme = url_themes[0]
            source_seeded = source_seeded or bool(theme)

        if source_seeded:
            Log.info(
                self.app,
                "Seeded baseline.json from source sections as a starting "
                "template; baseline.json is now operator-owned"
            )

        if not all_plugins and not theme:
            Log.warn(
                self.app,
                "Bootstrapping baseline.json with no plugins or theme; populate "
                "it via 'wo multitenancy add-plugin' / 'wo multitenancy set-theme' "
                "or hand-edit baseline.json"
            )

        baseline_sources = {
            'plugins': {},
            'themes': {}
        }

        legacy_plugins = config.get('baseline_plugins', [])
        if isinstance(legacy_plugins, str):
            legacy_plugins = [p.strip() for p in legacy_plugins.split(',')]
        for plugin_slug in [p for p in legacy_plugins if p]:
            baseline_sources['plugins'][plugin_slug] = self._wordpress_source('latest')

        legacy_theme = config.get('baseline_theme', '')
        if legacy_theme:
            baseline_sources['themes'][legacy_theme] = self._wordpress_source('latest')

        for plugin_slug, version in config.get('wordpress_plugins', {}).items():
            baseline_sources['plugins'][plugin_slug] = self._wordpress_source(version)

        for theme_slug, version in config.get('wordpress_themes', {}).items():
            baseline_sources['themes'][theme_slug] = self._wordpress_source(version)

        for plugin_slug, url in config.get('url_plugins', {}).items():
            baseline_sources['plugins'][plugin_slug] = {
                'type': 'url',
                'url': url
            }

        for theme_slug, url in config.get('url_themes', {}).items():
            baseline_sources['themes'][theme_slug] = {
                'type': 'url',
                'url': url
            }

        for plugin_slug, repo_info in config.get('github_plugins', {}).items():
            source = self._parse_github_source(repo_info)
            if source is not None:
                baseline_sources['plugins'][plugin_slug] = source

        for theme_slug, repo_info in config.get('github_themes', {}).items():
            source = self._parse_github_source(repo_info)
            if source is not None:
                baseline_sources['themes'][theme_slug] = source

        baseline = {
            'version': 1,
            'generated': datetime.now().isoformat(),
            'plugins': all_plugins,
            'theme': theme,
            'sources': baseline_sources,
            'options': {
                'blog_public': 1,
                'default_comment_status': 'closed',
                'default_ping_status': 'closed'
            }
        }

        with open(baseline_file, 'w') as f:
            json.dump(baseline, f, indent=2)

        Log.debug(self.app, "Created baseline configuration")
        return True
    
    def switch_release(self, release_name):
        """Switch to a specific release"""
        release_path = f"{self.releases_dir}/{release_name}"
        
        if not os.path.exists(release_path):
            raise Exception(f"Release {release_name} not found")
        
        # Ensure router wp-config.php exists (required by WordPress)
        router_config_path = f"{release_path}/wp-config.php"
        if not os.path.exists(router_config_path):
            Log.debug(self.app, f"Creating router wp-config.php for {release_name}")
            self.create_router_wp_config(release_path)
        
        current_link = f"{self.shared_root}/current"
        
        # Flip atomically: build the symlink aside, then rename over the
        # old one so `current` never transiently disappears.
        tmp_link = f"{current_link}.new"
        if os.path.islink(tmp_link) or os.path.exists(tmp_link):
            os.unlink(tmp_link)
        os.symlink(release_path, tmp_link)
        os.replace(tmp_link, current_link)
        Log.debug(self.app, f"Switched to release: {release_name}")
    
    def update_plugin(self, plugin_slug, config=None):
        """Refresh one shared plugin from its recorded source. Return True on success."""
        if not plugin_slug:
            Log.error(self.app, "Plugin slug is required", exit=False)
            return False
        if config is None:
            config = MTFunctions.load_config(self.app)
        baseline = self._load_baseline()
        source = self._resolve_source('plugin', plugin_slug, config=config, baseline=baseline)
        if not source:
            Log.error(self.app, f"No download source configured for plugin {plugin_slug}", exit=False)
            return False
        backup_records = []
        if not self._dispatch_download('plugin', plugin_slug, source, force=True, backup_records=backup_records):
            if not self.restore_asset_backups(backup_records):
                Log.warn(self.app, "Asset restore incomplete; restore "
                                   "manually from "
                                   f"{self.shared_root}/backups/assets/")
            return False
        for record in backup_records:
            if record.get('backup'):
                Log.info(self.app, f"Previous plugin backup: {record['backup']}")
        return True

    def update_theme(self, theme_slug=None, config=None):
        """Refresh one shared theme from its recorded source. Uses baseline['theme'] when slug omitted."""
        if config is None:
            config = MTFunctions.load_config(self.app)
        baseline = self._load_baseline()
        if not theme_slug:
            theme_slug = baseline.get('theme')
        if not theme_slug:
            Log.error(self.app, "No theme configured in baseline", exit=False)
            return False
        source = self._resolve_source('theme', theme_slug, config=config, baseline=baseline)
        if not source:
            Log.error(self.app, f"No download source configured for theme {theme_slug}", exit=False)
            return False
        backup_records = []
        if not self._dispatch_download('theme', theme_slug, source, force=True, backup_records=backup_records):
            if not self.restore_asset_backups(backup_records):
                Log.warn(self.app, "Asset restore incomplete; restore "
                                   "manually from "
                                   f"{self.shared_root}/backups/assets/")
            return False
        for record in backup_records:
            if record.get('backup'):
                Log.info(self.app, f"Previous theme backup: {record['backup']}")
        return True

    def update_plugins_and_themes(self, config):
        """Refresh all shared plugin/theme sources.

        Returns (success, backup_records, restore_ok). On any
        download/promote failure every promoted asset is restored;
        restore_ok reports whether that restore fully succeeded (True when
        no restore was needed). backup_records are the real promote records
        even on failure. Slugs without a resolvable source are warned and
        skipped, not failed.
        """
        baseline = self._load_baseline()
        backup_records = []

        def ordered_unique(items):
            seen = set()
            result = []
            for item in items:
                if item and item not in seen:
                    seen.add(item)
                    result.append(item)
            return result

        plugin_slugs = []
        plugin_slugs.extend(baseline.get('plugins', []) or [])
        plugin_slugs.extend((baseline.get('sources') or {}).get('plugins', {}).keys())
        plugin_slugs.extend(config.get('wordpress_plugins', {}).keys())
        plugin_slugs.extend(config.get('github_plugins', {}).keys())
        plugin_slugs.extend(config.get('url_plugins', {}).keys())
        legacy_plugins = config.get('baseline_plugins')
        if legacy_plugins:
            if isinstance(legacy_plugins, str):
                legacy_plugins = [p.strip() for p in legacy_plugins.split(',')]
            plugin_slugs.extend(legacy_plugins)
        plugin_slugs = ordered_unique(plugin_slugs)

        theme_slugs = []
        if baseline.get('theme'):
            theme_slugs.append(baseline['theme'])
        theme_slugs.extend((baseline.get('sources') or {}).get('themes', {}).keys())
        theme_slugs.extend(config.get('wordpress_themes', {}).keys())
        theme_slugs.extend(config.get('github_themes', {}).keys())
        theme_slugs.extend(config.get('url_themes', {}).keys())
        if config.get('baseline_theme'):
            theme_slugs.append(config['baseline_theme'])
        theme_slugs = ordered_unique(theme_slugs)

        failures = []
        for kind, slugs in (('plugin', plugin_slugs), ('theme', theme_slugs)):
            for slug in slugs:
                source = self._resolve_source(kind, slug, config=config, baseline=baseline)
                if not source:
                    Log.warn(self.app, f"No download source configured for {kind} {slug}; skipping")
                    continue
                if not self._dispatch_download(kind, slug, source, force=True, backup_records=backup_records):
                    failures.append(f"{kind} {slug}")

        if failures:
            restore_ok = self.restore_asset_backups(backup_records)
            for item in failures:
                Log.error(self.app, f"Failed to update {item}", exit=False)
            return (False, backup_records, restore_ok)

        return (True, backup_records, True)
    
    def set_permissions(self):
        """Set proper permissions on shared infrastructure"""
        
        # Set ownership
        try:
            subprocess.run([
                'chown', '-R', 'www-data:www-data', self.shared_root
            ], check=True, capture_output=True)
        except:
            Log.debug(self.app, "Could not set ownership")
        
        # Set directory permissions
        for root, dirs, files in os.walk(self.shared_root, followlinks=False):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)


    def initialize_git_tracking(self):
        """Initialize git repository for baseline tracking"""
        try:
            # Check if git is installed
            result = subprocess.run(
                ['git', '--version'],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                Log.debug(self.app, "Git not installed, skipping baseline tracking")
                return False
            
            # Initialize git repo if not exists
            git_dir = f"{self.shared_root}/.git"
            if not os.path.exists(git_dir):
                subprocess.run(
                    ['git', 'init'],
                    cwd=self.shared_root,
                    capture_output=True,
                    check=True
                )
                
                # Configure git (local only)
                subprocess.run(
                    ['git', 'config', 'user.name', 'WordOps Multi-tenancy'],
                    cwd=self.shared_root,
                    capture_output=True
                )
                subprocess.run(
                    ['git', 'config', 'user.email', 'multitenancy@wordops.local'],
                    cwd=self.shared_root,
                    capture_output=True
                )
                
                # Create .gitignore
                gitignore_path = f"{self.shared_root}/.gitignore"
                with open(gitignore_path, 'w') as f:
                    f.write("""# Ignore everything except baseline config
*
!.gitignore
!config/
!config/baseline.json
""")
                
                # Initial commit
                subprocess.run(
                    ['git', 'add', '.gitignore', 'config/baseline.json'],
                    cwd=self.shared_root,
                    capture_output=True
                )
                subprocess.run(
                    ['git', 'commit', '-m', 'Initial baseline configuration'],
                    cwd=self.shared_root,
                    capture_output=True
                )
                
                Log.debug(self.app, "Initialized git tracking for baseline")
                return True
            
            return True
            
        except Exception as e:
            Log.debug(self.app, f"Could not initialize git tracking: {e}")
            return False

    def git_commit_baseline(self, message):
        """Commit baseline.json changes to git"""
        try:
            git_dir = f"{self.shared_root}/.git"
            if not os.path.exists(git_dir):
                Log.debug(self.app, "Git not initialized, skipping commit")
                return False
            
            add = subprocess.run(
                ['git', 'add', 'config/baseline.json'],
                cwd=self.shared_root,
                capture_output=True,
                text=True,
            )
            if add.returncode != 0:
                Log.debug(self.app, f"Git add failed: {add.stderr}")
                return False

            commit = subprocess.run(
                ['git', 'commit', '-m', message],
                cwd=self.shared_root,
                capture_output=True,
                text=True,
            )
            if commit.returncode != 0:
                output = f"{commit.stdout}\n{commit.stderr}"
                if ('nothing to commit' in output
                        or 'nothing added to commit' in output):
                    # No staged changes — the baseline is already committed.
                    return True
                Log.debug(self.app, f"Git commit failed: {output}")
                return False

            Log.debug(self.app, f"Git commit: {message}")
            return True

        except Exception as e:
            Log.debug(self.app, f"Git commit failed: {e}")
            return False


class ReleaseManager:
    """Manage WordPress releases"""
    
    def __init__(self, app, shared_root):
        self.app = app
        self.shared_root = shared_root
        self.releases_dir = f"{shared_root}/releases"
        self.backups_dir = f"{shared_root}/backups"
    
    def list_releases(self):
        """List all available releases"""
        releases = []
        
        if os.path.exists(self.releases_dir):
            for item in os.listdir(self.releases_dir):
                if item.startswith('wp-') and os.path.isdir(f"{self.releases_dir}/{item}"):
                    releases.append(item)
        
        return sorted(releases, reverse=True)
    
    def get_current_release(self):
        """Get current active release"""
        current_link = f"{self.shared_root}/current"
        
        if os.path.islink(current_link):
            target = os.readlink(current_link)
            return os.path.basename(target)
        
        return None
    
    def get_previous_release(self, current_release):
        """Get the previous release for rollback"""
        releases = self.list_releases()
        
        if current_release in releases:
            current_index = releases.index(current_release)
            if current_index < len(releases) - 1:
                return releases[current_index + 1]
        
        return None
    
    def backup_current(self):
        """Backup current release information"""
        current = self.get_current_release()
        if current:
            timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            backup_file = f"{self.backups_dir}/release-{timestamp}.txt"
            
            os.makedirs(self.backups_dir, exist_ok=True)
            
            with open(backup_file, 'w') as f:
                f.write(current)
            
            Log.debug(self.app, f"Backed up release info: {backup_file}")
    
    def cleanup_old_releases(self, keep_count=3):
        """Remove old releases keeping only the specified count"""
        releases = self.list_releases()
        current = self.get_current_release()
        
        # Retention counts only promoted releases: ignore anything lexically
        # newer than current (leaked/staged dirs) so they can't displace a
        # promoted release; never delete current.
        if current in releases:
            releases = releases[releases.index(current):]
        keep_count = max(1, keep_count)
        for release in releases[keep_count:]:
            if release != current:
                release_path = f"{self.releases_dir}/{release}"
                if os.path.exists(release_path):
                    shutil.rmtree(release_path)
                    Log.debug(self.app, f"Removed old release: {release}")

class BaselineApplicator:
    """Helper class for applying baseline configuration to sites"""

    # WP-CLI baseline commands (plugin/theme/option/cache) use this timeout,
    # in seconds. Cold-start plugin activation during initial site creation
    # bootstraps every already-active plugin on each call (WooCommerce
    # first-run migrations, Object Cache Pro cold cache), which can transiently
    # exceed a tight limit even though steady-state calls take a few seconds.
    WP_CLI_TIMEOUT = 120
    
    @staticmethod
    def find_plugin_main_file(site_path, plugin_slug):
        """Find a plugin's main PHP file relative to the plugins directory.

        Returns the path relative to wp-content/plugins (for example
        "moyasar/moyasar-payments.php"), or None when the plugin is not
        present on disk.
        """
        plugins_root = f"{site_path}/wp-content/plugins"

        # A plugin's main file is the top-level PHP file that carries the
        # WordPress "Plugin Name:" header, exactly the way WordPress itself
        # discovers it. Requiring the header is essential: many plugins ship
        # an empty "silence is golden" index.php in their root that is NOT the
        # main file (for example madfu-payment-gateway keeps its header in
        # madfu-pay.php), and some ship their main file under a name that does
        # not match the folder (moyasar ships moyasar-payments.php).

        # Single-file plugin living directly under plugins/.
        single = f"{plugins_root}/{plugin_slug}.php"
        if os.path.isfile(single) and \
                BaselineApplicator._has_plugin_header(single):
            return f"{plugin_slug}.php"

        plugin_dir = f"{plugins_root}/{plugin_slug}"
        if not os.path.isdir(plugin_dir):
            return None

        # Prefer the conventional <slug>/<slug>.php when it carries the header.
        conventional = f"{plugin_dir}/{plugin_slug}.php"
        if os.path.isfile(conventional) and \
                BaselineApplicator._has_plugin_header(conventional):
            return f"{plugin_slug}/{plugin_slug}.php"

        # Otherwise take the first (alphabetical) top-level PHP file that has
        # the header. Header-less stubs such as an empty index.php are ignored.
        try:
            entries = sorted(os.listdir(plugin_dir))
        except OSError:
            return None
        for entry in entries:
            if not entry.endswith('.php'):
                continue
            candidate = f"{plugin_dir}/{entry}"
            if os.path.isfile(candidate) and \
                    BaselineApplicator._has_plugin_header(candidate):
                return f"{plugin_slug}/{entry}"

        return None

    @staticmethod
    def _has_plugin_header(file_path):
        """Return True if a PHP file declares a 'Plugin Name:' header.

        Mirrors WordPress core plugin detection: the metadata block lives in
        the first 8 KB of the file and the label may be preceded by comment
        characters.
        """
        try:
            with open(file_path, 'r', encoding='utf-8',
                      errors='ignore') as handle:
                head = handle.read(8192)
        except OSError:
            return False
        return re.search(r'^[ \t/*#@]*Plugin Name:', head,
                         re.IGNORECASE | re.MULTILINE) is not None
    
    @staticmethod
    def _get_active_plugin_slugs(app, site_path):
        """Return active plugin slugs for a site"""
        active_cmd = [
            'wp', 'plugin', 'list',
            '--status=active',
            '--field=name',
            '--path=' + site_path,
            '--allow-root'
        ]

        active_result = subprocess.run(
            active_cmd,
            capture_output=True,
            text=True,
            timeout=BaselineApplicator.WP_CLI_TIMEOUT
        )

        if active_result.returncode != 0:
            raise RuntimeError(
                "Failed to list active plugins: " + active_result.stderr
            )

        return [
            plugin.strip() for plugin in active_result.stdout.splitlines()
            if plugin.strip()
        ]

    @staticmethod
    def _option_value_for_wp_cli(value):
        """Convert a baseline option value to a WP-CLI argument."""
        if isinstance(value, bool):
            return '1' if value else '0', False
        if isinstance(value, (dict, list)):
            return json.dumps(value), True
        return str(value), False

    @staticmethod
    def apply_baseline_to_site(app, domain, site_path, baseline, prune=False,
                               cache_type=None):
        """Apply baseline configuration to a single site via WP-CLI"""
        
        result = {'success': False, 'error': None, 'skipped_plugins': []}
        
        try:
            # Get current active plugins (for rollback)
            get_plugins_cmd = [
                'wp', 'option', 'get', 'active_plugins',
                '--format=json',
                '--path=' + site_path,
                '--allow-root'
            ]
            
            plugins_result = subprocess.run(
                get_plugins_cmd,
                capture_output=True,
                text=True,
                timeout=BaselineApplicator.WP_CLI_TIMEOUT
            )
            
            if plugins_result.returncode != 0:
                result['error'] = "Could not read current plugins: " + plugins_result.stderr.strip()
                return result
            
            # Activate each baseline plugin. A plugin that is missing on disk
            # or fails to activate is skipped with a warning so that one bad
            # plugin never blocks the rest of the baseline (theme, options and
            # cache configuration) from being applied.
            for plugin_slug in baseline.get('plugins', []):
                plugin_file = BaselineApplicator.find_plugin_main_file(
                    site_path,
                    plugin_slug
                )

                if not plugin_file:
                    Log.warn(
                        app,
                        f"Baseline plugin {plugin_slug} not found on disk "
                        f"for {domain}; skipping"
                    )
                    result['skipped_plugins'].append(plugin_slug)
                    continue

                try:
                    activate_result = subprocess.run(
                        [
                            'wp', 'plugin', 'activate', plugin_file,
                            '--path=' + site_path,
                            '--allow-root'
                        ],
                        capture_output=True,
                        text=True,
                        timeout=BaselineApplicator.WP_CLI_TIMEOUT
                    )
                except subprocess.TimeoutExpired:
                    Log.warn(
                        app,
                        f"Timed out activating baseline plugin {plugin_slug} "
                        f"for {domain}; skipping"
                    )
                    result['skipped_plugins'].append(plugin_slug)
                    continue

                if activate_result.returncode != 0:
                    Log.warn(
                        app,
                        f"Failed to activate baseline plugin {plugin_slug} "
                        f"for {domain}; skipping: "
                        f"{activate_result.stderr.strip()}"
                    )
                    result['skipped_plugins'].append(plugin_slug)
                    continue
            
            for option_name, option_value in baseline.get('options', {}).items():
                wp_value, use_json_format = BaselineApplicator._option_value_for_wp_cli(
                    option_value
                )
                option_cmd = [
                    'wp', 'option', 'update', option_name, wp_value,
                    '--path=' + site_path,
                    '--allow-root'
                ]
                if use_json_format:
                    option_cmd.append('--format=json')

                option_result = subprocess.run(
                    option_cmd,
                    capture_output=True,
                    text=True,
                    timeout=BaselineApplicator.WP_CLI_TIMEOUT
                )

                if option_result.returncode != 0:
                    Log.warn(
                        app,
                        f"Failed to update option {option_name} for {domain}: "
                        f"{option_result.stderr.strip()}"
                    )

            if prune:
                baseline_plugins = set(baseline.get('plugins', []))
                active_plugins = set(
                    BaselineApplicator._get_active_plugin_slugs(app, site_path)
                )
                plugins_to_deactivate = sorted(active_plugins - baseline_plugins)
                if plugins_to_deactivate:
                    deactivate_cmd = [
                        'wp', 'plugin', 'deactivate',
                    ] + plugins_to_deactivate + [
                        '--path=' + site_path,
                        '--allow-root'
                    ]
                    deactivate_result = subprocess.run(
                        deactivate_cmd,
                        capture_output=True,
                        text=True,
                        timeout=BaselineApplicator.WP_CLI_TIMEOUT
                    )
                    if deactivate_result.returncode != 0:
                        result['error'] = "Failed to prune plugins: " + \
                                        deactivate_result.stderr
                        return result
                    for plugin_slug in plugins_to_deactivate:
                        Log.info(app, f"Deactivated plugin {plugin_slug} for {domain}")
            
            BaselineApplicator.enable_object_cache_dropin(
                app, domain, site_path, baseline
            )

            if 'nginx-helper' in baseline.get('plugins', []):
                BaselineApplicator.ensure_nginx_helper_caps(
                    app, domain, site_path
                )
                if cache_type:
                    BaselineApplicator.configure_nginx_helper(
                        app, domain, site_path, cache_type
                    )

            # Switch the baseline theme last, so a theme failure never blocks
            # the plugin, option, drop-in and cache configuration above from
            # being applied. A specified-but-failed baseline theme is still
            # fatal to the apply (success stays False) so the site is not
            # recorded as fully at baseline.
            theme_slug = baseline.get('theme')
            if theme_slug:
                theme_result = subprocess.run(
                    [
                        'wp', 'theme', 'activate', theme_slug,
                        '--path=' + site_path,
                        '--allow-root'
                    ],
                    capture_output=True,
                    text=True,
                    timeout=BaselineApplicator.WP_CLI_TIMEOUT
                )
                if theme_result.returncode != 0:
                    result['error'] = (
                        f"Failed to activate baseline theme {theme_slug}: "
                        f"{theme_result.stderr.strip()}"
                    )
                    Log.warn(app, f"{result['error']} for {domain}")
                    return result

            result['success'] = True
            return result
            
        except subprocess.TimeoutExpired:
            result['error'] = "WP-CLI command timeout"
            return result
        except Exception as e:
            result['error'] = str(e)
            return result

    @staticmethod
    def configure_nginx_helper(app, domain, site_path, cache_type):
        """Enable Nginx Helper cache purging for FastCGI/Redis tenants.

        The Nginx Helper plugin ships with purging disabled, so
        rt_wp_nginx_helper_options must be seeded for cache purge to work.
        Stock `wo site create` does this in site_functions.setupwordpress;
        the shared-core create/apply path must do the same. cache_method
        tracks the tenant cache type; cache types that do not use Nginx
        Helper (wprocket/wpce/wpsc/basic) are skipped. Failures warn only.
        """
        cache_method = {
            'wpfc': 'enable_fastcgi',
            'wpredis': 'enable_redis',
        }.get(cache_type)
        if not cache_method:
            return

        helper_options = {
            "log_level": "INFO",
            "log_filesize": 5,
            "enable_purge": 1,
            "enable_map": "0",
            "enable_log": 0,
            "enable_stamp": 1,
            "purge_homepage_on_new": 1,
            "purge_homepage_on_edit": 1,
            "purge_homepage_on_del": 1,
            "purge_archive_on_new": 1,
            "purge_archive_on_edit": 1,
            "purge_archive_on_del": 1,
            "purge_archive_on_new_comment": 0,
            "purge_archive_on_deleted_comment": 0,
            "purge_page_on_mod": 1,
            "purge_page_on_new_comment": 1,
            "purge_page_on_deleted_comment": 1,
            "cache_method": cache_method,
            "purge_method": "get_request",
            "redis_hostname": "127.0.0.1",
            "redis_port": "6379",
            "redis_prefix": "nginx-cache:",
        }

        option_cmd = [
            'wp', 'option', 'update', 'rt_wp_nginx_helper_options',
            json.dumps(helper_options),
            '--path=' + site_path,
            '--allow-root',
            '--format=json',
        ]
        try:
            option_result = subprocess.run(
                option_cmd,
                capture_output=True,
                text=True,
                timeout=BaselineApplicator.WP_CLI_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            Log.warn(
                app,
                f"Failed to enable Nginx Helper purge for {domain}: {e}"
            )
            return
        if option_result.returncode != 0:
            Log.warn(
                app,
                f"Failed to enable Nginx Helper purge for {domain}: "
                f"{option_result.stderr.strip()}"
            )
        else:
            Log.debug(app, f"Enabled Nginx Helper purge for {domain}")

    @staticmethod
    def ensure_nginx_helper_caps(app, domain, site_path):
        """Grant Nginx Helper's custom caps to the administrator role.

        Nginx Helper 2.x gates cache purging and its settings page behind the
        custom caps 'Nginx Helper | Purge cache' and 'Nginx Helper | Config'.
        Its activation hook only grants them to the administrator role when
        current_user_can('activate_plugins') is true, which never holds when the
        plugin is activated through WP-CLI without a user context (as the
        shared-core create/apply path does). Admins then hit "you do not have
        the necessary privileges" on the purge button. Grant the caps
        explicitly; `wp cap add` is idempotent, so this is safe on every apply.
        """
        for cap in ('Nginx Helper | Purge cache', 'Nginx Helper | Config'):
            cap_cmd = [
                'wp', 'cap', 'add', 'administrator', cap,
                '--path=' + site_path,
                '--allow-root',
            ]
            try:
                cap_result = subprocess.run(
                    cap_cmd,
                    capture_output=True,
                    text=True,
                    timeout=BaselineApplicator.WP_CLI_TIMEOUT,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                Log.warn(
                    app,
                    f"Failed to grant Nginx Helper cap '{cap}' for {domain}: {e}"
                )
                continue
            if cap_result.returncode != 0:
                Log.warn(
                    app,
                    f"Failed to grant Nginx Helper cap '{cap}' for {domain}: "
                    f"{cap_result.stderr.strip()}"
                )
            else:
                Log.debug(
                    app, f"Ensured Nginx Helper cap '{cap}' for {domain}"
                )

    @staticmethod
    def enable_object_cache_dropin(app, domain, site_path, baseline):
        """Enable the Object Cache Pro drop-in for a site.

        Object Cache Pro ships its drop-in as a stub; WordPress only routes
        caching through Redis once ``wp-content/object-cache.php`` exists.
        ``WP_REDIS_CONFIG`` in ``wp-config.php`` is inert without it. Runs on
        both create and apply, so new and existing sites converge.

        Best-effort: any failure is logged as a warning and never aborts
        baseline application or site provisioning.
        """
        if 'object-cache-pro' not in baseline.get('plugins', []):
            return

        dropin = os.path.join(site_path, 'wp-content', 'object-cache.php')

        # --force overwrites an existing/stale drop-in (OCP errors without it
        # once object-cache.php exists), making this safe to re-run. Skip the
        # per-site flush: cache is empty on create and cleared globally on apply.
        try:
            enable_result = subprocess.run(
                [
                    'wp', 'redis', 'enable', '--force',
                    '--skip-flush', '--skip-flush-notice',
                    '--path=' + site_path,
                    '--allow-root'
                ],
                capture_output=True,
                text=True,
                timeout=BaselineApplicator.WP_CLI_TIMEOUT
            )
        except Exception as e:
            Log.warn(
                app,
                f"Could not enable Object Cache Pro drop-in for {domain}: {e}"
            )
            return

        if enable_result.returncode != 0 or not os.path.exists(dropin):
            Log.warn(
                app,
                f"Could not enable Object Cache Pro drop-in for {domain}: "
                f"{(enable_result.stderr or enable_result.stdout).strip()}"
            )
            return

        # wp-cli ran with --allow-root, so the drop-in is root-owned; the web
        # server runs as www-data and must own it for later drop-in updates.
        try:
            shutil.chown(dropin, user='www-data', group='www-data')
        except Exception as e:
            Log.warn(
                app,
                f"Enabled Object Cache Pro drop-in for {domain} but could "
                f"not set ownership: {e}"
            )
            return

        Log.info(app, f"Enabled Object Cache Pro drop-in for {domain}")
    
    @staticmethod
    def apply_baseline_to_sites(app, config, baseline_version, dry_run=False, verbose=False, prune=False):
        """Apply current baseline to all enabled sites via WP-CLI.

        Returns a summary dict with attempted/succeeded/failed counts and a
        per-site breakdown. dry_run previews without mutating; verbose prints
        per-site progress and timings.
        """

        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"

        with open(baseline_file, 'r') as f:
            baseline = json.load(f)

        from wo.core.database import db_session
        from wo.cli.plugins.multitenancy_db import MultitenancySite

        session = db_session
        sites = session.query(MultitenancySite).filter_by(is_enabled=True).all()
        production_sites = [
            {'domain': s.domain, 'site_path': s.site_path,
             'cache_type': s.cache_type} for s in sites
        ]

        if not production_sites:
            Log.warn(app, "No enabled sites to apply baseline to")
            return {
                'status': 'noop', 'dry_run': dry_run,
                'baseline_version': baseline_version,
                'attempted': 0, 'succeeded': 0, 'failed': 0, 'sites': [],
            }

        header = f"Applying baseline v{baseline_version} to {len(production_sites)} sites"
        if dry_run:
            header += ' [DRY RUN — no changes will be written]'
        Log.info(app, header + '...')

        success_count = 0
        failed_count = 0
        per_site = []

        # Per-site work is pure wp-cli/filesystem; all SQLite writes stay in
        # the coordinator thread below. Workers must not touch db_session.
        try:
            apply_workers = int(config.get('apply_workers', 4))
        except (TypeError, ValueError):
            apply_workers = 4
        apply_workers = max(1, min(16, apply_workers))
        workers = min(apply_workers, len(production_sites)) or 1

        def _dry_run_site(site):
            domain = site['domain']
            cache_type = site.get('cache_type')
            # DB site_path is the site root; WordPress (and wp-cli --path)
            # lives in <root>/htdocs.
            wp_path = os.path.join(site['site_path'], 'htdocs')
            option_names = list(baseline.get('options', {}).keys())
            plugins_to_deactivate = []
            prune_error = None
            if prune:
                try:
                    baseline_plugins = set(baseline.get('plugins', []))
                    active_plugins = set(
                        BaselineApplicator._get_active_plugin_slugs(app, wp_path)
                    )
                    plugins_to_deactivate = sorted(active_plugins - baseline_plugins)
                except Exception as e:
                    prune_error = str(e)
                    Log.warn(
                        app,
                        f"  [dry-run] could not determine prune set for {domain}: {prune_error}"
                    )
            Log.info(
                app,
                f"  [dry-run] would apply baseline to {domain} "
                f"(plugins={','.join(baseline.get('plugins', []))}, "
                f"theme={baseline.get('theme', '')}, "
                f"options={','.join(option_names)})"
            )
            if 'nginx-helper' in baseline.get('plugins', []):
                Log.info(
                    app,
                    f"  [dry-run] would grant Nginx Helper admin "
                    f"capabilities for {domain}"
                )
                if cache_type in ('wpfc', 'wpredis'):
                    Log.info(
                        app,
                        f"  [dry-run] would enable Nginx Helper purge "
                        f"(cache_type={cache_type}) for {domain}"
                    )
            if prune:
                Log.info(
                    app,
                    f"  [dry-run] would deactivate for {domain}: "
                    f"{','.join(plugins_to_deactivate) if plugins_to_deactivate else '(none)'}"
                )
            return {
                'domain': domain,
                'status': 'dry_run',
                'options': option_names,
                'prune_deactivate': plugins_to_deactivate,
                'prune_error': prune_error,
            }

        def _apply_site(site):
            domain = site['domain']
            wp_path = os.path.join(site['site_path'], 'htdocs')
            start = _time.monotonic()
            result = BaselineApplicator.apply_baseline_to_site(
                app, domain, wp_path, baseline, prune=prune,
                cache_type=site.get('cache_type'),
            )
            dur = int((_time.monotonic() - start) * 1000)
            return result, dur

        with ThreadPoolExecutor(max_workers=workers) as pool:
            if dry_run:
                futures = {
                    pool.submit(_dry_run_site, site): site
                    for site in production_sites
                }
                for future in as_completed(futures):
                    site = futures[future]
                    try:
                        per_site.append(future.result())
                    except Exception as e:
                        failed_count += 1
                        Log.warn(app, f"  ❌ {site['domain']}: {e}")
                        per_site.append({
                            'domain': site['domain'], 'status': 'failed',
                            'error': str(e), 'duration_ms': 0,
                        })
            else:
                futures = {
                    pool.submit(_apply_site, site): site
                    for site in production_sites
                }
                for future in as_completed(futures):
                    site = futures[future]
                    domain = site['domain']
                    try:
                        result, dur = future.result()
                    except Exception as e:
                        # One site blowing up must not abort the fleet.
                        result = {'success': False, 'error': str(e)}
                        dur = 0
                    if result['success']:
                        skipped = result.get('skipped_plugins') or []
                        try:
                            site_obj = session.query(MultitenancySite).filter_by(domain=domain).first()
                            if site_obj:
                                # Only advance the recorded baseline version
                                # when every plugin applied. A site with
                                # skipped plugins is not fully at this
                                # baseline, so leave its version untouched so
                                # `validate` flags it and a later `apply`
                                # re-attempts.
                                if not skipped:
                                    site_obj.baseline_version = baseline_version
                                site_obj.updated_at = datetime.now()
                                session.commit()
                        except Exception as e:
                            # A tracking-DB hiccup fails this site only, not
                            # the fleet; keep the session usable.
                            try:
                                session.rollback()
                            except Exception:
                                pass
                            failed_count += 1
                            Log.warn(app, f"  ❌ {domain}: applied but "
                                          f"tracking update failed: {e}")
                            per_site.append({
                                'domain': domain, 'status': 'failed',
                                'error': f'tracking update failed: {e}',
                                'duration_ms': dur,
                            })
                            continue
                        success_count += 1
                        if verbose:
                            Log.info(app, f"  ✅ {domain} ({dur} ms)")
                        else:
                            Log.debug(app, f"Applied to {domain}")
                        if skipped:
                            Log.warn(
                                app,
                                f"  ⚠️  {domain}: skipped plugins (version not "
                                f"advanced): {', '.join(skipped)}"
                            )
                        per_site.append({
                            'domain': domain,
                            'status': 'partial' if skipped else 'success',
                            'duration_ms': dur,
                            'skipped_plugins': skipped,
                        })
                    else:
                        failed_count += 1
                        Log.warn(app, f"  ❌ {domain}: {result['error']}")
                        per_site.append({
                            'domain': domain, 'status': 'failed',
                            'error': result['error'], 'duration_ms': dur,
                        })

        if not dry_run:
            Log.info(app, "Clearing cache globally...")
            if not MTFunctions.clear_all_caches(app):
                Log.warn(app, "Global cache clear failed after baseline apply")

        Log.info(app, "")
        Log.info(app, "=" * 60)
        Log.info(
            app,
            f"✅ Successfully applied to {success_count}/{len(production_sites)} sites"
            if not dry_run
            else f"[DRY RUN] Would apply to {len(production_sites)} sites",
        )

        if failed_count > 0:
            Log.warn(app, f"⚠️  {failed_count} site(s) failed — see warnings above")

        Log.info(app, "=" * 60)

        return {
            'status': 'completed' if not dry_run else 'dry_run',
            'dry_run': dry_run,
            'baseline_version': baseline_version,
            'attempted': len(production_sites),
            'succeeded': success_count,
            'failed': failed_count,
            'sites': per_site,
        }


# ============================================================================
# SHARED CONFIGURATION (wp-config-shared.php)
# ============================================================================
# Every tenant wp-config.php does require_once of this file. These helpers
# create it, lint it (php -l), reload services after a change, and edit it.
# ============================================================================


def create_shared_config_file(app, shared_root):
    """
    Create the initial shared WordPress configuration file.

    This file contains fleet-wide settings like security, performance,
    and caching configuration that apply to ALL tenant sites. Each site's
    wp-config.php will include this file automatically.

    Args:
        app: WordOps application instance
        shared_root: Path to shared infrastructure root (e.g., /var/www/shared)

    Returns:
        bool: True if file created successfully, False otherwise

    Note:
        - File is created at: {shared_root}/config/wp-config-shared.php
        - Permissions set to 644 (readable by www-data)
        - Contains production-safe defaults (debug disabled, etc.)
    """
    from wo.core.logging import Log
    from datetime import datetime

    config_dir = f"{shared_root}/config"
    config_file = f"{config_dir}/wp-config-shared.php"

    # Check if config file already exists
    if os.path.exists(config_file):
        Log.debug(app, f"Shared config already exists: {config_file}")
        return True

    # Ensure config directory exists
    os.makedirs(config_dir, exist_ok=True)

    # Get current timestamp for file header
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Generate the shared configuration file content
    # This template matches Appendix B from the implementation plan
    config_content = f'''<?php
/**
 * Shared WordPress Configuration
 * Applied to ALL multi-tenant sites
 * 
 * Created: {timestamp}
 * Version: 1.0
 * Last Updated: {timestamp}
 * 
 * WARNING: This file affects ALL sites in the multi-tenant installation.
 * Changes here apply immediately to all sites. Test carefully.
 */

// ============================================================================
// ⚠️  DO NOT ADD THESE TO SHARED CONFIG - THEY MUST BE SITE-SPECIFIC:
// ============================================================================
// ❌ Authentication salts (must be unique per site - in site's wp-config.php)
// ❌ Database credentials (must be unique per site - in site's wp-config.php)
// ❌ Redis prefix (must be unique per site - in site's wp-config.php)
// ❌ Site URLs (must be unique per site - WordPress manages these)
// ❌ WP_REDIS_CONFIG array (must be in site's wp-config.php with unique prefix)
// ============================================================================

// ============================================================================
// SECURITY SETTINGS
// ============================================================================

/** Prevent file editing from WordPress admin */
define('DISALLOW_FILE_EDIT', true);

/** Prevent plugin/theme installation/updates from admin */
define('DISALLOW_FILE_MODS', true);

/** Disable automatic updates (managed by WordOps) */
define('AUTOMATIC_UPDATER_DISABLED', true);
define('WP_AUTO_UPDATE_CORE', false);

/** Force SSL for admin area (set to true if all sites have SSL) */
define('FORCE_SSL_ADMIN', false);

// ============================================================================
// PERFORMANCE SETTINGS
// ============================================================================

/** Memory limits */
define('WP_MEMORY_LIMIT', '256M');
define('WP_MAX_MEMORY_LIMIT', '512M');

/** Post revisions (reduce DB bloat) */
define('WP_POST_REVISIONS', 5);

/** Autosave interval (5 minutes) */
define('AUTOSAVE_INTERVAL', 300);

/** Trash retention (7 days) */
define('EMPTY_TRASH_DAYS', 7);

/** Enable media trash */
define('MEDIA_TRASH', true);

// ============================================================================
// CACHE SETTINGS
// ============================================================================

/** Enable object caching */
define('WP_CACHE', true);

// ============================================================================
// DEBUG SETTINGS (Production)
// ============================================================================

/** Disable all debugging in production */
define('WP_DEBUG', false);
define('WP_DEBUG_LOG', false);
define('WP_DEBUG_DISPLAY', false);
define('SCRIPT_DEBUG', false);
define('SAVEQUERIES', false);

// ============================================================================
// CRON SETTINGS
// ============================================================================

// Disable loopback WP-Cron; system cron runs due events every minute
// (managed by WordOps multitenancy: /etc/cron.d/wo-multitenancy)
if (!defined('DISABLE_WP_CRON')) {{
    define('DISABLE_WP_CRON', true);
}}

// ============================================================================
// ENVIRONMENT TYPE
// ============================================================================

/** Define environment (production/staging/development) */
define('WP_ENVIRONMENT_TYPE', 'production');

// ============================================================================
// REDIS OBJECT CACHE PRO LICENSE TOKEN
// ============================================================================

/** 
 * Redis Object Cache Pro license token
 * Get your token from: https://objectcache.pro/
 * Each site's wp-config.php merges this with site-specific prefix
 * 
 * NOTE: The WP_REDIS_CONFIG array is defined in each site's wp-config.php
 * with a unique prefix. This token is shared across all sites.
 */
define('WO_REDIS_TOKEN', '');

// ============================================================================
// CUSTOM CONSTANTS
// ============================================================================

/** Add any custom fleet-wide constants below */

'''

    try:
        # Write the shared config file
        with open(config_file, 'w') as f:
            f.write(config_content)

        # Set secure permissions (644 - readable by www-data, writable by root only)
        os.chmod(config_file, 0o644)

        # Set ownership to root:root for security
        try:
            shutil.chown(config_file, user='root', group='root')
        except Exception:
            pass  # May fail in non-root contexts during testing

        Log.debug(app, f"Created shared config file: {config_file}")
        return True

    except Exception as e:
        Log.error(app, f"Failed to create shared config file: {str(e)}")
        return False


def lint_php_file(app, path, *, missing_ok=False, php_missing_ok=True):
    """Run `php -l` on a file; return True if syntax is valid (or skipped).

    Skips (returns True) when the file is absent and missing_ok, or when php is
    not on PATH and php_missing_ok. Returns False on a real syntax error
    (logging php's stderr) or when a required file is absent.
    """
    if not os.path.exists(path):
        if missing_ok:
            return True
        Log.error(app, f"Config file not found: {path}", False)
        return False
    if shutil.which('php') is None:
        if php_missing_ok:
            Log.debug(app, "php not found on PATH; skipping syntax check")
            return True
        Log.error(app, "php not found on PATH; cannot validate config", False)
        return False
    try:
        result = subprocess.run(['php', '-l', path], capture_output=True, text=True)
    except Exception as e:
        Log.error(app, f"Could not validate {path}: {str(e)}", False)
        return False
    if result.returncode != 0:
        Log.error(app, f"PHP syntax error in {path}:\n{result.stderr}", False)
        return False
    Log.debug(app, f"PHP syntax valid: {path}")
    return True


def reload_services_after_config_change(app, shared_root):
    """
    Reload PHP-FPM (all installed versions) and Nginx after config changes

    This method automatically detects all installed PHP versions and reloads
    their PHP-FPM services, plus Nginx. This ensures configuration changes
    take effect immediately across all sites.

    Uses native WordOps service management (WOService) for reliability.
    PHP-FPM reload failures are non-fatal (logged as warnings), but an Nginx
    reload failure is critical: the function logs it and returns False.

    Args:
        app: WordOps application instance

    Returns:
        bool: True if nginx (and PHP-FPM) reloaded; False if nginx failed

    Process:
        1. Detect all installed PHP versions (7.4, 8.0, 8.1, 8.2, 8.3, 8.4, 8.5)
        2. Reload PHP-FPM for each version (continue on failure)
        3. Reload Nginx (critical - failure returns False)

    Note:
        OpCache is automatically cleared during PHP-FPM reload
    """
    from wo.core.services import WOService
    from wo.core.logging import Log

    Log.info(app, "Reloading services to apply configuration changes...")

    # Detect all installed PHP versions by checking for config files
    php_versions = []
    for version in ['7.4', '8.0', '8.1', '8.2', '8.3', '8.4', '8.5']:
        config_path = f'/etc/php/{version}/fpm/php-fpm.conf'
        if os.path.exists(config_path):
            php_versions.append(version)
            Log.debug(app, f"Detected PHP {version} installed")

    if not php_versions:
        Log.warn(app, "No PHP-FPM versions detected on system")
    else:
        Log.debug(app, f"Found {len(php_versions)} PHP version(s): {', '.join(php_versions)}")

    # Reload PHP-FPM for each installed version
    # Continue even if one version fails (non-fatal)
    failed_php_reloads = []

    for version in php_versions:
        service_name = f'php{version}-fpm'
        Log.debug(app, f"Reloading {service_name}...")
        if WOService.reload_service(app, service_name):
            Log.debug(app, f"Reloaded {service_name} successfully")
        else:
            Log.warn(app, f"⚠️  Failed to reload {service_name}")
            failed_php_reloads.append(service_name)
            # Continue with other services (non-fatal)

    # Reload Nginx (CRITICAL - must succeed)
    Log.debug(app, "Reloading nginx...")
    if not WOService.reload_service(app, 'nginx'):
        Log.error(app, "❌ CRITICAL: Failed to reload nginx: "
                       "configuration may not be applied correctly!",
                  exit=False)
        return False

    # Summary
    if failed_php_reloads:
        Log.warn(app, f"Some PHP-FPM services failed to reload: {', '.join(failed_php_reloads)}")
        Log.warn(app, "Sites using those PHP versions may not have updated configuration")

    Log.info(app, "✅ Service reload completed")
    return True


def edit_shared_config(app, shared_root):
    """Open wp-config-shared.php in $EDITOR; lint on save, then reload or revert.

    A timestamped .bak is taken before editing (10 newest kept). After the
    editor exits the file is linted with `php -l`: on success PHP-FPM + nginx are
    reloaded so the change takes effect despite OPcache; on a syntax error the
    backup is restored so no site is left broken.
    """
    cfg = f"{shared_root}/config/wp-config-shared.php"
    if not os.path.exists(cfg):
        Log.error(app, f"Shared config not found: {cfg}. Run: wo multitenancy init")
        return
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = f"{cfg}.bak.{ts}"
    shutil.copy2(cfg, backup)
    # Keep only the 10 newest backups
    backups = sorted(glob.glob(f"{cfg}.bak.*"), key=os.path.getmtime, reverse=True)
    for old in backups[10:]:
        try:
            os.remove(old)
        except OSError:
            pass
    editor = os.environ.get('EDITOR', 'vi')
    subprocess.call([editor, cfg])
    if lint_php_file(app, cfg):
        if reload_services_after_config_change(app, shared_root):
            Log.info(app, "Shared config updated and services reloaded")
        else:
            Log.error(app, "Shared config saved, but the service reload failed; "
                           "reload nginx/PHP-FPM manually to apply it")
    else:
        shutil.copy2(backup, cfg)
        Log.error(app, f"Syntax error - reverted to {backup}. "
                       f"Re-run: wo multitenancy shared-config --action edit")
