"""WordOps Multi-tenancy Health Check Module

Composable `HealthChecker` that aggregates infrastructure, database, service,
and per-site probes into a single status envelope. Each checker is a small,
unit-testable function returning `{status, details, duration_ms}`.
"""

import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime

from wo.core.logging import Log
from wo.cli.plugins.multitenancy_db import MTDatabase
from wo.cli.plugins.multitenancy_functions import MTFunctions


STATUS_OK = 'ok'
STATUS_WARN = 'warn'
STATUS_ERROR = 'error'

OVERALL_HEALTHY = 'healthy'
OVERALL_DEGRADED = 'degraded'
OVERALL_UNHEALTHY = 'unhealthy'


class HealthChecker:
    """Runs a registered set of checkers and aggregates their results."""

    def __init__(self, app):
        self.app = app
        self.config = MTFunctions.load_config(app) if app is not None else {}
        self.shared_root = self.config.get('shared_root', '/var/www/shared')
        self.min_free_gb = float(self.config.get('min_free_space_gb', 2))
        self.checkers = []

    def register(self, name, fn, critical=True):
        self.checkers.append({'name': name, 'fn': fn, 'critical': critical})
        return self

    def register_defaults(self, site_filter=None):
        self.register('shared_infrastructure', self._check_shared_infra, critical=True)
        self.register('database', self._check_database, critical=True)
        self.register('disk_space', self._check_disk_space, critical=False)
        self.register('php_fpm', self._check_php_fpm, critical=True)
        self.register('nginx', self._check_nginx, critical=True)
        self.register(
            'sites',
            lambda: self._check_sites(site_filter=site_filter),
            critical=False,
        )
        return self

    def run_all(self):
        overall = OVERALL_HEALTHY
        checks = {}
        for checker in self.checkers:
            start = time.monotonic()
            try:
                result = checker['fn']()
                if result is None:
                    result = {'status': STATUS_ERROR, 'details': {'error': 'no result'}}
            except Exception as exc:
                result = {'status': STATUS_ERROR, 'details': {'error': str(exc)}}
            result.setdefault('duration_ms', int((time.monotonic() - start) * 1000))
            checks[checker['name']] = result
            status = result.get('status')
            if status != STATUS_OK:
                if checker['critical']:
                    overall = OVERALL_UNHEALTHY
                elif overall == OVERALL_HEALTHY:
                    overall = OVERALL_DEGRADED
        return {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'status': overall,
            'checks': checks,
        }

    # ------------------------------------------------------------------
    # Individual checkers
    # ------------------------------------------------------------------

    def _check_shared_infra(self):
        current_link = os.path.join(self.shared_root, 'current')
        releases_dir = os.path.join(self.shared_root, 'releases')
        details = {
            'shared_root': self.shared_root,
            'current_symlink': current_link,
            'releases_dir_exists': os.path.isdir(releases_dir),
        }
        if not os.path.islink(current_link):
            return {
                'status': STATUS_ERROR,
                'details': {**details, 'error': 'current symlink missing'},
            }
        try:
            target = os.readlink(current_link)
        except OSError as exc:
            return {
                'status': STATUS_ERROR,
                'details': {**details, 'error': f'readlink failed: {exc}'},
            }
        if not os.path.isabs(target):
            target = os.path.normpath(os.path.join(self.shared_root, target))
        details['target'] = target
        if not os.path.isdir(target):
            return {
                'status': STATUS_ERROR,
                'details': {**details, 'error': 'release target does not exist'},
            }
        return {'status': STATUS_OK, 'details': details}

    def _check_database(self):
        try:
            from wo.core.mysql import WOMysql
        except Exception as exc:
            return {'status': STATUS_ERROR, 'details': {'error': str(exc)}}
        start = time.monotonic()
        conn = None
        try:
            conn = WOMysql.connect(self)
            cursor = conn.cursor()
            cursor.execute('SELECT 1')
            cursor.fetchone()
            cursor.close()
            ping_ms = int((time.monotonic() - start) * 1000)
            return {
                'status': STATUS_OK,
                'details': {'ping_ms': ping_ms},
            }
        except Exception as exc:
            return {'status': STATUS_ERROR, 'details': {'error': str(exc)}}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _check_disk_space(self):
        try:
            usage = shutil.disk_usage(self.shared_root)
        except Exception as exc:
            return {'status': STATUS_ERROR, 'details': {'error': str(exc)}}
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        details = {
            'free_gb': round(free_gb, 2),
            'total_gb': round(total_gb, 2),
            'min_free_gb': self.min_free_gb,
        }
        if free_gb < self.min_free_gb:
            return {'status': STATUS_ERROR, 'details': details}
        if free_gb < self.min_free_gb * 2:
            return {'status': STATUS_WARN, 'details': details}
        return {'status': STATUS_OK, 'details': details}

    def _check_php_fpm(self):
        versions = sorted({
            (s.get('php_version') or '').strip()
            for s in MTDatabase.get_shared_sites(self.app)
            if s.get('php_version')
        })
        if not versions:
            # Fall back to the default configured version
            fallback = (self.config.get('php_version') or '8.3').strip()
            versions = [fallback]
        per_version = {}
        overall = STATUS_OK
        for ver in versions:
            svc = f'php{ver}-fpm'
            # WordOps drops the dot in the socket name
            # (get_php_fpm_socket): /var/run/php/php83-fpm.sock. The service
            # unit, however, keeps the dot: php8.3-fpm.
            socket_path = f"/var/run/php/php{ver.replace('.', '')}-fpm.sock"
            svc_up = _service_is_running(svc)
            socket_ok = os.path.exists(socket_path)
            status = STATUS_OK if svc_up and socket_ok else STATUS_ERROR
            if status != STATUS_OK and overall == STATUS_OK:
                overall = STATUS_ERROR
            per_version[ver] = {
                'service': svc,
                'service_running': svc_up,
                'socket_path': socket_path,
                'socket_exists': socket_ok,
                'status': status,
            }
        return {'status': overall, 'details': {'versions': per_version}}

    def _check_nginx(self):
        running = _service_is_running('nginx')
        config_ok, config_msg = _run_cmd('nginx -t')
        status = STATUS_OK if running and config_ok else STATUS_ERROR
        return {
            'status': status,
            'details': {
                'service_running': running,
                'config_test_ok': config_ok,
                'config_test_output': config_msg.strip() if config_msg else '',
            },
        }

    def _check_sites(self, site_filter=None):
        sites = MTDatabase.get_shared_sites(self.app)
        if site_filter:
            sites = [s for s in sites if s.get('domain') == site_filter]
        if not sites:
            return {
                'status': STATUS_OK,
                'details': {'total': 0, 'checked': 0, 'per_site': {}},
            }
        per_site = {}
        overall = STATUS_OK
        for site in sites:
            per_site[site['domain']] = _check_single_site(site)
            if per_site[site['domain']]['status'] != STATUS_OK:
                overall = STATUS_WARN  # per-site failure is non-critical by default
        return {
            'status': overall,
            'details': {
                'total': len(sites),
                'checked': len(per_site),
                'per_site': per_site,
            },
        }


