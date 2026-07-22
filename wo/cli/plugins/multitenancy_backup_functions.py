"""Core helpers for multi-tenancy fleet backups.

The backup command modules deliberately keep orchestration out of this module.  The
functions here own the small, testable pieces shared by backup, restore, status and
fleet operations: configuration, restic invocation, locking, and local state.
"""

import configparser
import contextlib
import datetime as _datetime
import functools
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass

import fcntl
import requests

from wo.core.logging import Log

try:
    from wo.cli.plugins.multitenancy_db import MultitenancySite
    from wo.core.database import db_session
except Exception:  # pragma: no cover - allows lightweight tooling to import us
    MultitenancySite = None
    db_session = None

try:
    from wo.cli.plugins.sitedb import getSiteInfo
except Exception:  # pragma: no cover - see above
    getSiteInfo = None


BACKUP_ENV_FILE = '/etc/wo/backup.env'
RESTIC_BIN = '/usr/local/bin/restic'
RESTIC_VERSION = '0.19.1'
RESTIC_SHA256 = {
    'amd64': 'f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c',
    'arm64': 'a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465',
}
RESTIC_URL = ('https://github.com/restic/restic/releases/download/v{v}/'
              'restic_{v}_linux_{arch}.bz2')
BACKUP_ROOT = '/var/lib/wo-backup'
STAGING_DIR = '/var/lib/wo-backup/staging'
RESTORE_ROOT = '/var/lib/wo-backup/restore'
TOMBSTONE_DIR = '/var/lib/wo-backup/tombstones'
QUARANTINE_ROOT = '/var/lib/wo-backup/quarantine'
STATE_FILE = '/var/lib/wo-backup/state.json'
CACHE_DIR = '/var/cache/restic'
LOCK_FILE = '/var/lock/wo-mt-operation.lock'
CRON_FILE = '/etc/cron.d/wo-backup'
LOG_FILE = '/var/log/wo/backup.log'

GATE_EXCLUDES = [
    '/var/www/*/conf/nginx/multitenancy-maintenance.conf',
    '/var/www/*/htdocs/maintenance.html',
]


class BackupError(Exception):
    """An operational backup or restore failure."""


class BackupConfigError(BackupError):
    """The backup subsystem is not configured or has invalid configuration."""


class OperationLockBusy(BackupError):
    """The shared fleet operation lock is held by another process."""

    def __init__(self, holder):
        self.holder = holder
        super().__init__('operation lock busy: {}'.format(holder or 'unknown'))


_CONFIG_DEFAULTS = {
    'enable_backup': True,
    'db_schedule_minute': 7,
    'files_schedule': '03:10',
    'prune_schedule': 'Sun 04:00',
    'keep_db': '24h,7d,4w,3m',
    'keep_files': '7d,4w,6m',
    'deleted_tenant_grace': 30,
    'check_schedule': '1 05:00',
    'db_ping_url': '',
    'files_ping_url': '',
    'prune_ping_url': '',
    'check_ping_url': '',
}

_KEEP_OPTIONS = {
    'h': '--keep-hourly',
    'd': '--keep-daily',
    'w': '--keep-weekly',
    'm': '--keep-monthly',
}


class _NullLog:
    """Minimal app-shaped object for the repository's unbound Log helpers."""

    class _Logger:
        def debug(self, *args, **kwargs):
            return None

        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

    def __init__(self):
        self.app = type('App', (), {'log': self._Logger()})()


_NULL_LOG = _NullLog()


def _log_info(message, app=None):
    """Log without requiring a Cement controller (core helpers have no app)."""
    try:
        Log.info(app if app is not None else _NULL_LOG, str(message))
    except Exception:
        try:
            print(message)
        except Exception:
            pass


def _log_warn(message, app=None):
    try:
        Log.warn(app if app is not None else _NULL_LOG, str(message))
    except Exception:
        try:
            print(message)
        except Exception:
            pass


