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
        
        # Convert to dictionary
        result = {}
        if config.has_section('multitenancy'):
            result = dict(config.items('multitenancy'))
        
        # Parse list values
        if 'baseline_plugins' in result:
            result['baseline_plugins'] = [p.strip() for p in result['baseline_plugins'].split(',')]
        
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
define( 'WP_CONTENT_DIR', __DIR__ . '/htdocs/wp-content' );
define( 'WP_CONTENT_URL', (isset($_SERVER['HTTPS']) && $_SERVER['HTTPS'] === 'on' ? 'https' : 'http') . '://{domain}/wp-content' );

// ** WordPress Core Directory ** //
if ( ! defined( 'ABSPATH' ) ) {{
    define( 'ABSPATH', __DIR__ . '/htdocs/wp/' );
}}

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

// ** Load WordPress ** //
require_once ABSPATH . 'wp-settings.php';
"""
        
        wp_config_path = f"{site_root}/wp-config.php"
        with open(wp_config_path, 'w') as f:
            f.write(wp_config)
        
        # Set secure permissions
        os.chmod(wp_config_path, 0o640)
        Log.debug(app, f"Generated wp-config.php for {domain}")
    
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
        """Generate nginx configuration for shared WordPress site using WordOps templates.

        Falls back to a minimal config if template rendering fails.
        """

        # Build data structure expected by WordOps virtualconf.mustache
        data = {
            'site_name': domain,
            'www_domain': f"www.{domain}",
            'static': False,
            'basic': False,
            'wp': True,
            'wpfc': cache_type == 'wpfc',
            'wpredis': cache_type == 'wpredis',
            'wpsc': cache_type == 'wpsc',
            'wprocket': cache_type == 'wprocket',
            'wpce': cache_type == 'wpce',
            'multisite': False,
            'wpsubdir': False,
            'webroot': site_root,
        }

        # Default to basic (no page cache) when not using a specific cache
        if cache_type not in ['wpfc', 'wpredis', 'wpsc', 'wprocket', 'wpce']:
            data['basic'] = True

        # Map PHP version (e.g., 8.2) to WordOps key (e.g., php82)
        try:
            wo_php_key = None
            for key, val in WOVar.wo_php_versions.items():
                if val == php_version:
                    wo_php_key = key
                    break
            if not wo_php_key:
                wo_php_key = f"php{php_version.replace('.', '')}"
            data['wo_php'] = wo_php_key
        except Exception:
            data['wo_php'] = f"php{php_version.replace('.', '')}"

        # Render using WordOps helper
        try:
            from wo.cli.plugins.site_functions import setupdomain, SiteError
            setupdomain(app, data)
            Log.debug(app, f"Generated nginx config for {domain} using WordOps templates")
            return f"/etc/nginx/sites-available/{domain}"
        except Exception as e:
            Log.debug(app, f"Nginx generation via templates failed ({e}), using fallback")
            config_content = MTFunctions.generate_basic_nginx_config(domain, site_root, php_version)
            nginx_conf = f"/etc/nginx/sites-available/{domain}"
            with open(nginx_conf, 'w') as f:
                f.write(config_content)
            return nginx_conf
    
    @staticmethod
    def generate_basic_nginx_config(domain, site_root, php_version):
        """Generate basic nginx configuration"""
        # Correct socket name format: php8.2-fpm.sock
        php_sock = f"php{php_version}-fpm"
        
        return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain} www.{domain};
    
    root {site_root}/htdocs;
    index index.php index.html;
    
    access_log {site_root}/logs/access.log;
    error_log {site_root}/logs/error.log;
    
    # WordPress shared core specific
    location /wp {{
        try_files $uri $uri/ /wp/index.php?$args;
    }}
    
    location / {{
        try_files $uri $uri/ /index.php?$args;
    }}
    
    location ~ \\.php$ {{
        try_files $uri =404;
        fastcgi_split_path_info ^(.+\\.php)(/.+)$;
        fastcgi_pass unix:/var/run/php/{php_sock}.sock;
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
    }}
    
    location ~ /\\. {{
        deny all;
    }}
    
    location ~* \\.(jpg|jpeg|gif|png|webp|svg|woff|woff2|ttf|css|js|ico|xml)$ {{
        access_log off;
        log_not_found off;
        expires 360d;
    }}
}}"""
    
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
        
        # Install WordPress
        try:
            cmd = [
                'wp', 'core', 'install',
                f'--url={site_url}',
                f'--title={domain}',
                f'--admin_user={admin_user}',
                f'--admin_password={admin_pass}',
                f'--admin_email={admin_email}',
                f'--path={site_htdocs}',
                '--skip-email',
                '--allow-root'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
        
        # Activate baseline plugins
        plugins = config.get('baseline_plugins', [])
        for plugin in plugins:
            try:
                cmd = [
                    'wp', 'plugin', 'activate', plugin,
                    f'--path={site_htdocs}',
                    '--allow-root'
                ]
                subprocess.run(cmd, capture_output=True, check=False)
                Log.debug(app, f"Activated plugin {plugin} for {domain}")
            except:
                Log.debug(app, f"Could not activate plugin {plugin}")
        
        # Activate baseline theme
        theme = config.get('baseline_theme', 'twentytwentyfour')
        try:
            cmd = [
                'wp', 'theme', 'activate', theme,
                f'--path={site_htdocs}',
                '--allow-root'
            ]
            subprocess.run(cmd, capture_output=True, check=False)
            Log.debug(app, f"Activated theme {theme} for {domain}")
        except:
            Log.debug(app, f"Could not activate theme {theme}")
    
    @staticmethod
    def setup_ssl(app, domain, pargs):
        """Setup SSL for shared site"""
        from wo.core.acme import WOAcme
        from wo.core.sslutils import SSL
        
        # This is a simplified SSL setup - the actual implementation
        # would use WordOps' existing SSL functions
        Log.info(app, f"SSL setup would be performed for {domain}")
        # TODO: Implement actual SSL setup using WOAcme
    
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
            site_htdocs = f"/var/www/{domain}/htdocs"
            cmd = [
                'wp', 'cache', 'flush',
                f'--path={site_htdocs}',
                '--allow-root'
            ]
            subprocess.run(cmd, capture_output=True, check=False)
        except:
            pass
    
    @staticmethod
    def test_site(app, domain):
        """Test if a site is working"""
        import requests
        
        try:
            response = requests.get(f"http://{domain}", timeout=5)
            return response.status_code < 500
        except:
            return False
    
    @staticmethod
    def backup_site(app, domain, site_root):
        """Create backup of a site"""
        backup_dir = f"{site_root}/backups"
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = f"{backup_dir}/backup-{timestamp}.tar.gz"
        
        with tarfile.open(backup_path, 'w:gz') as tar:
            tar.add(f"{site_root}/htdocs", arcname='htdocs')
            tar.add(f"{site_root}/wp-config.php", arcname='wp-config.php')
        
        return backup_path
    
    @staticmethod
    def update_wp_config_for_shared(app, site_root, site_htdocs):
        """Update wp-config.php when converting to shared"""
        wp_config_path = f"{site_root}/wp-config.php"
        
        if not os.path.exists(wp_config_path):
            Log.error(app, f"wp-config.php not found at {wp_config_path}")
            return
        
        # Read current config
        with open(wp_config_path, 'r') as f:
            config = f.read()
        
        # Update ABSPATH if needed
        if "define( 'ABSPATH'" not in config:
            config = config.replace(
                "require_once ABSPATH . 'wp-settings.php';",
                "define( 'ABSPATH', __DIR__ . '/htdocs/wp/' );\nrequire_once ABSPATH . 'wp-settings.php';"
            )
        
        # Write updated config
        with open(wp_config_path, 'w') as f:
            f.write(config)
    
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
            
            Log.debug(self.app, f"WordPress downloaded: {release_name}")
            return release_name
            
        except subprocess.CalledProcessError as e:
            Log.error(self.app, f"Failed to download WordPress: {e}")
            raise
    
    def seed_plugins_and_themes(self, config):
        """Download initial plugins and themes"""
        
        # Download baseline plugins
        plugins = config.get('baseline_plugins', ['nginx-helper', 'redis-cache'])
        for plugin in plugins:
            self.download_plugin(plugin)
        
        # Download baseline theme
        theme = config.get('baseline_theme', 'twentytwentyfour')
        self.download_theme(theme)
    
    def download_plugin(self, plugin_slug):
        """Download a plugin from WordPress.org"""
        plugin_dir = f"{self.wp_content_dir}/plugins/{plugin_slug}"
        
        if not os.path.exists(plugin_dir):
            try:
                # Download plugin
                cmd = [
                    'wp', 'plugin', 'install', plugin_slug,
                    f'--path={self.releases_dir}/temp',
                    '--allow-root'
                ]
                
                # Create temp WordPress for downloading
                temp_wp = f"{self.releases_dir}/temp"
                os.makedirs(temp_wp, exist_ok=True)
                
                # Run download
                subprocess.run(cmd, check=False, capture_output=True)
                
                # Move plugin to shared location
                downloaded = f"{temp_wp}/wp-content/plugins/{plugin_slug}"
                if os.path.exists(downloaded):
                    shutil.move(downloaded, plugin_dir)
                    Log.debug(self.app, f"Downloaded plugin: {plugin_slug}")
                
                # Cleanup temp
                if os.path.exists(temp_wp):
                    shutil.rmtree(temp_wp)
                    
            except Exception as e:
                Log.debug(self.app, f"Could not download plugin {plugin_slug}: {e}")
    
    def download_theme(self, theme_slug):
        """Download a theme from WordPress.org"""
        theme_dir = f"{self.wp_content_dir}/themes/{theme_slug}"
        
        if not os.path.exists(theme_dir):
            try:
                # Download using curl
                url = f"https://downloads.wordpress.org/theme/{theme_slug}.zip"
                zip_path = f"/tmp/{theme_slug}.zip"
                
                subprocess.run(['curl', '-o', zip_path, url], check=True, capture_output=True)
                
                # Extract theme
                subprocess.run(['unzip', '-q', zip_path, '-d', f"{self.wp_content_dir}/themes/"], 
                             check=True, capture_output=True)
                
                # Cleanup
                os.remove(zip_path)
                Log.debug(self.app, f"Downloaded theme: {theme_slug}")
                
            except Exception as e:
                Log.debug(self.app, f"Could not download theme {theme_slug}: {e}")
    
    def create_baseline_config(self, config):
        """Create baseline configuration file"""
        baseline = {
            'version': 1,
            'generated': datetime.now().isoformat(),
            'plugins': config.get('baseline_plugins', ['nginx-helper']),
            'theme': config.get('baseline_theme', 'twentytwentyfour'),
            'options': {
                'blog_public': 1,
                'default_comment_status': 'closed',
                'default_ping_status': 'closed'
            }
        }
        
        baseline_file = f"{self.config_dir}/baseline.json"
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

// Only run in admin context
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
        for root, dirs, files in os.walk(self.shared_root):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)


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