# ---------------------------------------------------------------------------
# Helpers — kept private to the module but importable for unit tests
# ---------------------------------------------------------------------------

def _service_is_running(service_name):
    """Return True if systemd reports the unit as active."""
    try:
        rc, out = subprocess.getstatusoutput(
            f'systemctl is-active --quiet {service_name}'
        )
        if rc == 0:
            return True
    except Exception:
        pass
    # Fallback to `service <name> status`
    try:
        rc, out = subprocess.getstatusoutput(f'service {service_name} status')
        return rc == 0
    except Exception:
        return False


def _run_cmd(command, timeout=10):
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout,
        )
        return proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    except Exception as exc:
        return False, str(exc)


def _check_single_site(site):
    domain = site.get('domain')
    scheme = 'https' if site.get('is_ssl') else 'http'
    url = f"{scheme}://{domain}/wp-login.php"
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'wo-health/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            status_code = getattr(resp, 'status', 200)
            ok = 200 <= status_code < 400
            return {
                'status': STATUS_OK if ok else STATUS_WARN,
                'url': url,
                'http_status': status_code,
                'duration_ms': int((time.monotonic() - start) * 1000),
                'quarantined': bool(site.get('is_quarantined')),
                'enabled': bool(site.get('is_enabled')),
            }
    except urllib.error.HTTPError as exc:
        # A 401/403 on wp-login is still "site up"
        status_code = exc.code
        ok = status_code in (200, 301, 302, 401, 403)
        return {
            'status': STATUS_OK if ok else STATUS_WARN,
            'url': url,
            'http_status': status_code,
            'duration_ms': int((time.monotonic() - start) * 1000),
            'quarantined': bool(site.get('is_quarantined')),
            'enabled': bool(site.get('is_enabled')),
        }
    except (urllib.error.URLError, socket.timeout) as exc:
        return {
            'status': STATUS_WARN,
            'url': url,
            'error': str(exc),
            'duration_ms': int((time.monotonic() - start) * 1000),
            'quarantined': bool(site.get('is_quarantined')),
            'enabled': bool(site.get('is_enabled')),
        }
    except Exception as exc:
        return {
            'status': STATUS_ERROR,
            'url': url,
            'error': str(exc),
            'duration_ms': int((time.monotonic() - start) * 1000),
            'quarantined': bool(site.get('is_quarantined')),
            'enabled': bool(site.get('is_enabled')),
        }


def render_text(result):
    """Human-friendly rendering (used by controller when --json is absent)."""
    lines = []
    overall = result.get('status', 'unknown')
    icon = {
        OVERALL_HEALTHY: '✅',
        OVERALL_DEGRADED: '⚠️',
        OVERALL_UNHEALTHY: '❌',
    }.get(overall, '•')
    lines.append(f"{icon} Overall: {overall}")
    lines.append(f"  timestamp: {result.get('timestamp')}")
    lines.append('')
    for name, check in result.get('checks', {}).items():
        status = check.get('status', 'unknown')
        ico = {
            STATUS_OK: '✅',
            STATUS_WARN: '⚠️',
            STATUS_ERROR: '❌',
        }.get(status, '•')
        dur = check.get('duration_ms')
        dur_part = f" ({dur} ms)" if dur is not None else ''
        lines.append(f"{ico} {name}: {status}{dur_part}")
        details = check.get('details') or {}
        if status != STATUS_OK:
            # Show the keys that matter most
            for key in ('error', 'config_test_output'):
                if details.get(key):
                    lines.append(f"     {key}: {details[key]}")
        if name == 'sites':
            per = details.get('per_site') or {}
            for domain, site_check in list(per.items())[:20]:
                ss = site_check.get('status')
                ssi = {
                    STATUS_OK: '    ✅',
                    STATUS_WARN: '    ⚠️',
                    STATUS_ERROR: '    ❌',
                }.get(ss, '    •')
                http_part = site_check.get('http_status') or site_check.get('error', '')
                lines.append(f"{ssi} {domain}: {ss} ({http_part})")
            if len(per) > 20:
                lines.append(f"    ... and {len(per) - 20} more")
    return '\n'.join(lines)