def _parse_grace(spec):
    if isinstance(spec, int):
        if spec < 0:
            raise BackupConfigError('deleted_tenant_grace must not be negative')
        return spec
    value = str(spec).strip()
    match = re.fullmatch(r'(\d+)d', value, flags=re.IGNORECASE)
    if not match:
        raise BackupConfigError(
            "invalid deleted_tenant_grace {!r}; expected <N>d".format(spec))
    return int(match.group(1))


def _get_config_value(config, key, default):
    if not config.has_option('backup', key):
        return default
    try:
        if key == 'enable_backup':
            return config.getboolean('backup', key)
        if key == 'db_schedule_minute':
            value = config.getint('backup', key)
            if not 0 <= value <= 59:
                raise ValueError
            return value
        if key == 'deleted_tenant_grace':
            return _parse_grace(config.get('backup', key))
        return config.get('backup', key).strip()
    except BackupConfigError:
        raise
    except Exception as exc:
        raise BackupConfigError(
            'invalid [backup] option {}: {}'.format(key, exc))


def load_backup_config(app) -> dict:
    """Load the optional ``[backup]`` section and apply plan defaults."""
    config = configparser.ConfigParser()
    config_file = '/etc/wo/plugins.d/multitenancy.conf'
    try:
        config.read(config_file)
    except Exception as exc:
        raise BackupConfigError('unable to read backup configuration: {}'.format(exc))

    result = {}
    for key, default in _CONFIG_DEFAULTS.items():
        result[key] = _get_config_value(config, key, default)
    return result


def parse_keep_policy(spec: str) -> list[str]:
    """Convert compact retention values to restic's keep flags."""
    if spec is None:
        raise BackupConfigError('retention policy is empty')
    values = []
    parts = str(spec).split(',')
    if not parts or any(not part.strip() for part in parts):
        raise BackupConfigError('invalid retention policy {!r}'.format(spec))
    for part in parts:
        match = re.fullmatch(r'([1-9]\d*)\s*([hdwm])', part.strip(), re.IGNORECASE)
        if not match:
            raise BackupConfigError(
                "invalid retention item {!r}; expected N[h|d|w|m]".format(part.strip()))
        values.extend([_KEEP_OPTIONS[match.group(2).lower()], match.group(1)])
    return values


def load_backup_env() -> dict:
    """Read the root-only restic environment file."""
    if not os.path.isfile(BACKUP_ENV_FILE):
        raise BackupConfigError('backup environment file is missing: {}'.format(
            BACKUP_ENV_FILE))
    try:
        mode = os.stat(BACKUP_ENV_FILE).st_mode & 0o777
        if mode != 0o600:
            os.chmod(BACKUP_ENV_FILE, 0o600)
    except OSError as exc:
        raise BackupConfigError('unable to secure backup environment: {}'.format(exc))

    values = {}
    try:
        with open(BACKUP_ENV_FILE, encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('export '):
                    line = line[7:].lstrip()
                if '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                if ((value.startswith('"') and value.endswith('"')) or
                        (value.startswith("'") and value.endswith("'"))):
                    value = value[1:-1]
                if key:
                    values[key] = value
    except OSError as exc:
        raise BackupConfigError('unable to read backup environment: {}'.format(exc))

    required = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
                'RESTIC_REPOSITORY', 'RESTIC_PASSWORD')
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise BackupConfigError(
            'backup environment is incomplete; missing {}'.format(', '.join(missing)))
    values.setdefault('RESTIC_CACHE_DIR', CACHE_DIR)
    return values


def backup_is_configured() -> bool:
    """Return whether credentials and the pinned restic binary are available."""
    if not os.path.exists(RESTIC_BIN):
        return False
    try:
        load_backup_env()
    except BackupConfigError:
        return False
    return True


def restic_env() -> dict:
    """Merge backup.env values over the process environment."""
    env = os.environ.copy()
    env.update(load_backup_env())
    env.setdefault('RESTIC_CACHE_DIR', CACHE_DIR)
    return env


