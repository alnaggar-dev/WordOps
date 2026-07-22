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
from datetime import datetime, timezone

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
        self.register('backup', self._check_backup, critical=False)
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
            fallback = (self.config.get('php_version') or '8.4').strip()
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


    def _check_backup(self):
        """Check backup freshness and local operator-attention state."""
        try:
            from wo.cli.plugins.multitenancy_backup_functions import (
                QUARANTINE_ROOT,
                backup_is_configured,
                load_backup_config,
                load_state,
                read_tombstones,
            )
        except Exception as exc:
            return {
                'status': STATUS_ERROR,
                'details': {'error': f'backup module unavailable: {exc}'},
            }

        try:
            config = load_backup_config(self.app) or {}
        except Exception as exc:
            return {
                'status': STATUS_ERROR,
                'details': {'error': f'backup config unavailable: {exc}'},
            }
        if not _backup_config_enabled(config):
            return {
                'status': STATUS_OK,
                'details': {'note': 'backup disabled/unconfigured'},
            }
        try:
            configured = backup_is_configured()
        except Exception as exc:
            return {
                'status': STATUS_ERROR,
                'details': {'error': f'backup configuration check failed: {exc}'},
            }
        if not configured:
            return {
                'status': STATUS_OK,
                'details': {'note': 'backup disabled/unconfigured'},
            }

        try:
            state = load_state() or {}
        except Exception as exc:
            return {
                'status': STATUS_ERROR,
                'details': {'error': f'backup state unavailable: {exc}'},
            }
        runs = state.get('runs') if isinstance(state, dict) else {}
        if not isinstance(runs, dict):
            runs = {}
        db_run = runs.get('db')
        files_run = runs.get('files')
        if not _backup_run_success(db_run) and not _backup_run_success(files_run):
            return {
                'status': STATUS_ERROR,
                'details': {
                    'error': 'backup configured but no successful run recorded',
                },
            }

        reasons = []
        now = datetime.now(timezone.utc)
        db_age = _backup_success_age(db_run, now)
        if db_age is None:
            reasons.append('db family has no successful run recorded')
        elif db_age > 7200:
            reasons.append(
                f'db family last successful run is {_backup_age_text(db_age)} old '
                '(threshold 7200s)'
            )

        files_age = _backup_success_age(files_run, now)
        if files_age is None:
            reasons.append('files family has no successful run recorded')
        elif files_age > 2 * 86400:
            reasons.append(
                f'files family last successful run is {_backup_age_text(files_age)} old '
                '(threshold 172800s)'
            )

        db_duration = _backup_duration(db_run)
        if db_duration is not None and db_duration > 1800:
            reasons.append(
                f'capacity warning: db run duration {_backup_age_text(db_duration)} '
                'exceeds 50% of the hourly period (1800s)'
            )

        try:
            tombstones = read_tombstones() or []
        except Exception as exc:
            reasons.append(f'pending tombstones unavailable: {exc}')
            tombstones = []
        grace_days = _backup_grace_days(config)
        stuck_tombstones = []
        for tombstone in tombstones:
            if not isinstance(tombstone, dict):
                continue
            age = _backup_age_seconds(
                tombstone.get('deleted_at') or tombstone.get('timestamp'),
                now,
            )
            if age is not None and age > grace_days * 86400 + 7 * 86400:
                stuck_tombstones.append(tombstone.get('domain') or 'unknown')
        if stuck_tombstones:
            reasons.append(
                'tombstone sweep stuck past grace + 7d: '
                + ', '.join(str(domain) for domain in stuck_tombstones)
            )

        quarantine = _backup_quarantine_dirs(QUARANTINE_ROOT)
        if quarantine:
            reasons.append(
                'quarantine entries need operator attention: '
                + ', '.join(quarantine)
            )

        anomalies = state.get('anomalies') if isinstance(state, dict) else {}
        orphan_sites = anomalies.get('orphan_sites') if isinstance(anomalies, dict) else []
        if orphan_sites:
            domains = []
            for item in orphan_sites:
                if isinstance(item, dict):
                    domains.append(item.get('domain') or item.get('site') or str(item))
                else:
                    domains.append(str(item))
            reasons.append('orphan site tags: ' + ', '.join(str(domain) for domain in domains))

        details = {
            'db_age_seconds': db_age,
            'files_age_seconds': files_age,
            'db_duration_seconds': db_duration,
        }
        if reasons:
            details['error'] = '; '.join(reasons)
            return {'status': STATUS_ERROR, 'details': details}
        details['note'] = 'backup freshness and local state healthy'
        return {'status': STATUS_OK, 'details': details}

def _backup_config_enabled(config):
    value = (config or {}).get('enable_backup', True)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _backup_parse_time(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _backup_age_seconds(value, now=None):
    parsed = _backup_parse_time(value)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


def _backup_run_success(record):
    if not isinstance(record, dict):
        return False
    value = record.get('ok')
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', 'ok', 'success')
    return bool(value)


def _backup_success_age(record, now):
    if not _backup_run_success(record):
        return None
    for key in ('finished', 'success_at', 'last_success', 'completed_at', 'timestamp'):
        if isinstance(record, dict) and record.get(key):
            age = _backup_age_seconds(record.get(key), now)
            if age is not None:
                return age
    return None


def _backup_duration(record):
    if not isinstance(record, dict):
        return None
    if record.get('duration') is not None:
        try:
            return float(record.get('duration'))
        except (TypeError, ValueError):
            pass
    started = _backup_parse_time(record.get('started'))
    finished = _backup_parse_time(record.get('finished'))
    if started is not None and finished is not None:
        return max(0.0, (finished - started).total_seconds())
    return None


def _backup_age_text(seconds):
    if seconds is None:
        return 'unknown'
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f'{seconds:.1f}s'
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f'{int(minutes)}m {int(remainder)}s'
    hours, remainder = divmod(minutes, 60)
    if hours < 24:
        return f'{int(hours)}h {int(remainder)}m'
    days, remainder = divmod(hours, 24)
    return f'{int(days)}d {int(remainder)}h'


def _backup_grace_days(config):
    raw = (config or {}).get('deleted_tenant_grace_days')
    if raw is None:
        raw = (config or {}).get('deleted_tenant_grace', 30)
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    text = str(raw).strip().lower()
    if text.endswith('d'):
        text = text[:-1]
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return 30.0


def _backup_quarantine_dirs(root):
    try:
        entries = os.scandir(root)
    except OSError:
        return []
    names = []
    try:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    names.append(entry.name)
            except OSError:
                continue
    finally:
        entries.close()
    return sorted(names)


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
            'enabled': bool(site.get('is_enabled')),
        }
    except (urllib.error.URLError, socket.timeout) as exc:
        return {
            'status': STATUS_WARN,
            'url': url,
            'error': str(exc),
            'duration_ms': int((time.monotonic() - start) * 1000),
            'enabled': bool(site.get('is_enabled')),
        }
    except Exception as exc:
        return {
            'status': STATUS_ERROR,
            'url': url,
            'error': str(exc),
            'duration_ms': int((time.monotonic() - start) * 1000),
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
