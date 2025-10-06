"""WordOps Multi-tenancy Functions Module
Core functions for managing shared WordPress infrastructure.
"""

import os
import json
import shutil
import subprocess
import random
import string
import tarfile
import configparser
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
    @staticmethod
    def load_config(app):
        """Load multi-tenancy configuration"""
        config_file = '/etc/wo/plugins.d/multitenancy.conf'
        config = configparser.ConfigParser()
        
        # Default configuration
        defaults = {
            'shared_root': '/var/www/shared',
            'keep_releases': '3',
            'php_version': '8.3',
            'admin_email': 'admin@example.com',
            'baseline_plugins': 'nginx-helper,redis-cache',
            'baseline_theme': 'twentytwentyfour',
            'auto_activate': 'true',
            'wp_locale': 'en_US'
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
        
        # Add GitHub plugins section
        if config.has_section('github_plugins'):
            github_plugins = {}
            for key, value in config.items('github_plugins'):
                # Skip items that don't look like GitHub repo definitions
                if ',' in value and '/' in value:
                    github_plugins[key] = value
            if github_plugins:
                result['github_plugins'] = github_plugins
        
        # Add GitHub themes section
        if config.has_section('github_themes'):
            github_themes = {}
            for key, value in config.items('github_themes'):
                # Skip items that don't look like GitHub repo definitions
                if ',' in value and '/' in value:
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
    
        return result
    
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
            return config.get('php_version', '8.3')
    
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
                    Log.error(app, f"Nginx configuration test failed!")
                    Log.error(app, f"Error: {result.stderr.strip()}")
                    Log.error(app, f"Output: {result.stdout.strip()}")
                return False

        except subprocess.TimeoutExpired:
            if log_errors:
                Log.error(app, "Nginx configuration test timed out")
            return False
        except Exception as e:
            if log_errors:
                Log.error(app, f"Nginx configuration test error: {e}")
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
            php_version = "8.3"  # Default, will be overridden by actual version
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
                Log.error(app, f"Nginx configuration test failed before reload:")
                Log.error(app, f"Error: {test_result.stderr}")
                Log.error(app, f"Output: {test_result.stdout}")
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
            Log.error(app, f"All nginx reload methods failed for {domain}")
            Log.error(app, f"systemctl error: {reload_result.stderr}")
            Log.error(app, f"signal error: {signal_result.stderr}")
            return False

        except Exception as e:
            Log.error(app, f"Exception during nginx reload for {domain}: {e}")
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
    def generate_wp_config(app, site_root, domain, db_name, db_user, db_pass, db_host):
        """Generate wp-config.php for shared WordPress site"""
        
        # Get salts from WordPress.org
        try:
            salts = subprocess.check_output(
                ['curl', '-s', 'https://api.wordpress.org/secret-key/1.1/salt/'],
                universal_newlines=True
            )
        except:
            # Generate fallback salts if API is unavailable
            salts = MTFunctions.generate_salts()
        
        # Following HandPressed pattern: wp-config.php goes IN the webroot (htdocs)
        # This is secure because WordPress blocks direct HTTP access to wp-config.php
        # and it allows the router to load it without permission issues
        
        wp_config = f"""<?php
/**
 * WordPress Configuration for {domain}
 * Generated by WordOps Multi-tenancy Plugin
 */

// ** Database settings ** //
define( 'DB_NAME', '{db_name}' );
define( 'DB_USER', '{db_user}' );
define( 'DB_PASSWORD', '{db_pass}' );
define( 'DB_HOST', '{db_host}' );
define( 'DB_CHARSET', 'utf8mb4' );
define( 'DB_COLLATE', '' );

// ** Authentication Unique Keys and Salts ** //
{salts}

// ** WordPress Database Table Prefix ** //
$table_prefix = 'wp_';

// ** WordPress Content Directory ** //
define( 'WP_CONTENT_DIR', __DIR__ . '/wp-content' );
define( 'WP_CONTENT_URL', (isset($_SERVER['HTTPS']) && $_SERVER['HTTPS'] === 'on' ? 'https' : 'http') . '://{domain}/wp-content' );

// ** WordPress Core Directory - FORCED for shared setup ** //
define( 'ABSPATH', __DIR__ . '/wp/' );

// ** File System Method ** //
define( 'FS_METHOD', 'direct' );

// ** Performance Settings ** //
define( 'WP_MEMORY_LIMIT', '256M' );
define( 'WP_MAX_MEMORY_LIMIT', '512M' );

// ** Security Settings ** //
define( 'DISALLOW_FILE_EDIT', true );
define( 'DISALLOW_FILE_MODS', false );

// ** Debug Settings ** //
define( 'WP_DEBUG', false );
define( 'WP_DEBUG_LOG', false );
define( 'WP_DEBUG_DISPLAY', false );

// ** Cache Settings ** //
define( 'WP_CACHE', true );

// ** SSL Settings ** //
if ( isset( $_SERVER['HTTP_X_FORWARDED_PROTO'] ) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https' ) {{
    $_SERVER['HTTPS'] = 'on';
}}

// ** Multisite Settings (if needed) ** //
// define( 'WP_ALLOW_MULTISITE', true );

/* That's all, stop editing! Happy publishing. */

/** Absolute path to the WordPress directory. */
if ( ! defined( 'ABSPATH' ) ) {{
    define( 'ABSPATH', __DIR__ . '/wp/' );
}}

/** Sets up WordPress vars and included files. */
/** WP-CLI loads wp-settings automatically, so skip for CLI */
if ( ! defined( 'WP_CLI' ) ) {{
    require_once ABSPATH . 'wp-settings.php';
}}


/* That's all, stop editing! Happy publishing. */

/** Sets up WordPress vars and included files. */
/** WP-CLI loads wp-settings automatically, so skip for CLI */
if ( ! defined( 'WP_CLI' ) ) {{
    require_once ABSPATH . 'wp-settings.php';
}}
"""
        
        # Place wp-config.php in htdocs (webroot) like HandPressed does
        # This is secure - WordPress blocks direct access via .htaccess/nginx rules
        wp_config_path = f"{site_root}/htdocs/wp-config.php"
        with open(wp_config_path, 'w') as f:
            f.write(wp_config)
        
        # Set secure permissions (readable by www-data)
        os.chmod(wp_config_path, 0o640)
        Log.debug(app, f"Generated wp-config.php at {wp_config_path}")
    
    @staticmethod
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
    location /wp {{
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
            Log.error(app, f"Failed to install WordPress: {e.stderr}")
            raise
    
    @staticmethod
    def get_admin_password(app, domain):
        """Retrieve admin password for a site"""
        pass_file = f"/var/www/{domain}/.admin_pass"
        if os.path.exists(pass_file):
            with open(pass_file, 'r') as f:
                return f.read().strip()
        return "Check /var/www/{domain}/.admin_pass"
    
    @staticmethod
    def apply_baseline(app, domain, site_htdocs, config):
        """Apply baseline configuration to site"""
        
        # Activate baseline plugins (run from htdocs where wp-config.php shim exists)
        plugins = config.get('baseline_plugins', [])
        for plugin in plugins:
            try:
                cmd = [
                    'wp', 'plugin', 'activate', plugin,
                    '--allow-root'
                ]
                subprocess.run(cmd, cwd=site_htdocs, capture_output=True, check=False)
                Log.debug(app, f"Activated plugin {plugin} for {domain}")
            except:
                Log.debug(app, f"Could not activate plugin {plugin}")
        
        # Activate baseline theme (run from htdocs where wp-config.php shim exists)
        theme = config.get('baseline_theme', 'twentytwentyfour')
        if not MTFunctions.ensure_and_activate_theme(app, domain, site_htdocs, theme):
            Log.warn(app, f"Failed to activate theme {theme}, trying fallback themes")
            # Try fallback themes if primary theme fails
            fallback_themes = ['twentytwentyfour', 'twentytwentythree', 'twentytwentytwo']
            for fallback_theme in fallback_themes:
                if MTFunctions.ensure_and_activate_theme(app, domain, site_htdocs, fallback_theme):
                    Log.info(app, f"Successfully activated fallback theme {fallback_theme} for {domain}")
                    break
            else:
                Log.warn(app, f"All theme activation attempts failed for {domain}")

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

        try:
            Log.debug(app, f"Starting SSL setup for {domain}")

            # Prepare acme domains list
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

            # Configure Let's Encrypt SSL
            if WOAcme.setupletsencrypt(app, acme_domains, acmedata):
                Log.debug(app, f"Let's Encrypt certificates obtained for {domain}")

                # Deploy certificate files and create ssl.conf with listen 443 directives
                # Note: deploycert() returns 0 on success, not True
                deploy_result = WOAcme.deploycert(app, domain)
                if deploy_result == 0:
                    Log.debug(app, f"SSL certificates deployed for {domain}")

                    # Test nginx configuration before applying SSL changes
                    if MTFunctions.validate_nginx_config(app):
                        # Configure HTTPS redirect
                        SSL.httpsredirect(app, domain, acme_domains, redirect=True)
                        SSL.siteurlhttps(app, domain)

                        # Enable HSTS if requested
                        if hasattr(pargs, 'hsts') and pargs.hsts:
                            SSL.setuphsts(app, domain)

                        # Final validation after all SSL changes
                        if MTFunctions.validate_nginx_config(app):
                            # Reload nginx to apply SSL configuration using our robust function
                            if MTFunctions.safe_nginx_reload(app, domain):
                                Log.info(app, f"SSL configured successfully for {domain}")
                                return True
                            else:
                                Log.error(app, f"Failed to reload nginx after SSL setup for {domain}")
                                return False
                        else:
                            Log.error(app, f"Nginx configuration invalid after SSL setup for {domain}")
                            return False
                    else:
                        Log.error(app, f"Nginx configuration invalid after certificate deployment for {domain}")
                        return False
                else:
                    Log.error(app, f"Failed to deploy SSL certificates for {domain}")
                    return False
            else:
                Log.warn(app, f"Failed to obtain SSL certificates for {domain}")
                return False

        except Exception as e:
            Log.debug(app, f"SSL setup error: {e}")
            Log.warn(app, f"Could not configure SSL for {domain}: {str(e)}")
            return False

    @staticmethod
    def cleanup_failed_site(app, domain, site_root):
        """Cleanup partially created site on failure"""
        from wo.cli.plugins.sitedb import deleteSiteInfo
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
        nginx_files = [f for f in os.listdir('/etc/nginx/sites-available/') if f.startswith(domain)]
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

        # Remove from WordOps database if exists
        try:
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
    def clear_cache(app, domain, cache_type):
        """Clear cache for a site"""

        # Clear nginx cache
        if cache_type in ['wpfc', 'wpredis']:
            try:
                WOShellExec.cmd_exec(app, f"wo clean --fastcgi {domain}")
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
    
    def test_site(app, domain):
        """Test if a site is working"""
        import requests
        
        try:
            response = requests.get(f"http://{domain}", timeout=5)
            return response.status_code < 500
        except:
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
    
    def download_wordpress_core(self):
        """Download WordPress core"""
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        release_name = f"wp-{timestamp}"
        release_path = f"{self.releases_dir}/{release_name}"
        
        # Download WordPress using WP-CLI
        cmd = [
            'wp', 'core', 'download',
            f'--path={release_path}',
            '--skip-content',  # Don't download default themes/plugins
            '--allow-root'
        ]
        
        try:
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
    
    def seed_plugins_and_themes(self, config):
        """Download initial plugins and themes"""
        
        # Download baseline plugins from WordPress.org
        plugins = config.get('baseline_plugins', ['nginx-helper', 'redis-cache'])
        if isinstance(plugins, str):
            plugins = [p.strip() for p in plugins.split(',')]
        
        for plugin in plugins:
            self.download_plugin(plugin)
        
        # Download GitHub plugins
        github_plugins = config.get('github_plugins', {})
        if github_plugins:
            for plugin_slug, repo_info in github_plugins.items():
                if isinstance(repo_info, str):
                    # Parse format: "user/repo,branch,name" or "user/repo,tag,name"
                    parts = [p.strip() for p in repo_info.split(',')]
                    if len(parts) >= 2:
                        github_repo = parts[0]
                        ref_type = parts[1]  # 'branch' or 'tag'
                        ref_name = parts[2] if len(parts) > 2 else None
                        
                        if ref_type == 'branch' and ref_name:
                            self.download_plugin_from_github(github_repo, plugin_slug, branch=ref_name)
                        elif ref_type == 'tag' and ref_name:
                            self.download_plugin_from_github(github_repo, plugin_slug, tag=ref_name)
                        else:
                            self.download_plugin_from_github(github_repo, plugin_slug)
        
        # Download baseline theme from WordPress.org
        theme = config.get('baseline_theme', 'twentytwentyfour')
        if theme:
            self.download_theme(theme)
        
        # Download GitHub themes
        github_themes = config.get('github_themes', {})
        if github_themes:
            for theme_slug, repo_info in github_themes.items():
                if isinstance(repo_info, str):
                    # Parse format: "user/repo,branch,name" or "user/repo,tag,name"
                    parts = [p.strip() for p in repo_info.split(',')]
                    if len(parts) >= 2:
                        github_repo = parts[0]
                        ref_type = parts[1]  # 'branch' or 'tag'
                        ref_name = parts[2] if len(parts) > 2 else None
                        
                        if ref_type == 'branch' and ref_name:
                            self.download_theme_from_github(github_repo, theme_slug, branch=ref_name)
                        elif ref_type == 'tag' and ref_name:
                            self.download_theme_from_github(github_repo, theme_slug, tag=ref_name)
                        else:
                            self.download_theme_from_github(github_repo, theme_slug)
        
        # Download URL plugins
        url_plugins = config.get('url_plugins', {})
        if url_plugins:
            for plugin_slug, url in url_plugins.items():
                if isinstance(url, str):
                    self.download_plugin_from_url(url, plugin_slug)
        
        # Download URL themes
        url_themes = config.get('url_themes', {})
        if url_themes:
            for theme_slug, url in url_themes.items():
                if isinstance(url, str):
                    self.download_theme_from_url(url, theme_slug)
    
    def download_plugin(self, plugin_slug):
        """Download a plugin from WordPress.org"""
        plugin_dir = f"{self.wp_content_dir}/plugins/{plugin_slug}"

        if not os.path.exists(plugin_dir):
            try:
                # Create temp directory for plugin download
                temp_dir = f"/tmp/wo_plugin_{plugin_slug}"
                os.makedirs(temp_dir, exist_ok=True)

                # Download plugin zip from wordpress.org
                plugin_url = f"https://downloads.wordpress.org/plugin/{plugin_slug}.latest-stable.zip"
                zip_file = f"{temp_dir}/{plugin_slug}.zip"

                # Download using curl
                download_cmd = ['curl', '-L', '-o', zip_file, plugin_url]
                result = subprocess.run(download_cmd, capture_output=True, text=True, check=False)

                if result.returncode == 0 and os.path.exists(zip_file):
                    # Extract the plugin
                    unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
                    subprocess.run(unzip_cmd, capture_output=True, check=False)

                    # Move to shared plugins directory
                    extracted_plugin = f"{temp_dir}/{plugin_slug}"
                    if os.path.exists(extracted_plugin):
                        shutil.move(extracted_plugin, plugin_dir)
                        Log.debug(self.app, f"Downloaded plugin: {plugin_slug}")
                    else:
                        Log.debug(self.app, f"Plugin extraction failed for: {plugin_slug}")
                else:
                    Log.debug(self.app, f"Plugin download failed for: {plugin_slug}")

                # Cleanup temp directory
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)

            except Exception as e:
                Log.debug(self.app, f"Could not download plugin {plugin_slug}: {e}")
    
    def download_theme(self, theme_slug):
        """Download a theme from WordPress.org"""
        theme_dir = f"{self.wp_content_dir}/themes/{theme_slug}"

        if not os.path.exists(theme_dir):
            try:
                Log.debug(self.app, f"Downloading theme: {theme_slug}")

                # Create temp directory for theme download
                temp_dir = f"/tmp/wo_theme_{theme_slug}"
                os.makedirs(temp_dir, exist_ok=True)

                # Download theme zip from wordpress.org
                theme_url = f"https://downloads.wordpress.org/theme/{theme_slug}.latest-stable.zip"
                zip_file = f"{temp_dir}/{theme_slug}.zip"

                # Download using curl
                download_cmd = ['curl', '-L', '-o', zip_file, theme_url]
                result = subprocess.run(download_cmd, capture_output=True, text=True, check=False)

                if result.returncode == 0 and os.path.exists(zip_file):
                    # Extract the theme
                    unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
                    subprocess.run(unzip_cmd, capture_output=True, check=False)

                    # Move to shared themes directory
                    extracted_theme = f"{temp_dir}/{theme_slug}"
                    if os.path.exists(extracted_theme):
                        shutil.move(extracted_theme, theme_dir)
                        Log.debug(self.app, f"Downloaded theme: {theme_slug}")
                    else:
                        Log.debug(self.app, f"Theme extraction failed for: {theme_slug}")
                else:
                    Log.debug(self.app, f"Theme download failed for: {theme_slug}")

                # Cleanup temp directory
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)

            except Exception as e:
                Log.debug(self.app, f"Could not download theme {theme_slug}: {e}")


    # ==========================================
    # PHASE 3: GitHub Download Support
    # ==========================================
    
    def download_plugin_from_github(self, github_repo, plugin_slug, branch=None, tag=None):
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
        try:
            plugin_dir = f"{self.wp_content_dir}/plugins/{plugin_slug}"
            
            # Check if plugin already exists on disk
            if os.path.exists(plugin_dir):
                Log.debug(self.app, f"Plugin {plugin_slug} already exists, skipping download")
                return True
            
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
                # Try 'main' branch first (GitHub's default for new repos)
                url = f"https://github.com/{github_repo}/archive/refs/heads/main.zip"
                Log.debug(self.app, f"Downloading from GitHub (trying 'main' branch)")
            
            # Create temporary directory for download and extraction
            temp_dir = f"/tmp/wo_github_{plugin_slug}"
            os.makedirs(temp_dir, exist_ok=True)
            zip_file = f"{temp_dir}/{plugin_slug}.zip"
            
            # Download the zip file using curl with follow redirects (-L)
            download_cmd = ['curl', '-L', '-o', zip_file, url]
            result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # If download failed and we were trying 'main', fallback to 'master'
            if result.returncode != 0 and not branch and not tag:
                Log.debug(self.app, "Failed to download 'main', trying 'master' branch")
                url = f"https://github.com/{github_repo}/archive/refs/heads/master.zip"
                download_cmd = ['curl', '-L', '-o', zip_file, url]
                result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # Verify download was successful
            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Failed to download from GitHub: {github_repo}")
                # Cleanup temp directory
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Extract the downloaded zip file
            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            result = subprocess.run(unzip_cmd, capture_output=True)
            
            if result.returncode != 0:
                Log.debug(self.app, "Failed to extract GitHub archive")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
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
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Move the extracted directory to the plugins directory with correct name
            shutil.move(extracted, plugin_dir)
            
            # Cleanup temporary directory
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            
            Log.debug(self.app, f"Successfully downloaded plugin from GitHub: {github_repo}")
            return True
            
        except Exception as e:
            Log.debug(self.app, f"GitHub plugin download failed: {e}")
            # Ensure cleanup on error
            if 'temp_dir' in locals() and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return False
    
    def download_plugin_from_url(self, url, plugin_slug):
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
        try:
            plugin_dir = f"{self.wp_content_dir}/plugins/{plugin_slug}"
            
            # Check if plugin already exists
            if os.path.exists(plugin_dir):
                Log.debug(self.app, f"Plugin {plugin_slug} already exists, skipping download")
                return True
            
            # Create temporary directory for download
            temp_dir = f"/tmp/wo_url_{plugin_slug}"
            os.makedirs(temp_dir, exist_ok=True)
            zip_file = f"{temp_dir}/{plugin_slug}.zip"
            
            Log.debug(self.app, f"Downloading plugin from URL: {url}")
            
            # Download the file using curl with follow redirects
            download_cmd = ['curl', '-L', '-o', zip_file, url]
            result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # Verify download was successful
            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Failed to download from URL: {url}")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Verify it's actually a zip file (check magic bytes)
            with open(zip_file, 'rb') as f:
                magic = f.read(4)
                # ZIP files start with 'PK\x03\x04' or 'PK\x05\x06' (empty archive)
                if not (magic[:2] == b'PK'):
                    Log.debug(self.app, "Downloaded file is not a valid ZIP archive")
                    if os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir)
                    return False
            
            # Extract the zip file
            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            result = subprocess.run(unzip_cmd, capture_output=True)
            
            if result.returncode != 0:
                Log.debug(self.app, "Failed to extract plugin zip")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
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
                # In this case, create a directory and move all PHP files there
                Log.debug(self.app, "No plugin directory found, checking for flat structure")
                php_files = [f for f in os.listdir(temp_dir) if f.endswith('.php')]
                if php_files:
                    # Create plugin directory and move contents
                    os.makedirs(plugin_dir, exist_ok=True)
                    for item in os.listdir(temp_dir):
                        if item != plugin_slug + '.zip':
                            src = f"{temp_dir}/{item}"
                            dst = f"{plugin_dir}/{item}"
                            if os.path.isfile(src):
                                shutil.move(src, dst)
                    if os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir)
                    Log.debug(self.app, f"Downloaded plugin from URL (flat structure)")
                    return True
                else:
                    Log.debug(self.app, "No valid plugin structure found in archive")
                    if os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir)
                    return False
            
            # Move extracted directory to plugins directory
            shutil.move(extracted, plugin_dir)
            
            # Cleanup temporary directory
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            
            Log.debug(self.app, f"Successfully downloaded plugin from URL")
            return True
            
        except Exception as e:
            Log.debug(self.app, f"URL plugin download failed: {e}")
            if 'temp_dir' in locals() and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return False
    
    def download_theme_from_github(self, github_repo, theme_slug, branch=None, tag=None):
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
        try:
            theme_dir = f"{self.wp_content_dir}/themes/{theme_slug}"
            
            # Check if theme already exists
            if os.path.exists(theme_dir):
                Log.debug(self.app, f"Theme {theme_slug} already exists, skipping download")
                return True
            
            # Construct GitHub URL (same logic as plugins)
            if tag:
                url = f"https://github.com/{github_repo}/archive/refs/tags/{tag}.zip"
                Log.debug(self.app, f"Downloading theme from GitHub tag: {tag}")
            elif branch:
                url = f"https://github.com/{github_repo}/archive/refs/heads/{branch}.zip"
                Log.debug(self.app, f"Downloading theme from GitHub branch: {branch}")
            else:
                url = f"https://github.com/{github_repo}/archive/refs/heads/main.zip"
                Log.debug(self.app, f"Downloading theme from GitHub (trying 'main' branch)")
            
            # Create temporary directory
            temp_dir = f"/tmp/wo_github_theme_{theme_slug}"
            os.makedirs(temp_dir, exist_ok=True)
            zip_file = f"{temp_dir}/{theme_slug}.zip"
            
            # Download
            download_cmd = ['curl', '-L', '-o', zip_file, url]
            result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # Fallback to master if main failed
            if result.returncode != 0 and not branch and not tag:
                Log.debug(self.app, "Failed to download 'main', trying 'master' branch")
                url = f"https://github.com/{github_repo}/archive/refs/heads/master.zip"
                download_cmd = ['curl', '-L', '-o', zip_file, url]
                result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # Verify download
            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Failed to download theme from GitHub: {github_repo}")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Extract
            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            result = subprocess.run(unzip_cmd, capture_output=True)
            
            if result.returncode != 0:
                Log.debug(self.app, "Failed to extract GitHub theme archive")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
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
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Move to themes directory
            shutil.move(extracted, theme_dir)
            
            # Cleanup
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            
            Log.debug(self.app, f"Successfully downloaded theme from GitHub: {github_repo}")
            return True
            
        except Exception as e:
            Log.debug(self.app, f"GitHub theme download failed: {e}")
            if 'temp_dir' in locals() and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return False
    
    def download_theme_from_url(self, url, theme_slug):
        """
        Download a theme from a direct URL
        
        Downloads a theme zip file from any URL and extracts it to shared themes directory.
        
        Args:
            url (str): Direct URL to the theme zip file
            theme_slug (str): Local directory name for the theme
        
        Returns:
            bool: True if download and extraction successful, False otherwise
        """
        try:
            theme_dir = f"{self.wp_content_dir}/themes/{theme_slug}"
            
            # Check if theme already exists
            if os.path.exists(theme_dir):
                Log.debug(self.app, f"Theme {theme_slug} already exists, skipping download")
                return True
            
            # Create temporary directory
            temp_dir = f"/tmp/wo_url_theme_{theme_slug}"
            os.makedirs(temp_dir, exist_ok=True)
            zip_file = f"{temp_dir}/{theme_slug}.zip"
            
            Log.debug(self.app, f"Downloading theme from URL: {url}")
            
            # Download
            download_cmd = ['curl', '-L', '-o', zip_file, url]
            result = subprocess.run(download_cmd, capture_output=True, text=True)
            
            # Verify download
            if result.returncode != 0 or not os.path.exists(zip_file):
                Log.debug(self.app, f"Failed to download theme from URL: {url}")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Verify it's a zip file
            with open(zip_file, 'rb') as f:
                magic = f.read(4)
                if not (magic[:2] == b'PK'):
                    Log.debug(self.app, "Downloaded file is not a valid ZIP archive")
                    if os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir)
                    return False
            
            # Extract
            unzip_cmd = ['unzip', '-q', zip_file, '-d', temp_dir]
            result = subprocess.run(unzip_cmd, capture_output=True)
            
            if result.returncode != 0:
                Log.debug(self.app, "Failed to extract theme zip")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Find extracted directory
            extracted = None
            for item in os.listdir(temp_dir):
                item_path = f"{temp_dir}/{item}"
                if os.path.isdir(item_path) and item not in ['__MACOSX']:
                    extracted = item_path
                    break
            
            if not extracted:
                Log.debug(self.app, "No theme directory found in archive")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                return False
            
            # Move to themes directory
            shutil.move(extracted, theme_dir)
            
            # Cleanup
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            
            Log.debug(self.app, f"Successfully downloaded theme from URL")
            return True
            
        except Exception as e:
            Log.debug(self.app, f"URL theme download failed: {e}")
            if 'temp_dir' in locals() and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return False
    
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
        """Create baseline configuration file"""
        # Collect all plugins: baseline + GitHub + URL
        all_plugins = list(config.get('baseline_plugins', ['nginx-helper']))
        
        # Add GitHub plugins
        github_plugins = config.get('github_plugins', {})
        if github_plugins:
            for plugin_slug in github_plugins.keys():
                if plugin_slug not in all_plugins:
                    all_plugins.append(plugin_slug)
        
        # Add URL plugins
        url_plugins = config.get('url_plugins', {})
        if url_plugins:
            for plugin_slug in url_plugins.keys():
                if plugin_slug not in all_plugins:
                    all_plugins.append(plugin_slug)
        
        # Determine baseline version (increment if config changed)
        baseline_file = f"{self.config_dir}/baseline.json"
        current_version = 1
        
        if os.path.exists(baseline_file):
            try:
                with open(baseline_file, 'r') as f:
                    old_baseline = json.load(f)
                    old_version = old_baseline.get('version', 1)
                    old_plugins = old_baseline.get('plugins', [])
                    old_theme = old_baseline.get('theme', '')
                    
                    # Check if configuration changed
                    plugins_changed = set(old_plugins) != set(all_plugins)
                    theme_changed = old_theme != config.get('baseline_theme', 'twentytwentyfour')
                    
                    if plugins_changed or theme_changed:
                        current_version = old_version + 1
                        Log.info(self.app, f"   Baseline configuration changed - incrementing version to {current_version}")
                    else:
                        current_version = old_version
            except:
                current_version = 1
        
        baseline = {
            'version': current_version,
            'generated': datetime.now().isoformat(),
            'plugins': all_plugins,
            'theme': config.get('baseline_theme', 'twentytwentyfour'),
            'options': {
                'blog_public': 1,
                'default_comment_status': 'closed',
                'default_ping_status': 'closed'
            }
        }
        
        with open(baseline_file, 'w') as f:
            json.dump(baseline, f, indent=2)
        
        Log.debug(self.app, "Created baseline configuration")
    
    def create_mu_plugin(self):
        """Create MU-plugin for baseline enforcement"""
        mu_plugin = f"{self.wp_content_dir}/mu-plugins/wo-baseline-enforcer.php"
        
        with open(mu_plugin, 'w') as f:
            f.write(self.get_mu_plugin_content())
        
        Log.debug(self.app, "Created baseline enforcer MU-plugin")
    
    def get_mu_plugin_content(self):
        """Get MU-plugin PHP code"""
        return '''<?php
/**
 * WordOps Multi-tenancy Baseline Enforcer
 * Ensures all sites maintain baseline configuration
 */

// Do not run during installation or before tables are created
if (defined('WP_INSTALLING') && WP_INSTALLING) {
    return;
}

// Skip if WordPress not fully installed (no tables yet)
if (!function_exists('is_blog_installed')) {
    require_once ABSPATH . 'wp-includes/load.php';
}
if (!function_exists('is_blog_installed') || !is_blog_installed()) {
    return;
}

// Only run in admin or CLI contexts
if (!is_admin() && !defined('WP_CLI')) {
    return;
}

add_action('init', function() {
    // Skip on AJAX requests
    if (defined('DOING_AJAX') && DOING_AJAX) {
        return;
    }
    
    $version_option = 'wo_mt_baseline_version';
    
    // Find baseline configuration
    $config_file = dirname(dirname(__DIR__)) . '/config/baseline.json';
    
    if (!file_exists($config_file)) {
        return;
    }
    
    $config = json_decode(file_get_contents($config_file), true);
    if (!is_array($config) || empty($config['version'])) {
        return;
    }
    
    // Check if update needed
    $current_version = (int) get_option($version_option, 0);
    $target_version = (int) $config['version'];
    
    if ($current_version >= $target_version) {
        return;
    }
    
    // Load plugin functions
    if (!function_exists('activate_plugin')) {
        require_once ABSPATH . 'wp-admin/includes/plugin.php';
    }
    
    // Activate plugins
    if (!empty($config['plugins']) && is_array($config['plugins'])) {
        foreach ($config['plugins'] as $plugin_slug) {
            // Try to find and activate the plugin
            $plugin_file = null;
            $candidates = [
                $plugin_slug . '/' . $plugin_slug . '.php',
                $plugin_slug . '/index.php',
                $plugin_slug . '/plugin.php',
            ];
            
            foreach ($candidates as $candidate) {
                if (file_exists(WP_PLUGIN_DIR . '/' . $candidate)) {
                    $plugin_file = $candidate;
                    break;
                }
            }
            
            if ($plugin_file && !is_plugin_active($plugin_file)) {
                activate_plugin($plugin_file, '', false, true);
            }
        }
    }
    
    // Activate theme
    if (!empty($config['theme'])) {
        $theme = wp_get_theme($config['theme']);
        if ($theme->exists() && get_option('stylesheet') !== $config['theme']) {
            switch_theme($config['theme']);
        }
    }
    
    // Apply default options
    if (!empty($config['options']) && is_array($config['options'])) {
        foreach ($config['options'] as $option_name => $option_value) {
            if (get_option($option_name) === false) {
                update_option($option_name, $option_value);
            }
        }
    }
    
    // Update version
    update_option($version_option, $target_version);
    
    // Clear caches
    if (function_exists('wp_cache_flush')) {
        wp_cache_flush();
    }
}, 5);
'''
    
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
        
        # Remove old symlink if exists
        if os.path.islink(current_link):
            os.unlink(current_link)
        
        # Create new symlink
        os.symlink(release_path, current_link)
        Log.debug(self.app, f"Switched to release: {release_name}")
    
    def update_plugins_and_themes(self, config):
        """Update all plugins and themes"""
        
        # Update plugins
        plugins_dir = f"{self.wp_content_dir}/plugins"
        for plugin in os.listdir(plugins_dir):
            plugin_path = f"{plugins_dir}/{plugin}"
            if os.path.isdir(plugin_path):
                try:
                    # Try to update using WP-CLI (needs a working WordPress)
                    # In production, this would be more sophisticated
                    Log.debug(self.app, f"Would update plugin: {plugin}")
                except:
                    pass
        
        # Re-download baseline plugins to ensure latest versions
        for plugin in config.get('baseline_plugins', []):
            self.download_plugin(plugin)
    
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
            
            # Stage baseline.json
            subprocess.run(
                ['git', 'add', 'config/baseline.json'],
                cwd=self.shared_root,
                capture_output=True,
                check=True
            )
            
            # Commit with message
            subprocess.run(
                ['git', 'commit', '-m', message],
                cwd=self.shared_root,
                capture_output=True,
                check=True
            )
            
            Log.debug(self.app, f"Git commit: {message}")
            return True
            
        except subprocess.CalledProcessError:
            # No changes to commit (this is okay)
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
        
        if len(releases) > keep_count:
            to_remove = releases[keep_count:]
            
            for release in to_remove:
                # Never remove current release
                if release != current:
                    release_path = f"{self.releases_dir}/{release}"
                    if os.path.exists(release_path):
                        shutil.rmtree(release_path)
                        Log.debug(self.app, f"Removed old release: {release}")