def run_restic(args: list[str], *, retry_lock=True, json_lines=False,
               timeout=3600, check=True) -> subprocess.CompletedProcess:
    """Invoke restic without a shell with a bounded operation timeout."""
    del json_lines  # The caller owns JSON decoding; this flag documents intent.
    argv = [RESTIC_BIN]
    if retry_lock:
        argv.extend(['--retry-lock', '5m'])
    argv.extend(str(arg) for arg in args)
    try:
        return subprocess.run(argv, env=restic_env(), capture_output=True,
                              text=True, check=check, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BackupError('restic timed out after {} seconds'.format(timeout)) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ''
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors='replace')
        tail = str(stderr)[-500:]
        raise BackupError('restic failed ({}): {}'.format(
            exc.returncode, tail)) from exc
    except (OSError, ValueError) as exc:
        raise BackupError('unable to run restic: {}'.format(exc)) from exc


def _summary_from_result(result, duration):
    stdout = result.stdout or ''
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors='replace')
    summary = None
    for line in str(stdout).splitlines():
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get('message_type') == 'summary':
            summary = item
    if summary is None:
        raise BackupError('restic backup produced no summary')
    if not summary.get('snapshot_id'):
        raise BackupError('restic backup summary has no snapshot_id')
    return {
        'snapshot_id': summary.get('snapshot_id'),
        'data_added': summary.get('data_added', 0),
        'duration': duration,
    }


def restic_backup_paths(paths, tags, excludes=()) -> dict:
    """Back up existing filesystem paths and return restic's summary metrics."""
    argv = ['backup', '--json']
    if isinstance(paths, str):
        paths = [paths]
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(excludes, str):
        excludes = [excludes]
    argv.extend(str(path) for path in paths)
    for tag in tags or ():
        argv.extend(['--tag', str(tag)])
    for exclude in excludes or ():
        argv.extend(['--exclude', str(exclude)])
    started = time.monotonic()
    result = run_restic(argv, json_lines=True)
    return _summary_from_result(result, time.monotonic() - started)


def restic_backup_stdin_command(filename, command_argv, tags) -> dict:
    """Run a command under restic's checked stdin-from-command backup mode."""
    argv = ['backup', '--json', '--stdin-from-command', '--stdin-filename',
            str(filename)]
    if isinstance(tags, str):
        tags = [tags]
    for tag in tags or ():
        argv.extend(['--tag', str(tag)])
    argv.extend(['--'])
    argv.extend(str(arg) for arg in command_argv)
    started = time.monotonic()
    result = run_restic(argv, json_lines=True)
    return _summary_from_result(result, time.monotonic() - started)


def _json_output(stdout):
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors='replace')
    text = (stdout or '').strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except ValueError:
        values = []
        for line in text.splitlines():
            try:
                values.append(json.loads(line))
            except ValueError:
                continue
        value = values
    if isinstance(value, dict) and isinstance(value.get('snapshots'), list):
        return value['snapshots']
    return value if isinstance(value, list) else []


def list_snapshots(tags=None) -> list[dict]:
    """List snapshots; each repeated ``--tag`` is an OR group."""
    argv = ['snapshots', '--json']
    if tags:
        if isinstance(tags, str):
            tags = [tags]
        for tag_group in tags:
            argv.extend(['--tag', str(tag_group)])
    result = run_restic(argv)
    snapshots = _json_output(result.stdout)
    if not isinstance(snapshots, list):
        raise BackupError('restic snapshots returned invalid JSON')
    return snapshots


def _snapshot_tags(snapshot):
    tags = snapshot.get('tags', ()) if isinstance(snapshot, dict) else ()
    return set(str(tag) for tag in (tags or ()))


def _snapshot_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = _datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.astimezone(_datetime.timezone.utc)


def _parse_at(value):
    if value is None:
        return None
    text = str(value).strip()
    parsed = None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            parsed = _datetime.datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise BackupError(
            "invalid --at {!r}; expected YYYY-MM-DD[ HH:MM[:SS]]".format(value))
    local_zone = _datetime.datetime.now().astimezone().tzinfo
    return parsed.replace(tzinfo=local_zone).astimezone(_datetime.timezone.utc)


def resolve_snapshot(family, *, domain=None, at=None, snapshot_id=None) -> dict:
    """Resolve a family/site snapshot, newest first, optionally bounded by time."""
    if family not in ('db', 'files'):
        raise BackupError('unknown snapshot family {}'.format(family))
    required = [family]
    if domain:
        required.append('site:{}'.format(domain))
    compound = ','.join(required)
    snapshots = list_snapshots([compound])
    wanted_at = None if snapshot_id is not None else _parse_at(at)

    matches = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        tags = _snapshot_tags(snapshot)
        if any(tag not in tags for tag in required):
            continue
        # Safety captures are explicitly addressed through --operation (or an
        # explicit --snapshot).  They must never become the implicit "latest"
        # source after a restore, since they represent the pre-restore state.
        if snapshot_id is None and "pre-restore" in tags:
            continue
        identifier = snapshot.get("id") or snapshot.get("snapshot_id")
        short_identifier = snapshot.get("short_id")
        if snapshot_id is not None:
            if snapshot_id in (identifier, short_identifier):
                return snapshot
            continue
        timestamp = _snapshot_datetime(snapshot.get('time'))
        if timestamp is None:
            continue
        if wanted_at is not None and timestamp > wanted_at:
            continue
        matches.append((timestamp, snapshot))

    if not matches:
        target = snapshot_id or (domain or family)
        raise BackupError('no snapshot found for {}'.format(target))
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def forget_snapshot_ids(ids, prune=False) -> None:
    """Forget explicit snapshot IDs, optionally pruning unreferenced packs."""
    if isinstance(ids, str):
        ids = [ids]
    else:
        ids = list(ids or ())
    if not ids:
        return None
    argv = ['forget'] + [str(identifier) for identifier in ids]
    argv.extend(['--group-by', 'host,tags'])
    if prune:
        argv.append('--prune')
    run_restic(argv)
    return None


def mysql_defaults_file() -> str:
    """Choose the system MariaDB defaults file, then the root user's file."""
    system_file = '/etc/mysql/conf.d/my.cnf'
    if os.path.exists(system_file):
        return system_file
    return '~/.my.cnf'


def _mysql_binary(primary, fallback):
    return primary if shutil.which(primary) else fallback


def mariadb_dump_argv(db_name) -> list[str]:
    binary = _mysql_binary('mariadb-dump', 'mysqldump')
    return [
        binary,
        '--defaults-file={}'.format(mysql_defaults_file()),
        '--single-transaction',
        '--quick',
        '--routines',
        '--triggers',
        '--events',
        '--hex-blob',
        '--default-character-set=utf8mb4',
        '--max-allowed-packet=256M',
        str(db_name),
    ]


def mysql_argv(db_name=None) -> list[str]:
    binary = _mysql_binary('mariadb', 'mysql')
    argv = [binary, '--defaults-file={}'.format(mysql_defaults_file())]
    if db_name is not None:
        argv.append(str(db_name))
    return argv


@dataclass
class TenantInfo:
    domain: str
    site_path: str
    cache_type: str
    php_version: str
    is_enabled: bool
    is_ssl: bool
    redis_prefix: str
    redis_db: int
    db_name: str
    db_user: str
    db_password: str
    db_host: str


def enumerate_enabled_tenants(app) -> list[TenantInfo]:
    """Query enabled MT rows and join optional SiteDB credentials."""
    if MultitenancySite is None or db_session is None:
        raise BackupError('multi-tenancy database modules are unavailable')
    try:
        rows = (db_session.query(MultitenancySite)
                .filter(MultitenancySite.is_enabled == True).all())
    except Exception as exc:
        try:
            db_session.rollback()
        except Exception:
            pass
        raise BackupError('unable to enumerate enabled tenants: {}'.format(exc)) from exc

    tenants = []
    for row in rows:
        domain = getattr(row, 'domain', None)
        if not domain:
            continue
        try:
            site_row = getSiteInfo(app, domain) if getSiteInfo else None
        except Exception as exc:
            raise BackupError(
                'unable to query credentials for {}: {}'.format(domain, exc)) from exc
        tenants.append(TenantInfo(
            domain=domain,
            site_path=getattr(row, 'site_path', None),
            cache_type=getattr(row, 'cache_type', None),
            php_version=getattr(row, 'php_version', None),
            is_enabled=getattr(row, 'is_enabled', True),
            is_ssl=getattr(row, 'is_ssl', False),
            redis_prefix=getattr(row, 'redis_prefix', None),
            redis_db=getattr(row, 'redis_db', None),
            db_name=getattr(site_row, 'db_name', None) if site_row else None,
            db_user=getattr(site_row, 'db_user', None) if site_row else None,
            db_password=getattr(site_row, 'db_password', None) if site_row else None,
            db_host=getattr(site_row, 'db_host', None) if site_row else None,
        ))
    return tenants