class BaselineApplicator:
    """Helper class for applying baseline configuration to sites"""
    
    @staticmethod
    def find_plugin_main_file(site_path, plugin_slug):
        """Find the main PHP file for a plugin"""
        plugin_dir = f"{site_path}/wp-content/plugins/{plugin_slug}"
        
        if not os.path.exists(plugin_dir):
            return None
        
        # Common patterns
        candidates = [
            f"{plugin_slug}/{plugin_slug}.php",
            f"{plugin_slug}/index.php",
            f"{plugin_slug}/plugin.php",
            f"{plugin_slug}.php"  # Single-file plugin
        ]
        
        for candidate in candidates:
            full_path = f"{site_path}/wp-content/plugins/{candidate}"
            if os.path.exists(full_path):
                return candidate
        
        return None
    
    @staticmethod
    def restore_plugins_from_json(app, site_path, plugins_json):
        """Restore active_plugins option from JSON string"""
        try:
            restore_cmd = [
                'wp', 'option', 'update', 'active_plugins', plugins_json,
                '--format=json',
                '--path=' + site_path,
                '--allow-root'
            ]
            
            subprocess.run(
                restore_cmd,
                capture_output=True,
                timeout=30,
                check=True
            )
            
            Log.debug(app, f"Restored plugins for {site_path}")
            
        except Exception as e:
            Log.debug(app, f"Failed to restore plugins: {e}")
    
    @staticmethod
    def apply_baseline_to_site(app, domain, site_path, baseline):
        """Apply baseline configuration to a single site via WP-CLI"""
        
        result = {'success': False, 'error': None}
        
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
                timeout=30
            )
            
            if plugins_result.returncode != 0:
                result['error'] = "Could not read current plugins"
                return result
            
            current_plugins = plugins_result.stdout.strip()
            
            # Activate each baseline plugin
            for plugin_slug in baseline.get('plugins', []):
                # Find plugin main file
                plugin_file = BaselineApplicator.find_plugin_main_file(
                    site_path, 
                    plugin_slug
                )
                
                if not plugin_file:
                    result['error'] = f"Plugin {plugin_slug} not found on disk"
                    return result
                
                # Activate plugin
                activate_cmd = [
                    'wp', 'plugin', 'activate', plugin_file,
                    '--path=' + site_path,
                    '--allow-root'
                ]
                
                activate_result = subprocess.run(
                    activate_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if activate_result.returncode != 0:
                    result['error'] = f"Failed to activate {plugin_slug}: " + \
                                    activate_result.stderr
                    # Rollback - restore original plugins
                    BaselineApplicator.restore_plugins_from_json(
                        app, 
                        site_path, 
                        current_plugins
                    )
                    return result
            
            # Switch theme if needed
            theme_slug = baseline.get('theme')
            if theme_slug:
                theme_cmd = [
                    'wp', 'theme', 'activate', theme_slug,
                    '--path=' + site_path,
                    '--allow-root'
                ]
                
                theme_result = subprocess.run(
                    theme_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if theme_result.returncode != 0:
                    result['error'] = f"Failed to activate theme {theme_slug}"
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
    def apply_baseline_to_sites(app, config, baseline_version):
        """Apply current baseline to all sites via WP-CLI"""
        from wo.cli.plugins.multitenancy_db import MTDatabase
        
        # Get baseline config
        shared_root = config.get('shared_root', '/var/www/shared')
        baseline_file = f"{shared_root}/config/baseline.json"
        
        with open(baseline_file, 'r') as f:
            baseline = json.load(f)

        # *** PHASE 2: TEST ON STAGING SITE FIRST ***
        staging_site = MTDatabase.get_staging_site(app)
        
        if staging_site:
            Log.info(app, f"Testing on staging site: {staging_site['domain']}...")
            
            result = BaselineApplicator.apply_baseline_to_site(
                app,
                staging_site['domain'],
                staging_site['site_path'],
                baseline
            )
            
            if not result['success']:
                Log.error(app, "=" * 60)
                Log.error(app, f"❌ STAGING TEST FAILED: {result['error']}")
                Log.error(app, "=" * 60)
                Log.error(app, "Aborting production rollout!")
                Log.error(app, f"Fix the issue on staging site: {staging_site['domain']}")
                Log.error(app, "Then try again.")
                return
            
            Log.info(app, "✅ Staging test PASSED")
            Log.info(app, "")
        else:
            Log.warn(app, "⚠️  No staging site found")
            Log.warn(app, "   Skipping pre-production test (NOT RECOMMENDED)")
            Log.warn(app, "   Create one with: wo multitenancy staging create <domain>")
            Log.warn(app, "")
            
            # Ask for confirmation
            if not hasattr(app.pargs, 'force') or not app.pargs.force:
                try:
                    confirm = input("Continue without staging test? [y/N]: ").strip().lower()
                    if confirm != 'y':
                        Log.info(app, "Aborted by user")
                        return
                except:
                    pass  # If input fails (non-interactive), continue
        
        
        # Get all production sites (not staging, not quarantined)
        from wo.core.database import db_session
        from wo.cli.plugins.multitenancy_db import MultitenancySite
        
        session = db_session
        sites = session.query(MultitenancySite).filter_by(is_enabled=True).all()
        
        production_sites = [
            {
                'domain': s.domain,
                'site_path': s.site_path,
                'is_staging': s.is_staging,
                'is_quarantined': s.is_quarantined
            }
            for s in sites 
            if not getattr(s, 'is_staging', False) 
            and not getattr(s, 'is_quarantined', False)
        ]
        
        if not production_sites:
            Log.error(app, "No production sites found")
            return
        
        Log.info(app, f"Applying baseline v{baseline_version} to {len(production_sites)} sites...")
        
        success_count = 0
        quarantine_count = 0
        
        for site in production_sites:
            domain = site['domain']
            site_path = site['site_path']
            
            # Apply baseline to this site
            result = BaselineApplicator.apply_baseline_to_site(
                app, 
                domain, 
                site_path, 
                baseline
            )
            
            if result['success']:
                # Update baseline version in DB
                site_obj = session.query(MultitenancySite).filter_by(domain=domain).first()
                if site_obj:
                    site_obj.baseline_version = baseline_version
                    site_obj.updated_at = datetime.now()
                    session.commit()
                
                success_count += 1
                Log.debug(app, f"Applied to {domain}")
            else:
                # Quarantine the site
                MTDatabase.mark_site_quarantined(
                    app, 
                    domain, 
                    result['error']
                )
                quarantine_count += 1
                Log.warn(app, f"Quarantined {domain}: {result['error']}")
        
        # Clear global cache
        Log.info(app, "Clearing cache globally...")
        from wo.core.shellexec import WOShellExec
        WOShellExec.cmd_exec(app, "wo clean --all", errormsg="", log=False)
        
        # Report results
        Log.info(app, "")
        Log.info(app, "=" * 60)
        Log.info(app, f"✅ Successfully applied to {success_count}/{len(production_sites)} sites")
        
        if quarantine_count > 0:
            Log.warn(app, f"⚠️  {quarantine_count} site(s) quarantined due to errors")
            Log.info(app, "Run 'wo multitenancy baseline validate' to review")
        
        Log.info(app, "=" * 60)