_LOCK_STATE = {'pid': None, 'fd': None, 'count': 0, 'holder': None}


def _lock_contents(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 4096).decode(errors='replace').strip()
    except Exception:
        return ''


def _holder_message(contents):
    text = (contents or '').strip()
    if not text:
        return 'unknown (pid unknown)'
    pieces = text.rsplit(None, 1)
    if len(pieces) == 2 and pieces[1].isdigit():
        return '{} (pid {})'.format(pieces[0], pieces[1])
    return text


@contextlib.contextmanager
def operation_lock(holder: str, blocking: bool):
    """Acquire the shared, per-process-reentrant fleet operation lock."""
    pid = os.getpid()
    if (_LOCK_STATE.get('fd') is not None and _LOCK_STATE.get('pid') == pid):
        _LOCK_STATE['count'] += 1
        try:
            yield
        finally:
            _LOCK_STATE['count'] -= 1
        return

    parent = os.path.dirname(LOCK_FILE)
    if parent:
        try:
            os.makedirs(parent, mode=0o755, exist_ok=True)
        except OSError as exc:
            raise BackupError('unable to create operation lock directory: {}'.format(exc))
    try:
        fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            os.chmod(LOCK_FILE, 0o644)
        except OSError:
            pass
    except OSError as exc:
        raise BackupError('unable to open operation lock: {}'.format(exc))

    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            current = _lock_contents(fd)
            if not blocking:
                raise OperationLockBusy(current or 'unknown')
            _log_info('waiting for {} to finish...'.format(_holder_message(current)))
            fcntl.flock(fd, fcntl.LOCK_EX)
            acquired = True

        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, '{} {}\n'.format(holder, pid).encode())
        try:
            os.fsync(fd)
        except OSError:
            pass
        _LOCK_STATE.update(pid=pid, fd=fd, count=1, holder=holder)
        try:
            yield
        finally:
            _LOCK_STATE['count'] -= 1
            if _LOCK_STATE['count'] == 0:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    try:
                        os.close(fd)
                    finally:
                        _LOCK_STATE.update(pid=None, fd=None, count=0, holder=None)
    except Exception:
        if acquired and _LOCK_STATE.get('fd') != fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            os.close(fd)
        except Exception:
            pass
        raise


def fleet_operation(name):
    """Decorate a controller entry point with a blocking fleet lock.

    Apply this decorator *below* ``@expose`` (that is, immediately above the
    function definition after ``@expose``) so Cement metadata remains attached
    to the wrapper created by this function.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapped(self, *args, **kwargs):
            with operation_lock('multitenancy {}'.format(name), blocking=True):
                return func(self, *args, **kwargs)
        return wrapped
    return decorator


def _atomic_json(path, value, mode=0o600):
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=parent, text=True)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chown(path, 0, 0)
        except (AttributeError, OSError):
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_tombstone(domain) -> bool:
    """Write a durable deletion marker; deletion callers must not be blocked."""
    try:
        path = os.path.join(TOMBSTONE_DIR, '{}.json'.format(domain))
        value = {
            'domain': domain,
            'deleted_at': _datetime.datetime.now(_datetime.timezone.utc)
            .isoformat().replace('+00:00', 'Z'),
        }
        _atomic_json(path, value, mode=0o600)
        return True
    except Exception as exc:
        _log_warn('unable to write backup tombstone for {}: {}'.format(domain, exc))
        return False


def read_tombstones() -> list[dict]:
    result = []
    try:
        names = sorted(os.listdir(TOMBSTONE_DIR))
    except OSError:
        return result
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(TOMBSTONE_DIR, name)
        try:
            with open(path, encoding='utf-8') as stream:
                value = json.load(stream)
            if isinstance(value, dict):
                result.append(value)
        except Exception as exc:
            _log_warn('unable to read backup tombstone {}: {}'.format(path, exc))
    return result


def remove_tombstone(domain) -> None:
    path = os.path.join(TOMBSTONE_DIR, '{}.json'.format(domain))
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log_warn('unable to remove backup tombstone for {}: {}'.format(domain, exc))
    return None


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding='utf-8') as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError('state is not an object')
        return value
    except FileNotFoundError:
        return {}
    except Exception as exc:
        _log_warn('unable to read backup state; starting fresh: {}'.format(exc))
        return {}


def save_state(state) -> None:
    _atomic_json(STATE_FILE, state, mode=0o600)


def record_run(family: str, payload: dict) -> None:
    state = load_state()
    runs = state.get('runs')
    if not isinstance(runs, dict):
        runs = {}
    runs[family] = payload
    state['runs'] = runs
    save_state(state)


def ping_deadman(url, kind='') -> None:
    if not url:
        return None
    target = str(url).rstrip('/')
    if kind:
        if kind not in ('start', 'fail'):
            return None
        target = target + '/' + kind
    try:
        requests.get(target, timeout=10)
    except Exception:
        pass
    return None


def new_operation_id() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime(
        '%Y%m%dT%H%M%SZ-') + secrets.token_hex(3)


def site_file_paths(domain) -> dict:
    return {
        'uploads': '/var/www/{}/htdocs/wp-content/uploads'.format(domain),
        'wp_config': '/var/www/{}/htdocs/wp-config.php'.format(domain),
        'conf_nginx': '/var/www/{}/conf/nginx'.format(domain),
        'vhost': '/etc/nginx/sites-available/{}'.format(domain),
        'force_ssl': '/etc/nginx/conf.d/force-ssl-{}.conf'.format(domain),
    }


def safety_snapshot_files(app, domain, operation_id, extra_tags=()) -> dict | None:
    paths = site_file_paths(domain)
    existing = [path for path in paths.values() if os.path.exists(path)]
    if not existing:
        return None
    tags = ['pre-restore', 'files', 'site:{}'.format(domain),
            'operation:{}'.format(operation_id)]
    tags.extend(str(tag) for tag in (extra_tags or ()))
    return restic_backup_paths(existing, tags, excludes=GATE_EXCLUDES)


def safety_snapshot_db(app, tenant: TenantInfo, operation_id, local_dir,
                       extra_tags=()) -> dict:
    if not getattr(tenant, 'db_name', None):
        raise BackupError('tenant {} has no database name'.format(
            getattr(tenant, 'domain', 'unknown')))
    os.makedirs(local_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(local_dir, 0o700)
    except OSError:
        pass
    dump_path = os.path.join(local_dir, 'pre-restore.sql')
    argv = mariadb_dump_argv(tenant.db_name)
    try:
        with open(dump_path, 'wb') as dump_file:
            result = subprocess.run(argv, stdout=dump_file,
                                    stderr=subprocess.PIPE, check=False,
                                    timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise BackupError('database safety dump timed out after 300 seconds') from exc
    except (OSError, ValueError) as exc:
        raise BackupError('database safety dump failed: {}'.format(exc)) from exc
    if result.returncode != 0:
        stderr = result.stderr or b''
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors='replace')
        raise BackupError('database safety dump failed ({}): {}'.format(
            result.returncode, str(stderr)[-500:]))
    try:
        os.chmod(dump_path, 0o600)
    except OSError:
        pass
    if os.path.getsize(dump_path) <= 0:
        raise BackupError('database safety dump was empty')
    tags = ['pre-restore', 'db', 'site:{}'.format(tenant.domain),
            'operation:{}'.format(operation_id)]
    tags.extend(str(tag) for tag in (extra_tags or ()))
    return restic_backup_stdin_command(
        '{}.sql'.format(tenant.domain), ['cat', dump_path], tags)


def stage_sqlite_copy(dest=STAGING_DIR + '/dbase.db') -> str:
    source = '/var/lib/wo/dbase.db'
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    temporary = '{}.tmp-{}'.format(dest, os.getpid())
    try:
        source_conn = sqlite3.connect(source)
        dest_conn = sqlite3.connect(temporary)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            source_conn.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, dest)
        os.chmod(dest, 0o600)
    except Exception as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise BackupError('unable to stage SQLite database: {}'.format(exc)) from exc
    return dest


def sqlite_integrity_ok(path) -> bool:
    try:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute('PRAGMA integrity_check').fetchone()
        finally:
            connection.close()
        return bool(row and str(row[0]).lower() == 'ok')
    except Exception:
        return False


def free_space_check(path_dir, need_bytes) -> None:
    try:
        stats = os.statvfs(path_dir)
        available = stats.f_bavail * stats.f_frsize
    except OSError as exc:
        raise BackupError('unable to check free space in {}: {}'.format(
            path_dir, exc)) from exc
    if available < need_bytes:
        raise BackupError(
            'insufficient free space in {} ({} bytes available, {} needed)'.format(
                path_dir, available, need_bytes))
    return None


def _parse_clock(value, option):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', str(value).strip())
    if not match:
        raise BackupConfigError('invalid {} schedule {!r}'.format(option, value))
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise BackupConfigError('invalid {} schedule {!r}'.format(option, value))
    return minute, hour


def _cron_lines(config):
    db_minute = int(config['db_schedule_minute'])
    if not 0 <= db_minute <= 59:
        raise BackupConfigError('invalid db_schedule_minute')
    files_minute, files_hour = _parse_clock(config['files_schedule'], 'files_schedule')

    prune_parts = str(config['prune_schedule']).split()
    if len(prune_parts) != 2:
        raise BackupConfigError('invalid prune_schedule {!r}'.format(
            config['prune_schedule']))
    prune_minute, prune_hour = _parse_clock(prune_parts[1], 'prune_schedule')
    prune_day = prune_parts[0]

    check_parts = str(config['check_schedule']).split()
    if len(check_parts) != 2:
        raise BackupConfigError('invalid check_schedule {!r}'.format(
            config['check_schedule']))
    check_dom = int(check_parts[0])
    if not 1 <= check_dom <= 31:
        raise BackupConfigError('invalid check_schedule day')
    check_minute, check_hour = _parse_clock(check_parts[1], 'check_schedule')

    command = '/usr/local/bin/wo'
    return [
        'SHELL=/bin/sh',
        'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        '{} * * * * root {} multitenancy backup run --db --cron >> {} 2>&1'.format(
            db_minute, command, LOG_FILE),
        '{} {} * * * root {} multitenancy backup run --files --cron >> {} 2>&1'.format(
            files_minute, files_hour, command, LOG_FILE),
        '{} {} * * {} root {} multitenancy backup prune --cron >> {} 2>&1'.format(
            prune_minute, prune_hour, prune_day, command, LOG_FILE),
        '{} {} {} * * root {} multitenancy backup check --cron >> {} 2>&1'.format(
            check_minute, check_hour, check_dom, command, LOG_FILE),
        '',
    ]


def _atomic_text(path, text, mode):
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, mode=0o755, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=parent, text=True)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        try:
            os.chown(path, 0, 0)
        except (AttributeError, OSError):
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_backup_cron(app) -> None:
    config = load_backup_config(app)
    if not config.get('enable_backup') or not backup_is_configured():
        try:
            os.unlink(CRON_FILE)
        except FileNotFoundError:
            pass
        return None
    _atomic_text(CRON_FILE, '\n'.join(_cron_lines(config)), 0o644)
    return None


def repair_backup_cron(app) -> None:
    try:
        write_backup_cron(app)
    except Exception as exc:
        _log_warn('unable to repair backup cron: {}'.format(exc), app=app)
    return None


def ensure_backup_dirs():
    """Create the root-only local backup/cache directory tree."""
    for path in (BACKUP_ROOT, STAGING_DIR, RESTORE_ROOT,
                 TOMBSTONE_DIR, QUARANTINE_ROOT, CACHE_DIR):
        os.makedirs(path, mode=0o700, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
