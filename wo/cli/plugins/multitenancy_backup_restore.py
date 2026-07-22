"""Single-site restore flows for multi-tenancy backups.

The backup controller deliberately keeps this module separate from the core
restic engine.  The functions at the top of the module are also the small
apply primitives used by the fleet restore implementation.
"""

import json
import shlex
import os
import pwd
import grp
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from wo.core.logging import Log
from wo.core.shellexec import WOShellExec
from wo.cli.plugins.multitenancy_backup_functions import (
    RESTIC_BIN,
    RESTORE_ROOT,
    BackupError,
    OperationLockBusy,
    TenantInfo,
    free_space_check,
    list_snapshots,
    mysql_argv,
    new_operation_id,
    operation_lock,
    resolve_snapshot,
    restic_env,
    run_restic,
    safety_snapshot_db,
    safety_snapshot_files,
    site_file_paths,
)
from wo.cli.plugins.multitenancy_functions import MTFunctions
from wo.cli.plugins.sitedb import getSiteInfo


_DB_DEFINE_NAMES = ('DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST')
_DB_DEFINE_RE = {
    name: re.compile(
        r"(define\(\s*['\"]" + re.escape(name) +
        r"['\"]\s*,\s*['\"])([^'\"]*)(['\"]\s*\))"
    )
    for name in _DB_DEFINE_NAMES
}

_SUBPROCESS_TIMEOUT = 300
_NGINX_TIMEOUT = 30
_NGINX_RELOAD_TIMEOUT = 60

__all__ = [
    'restore_site_files',
    'restore_site_db',
    'flush_site_caches',
    'nginx_test_and_reload',
    'cmd_restore_site',
]


def _value(obj, name, default=None):
    """Read a value from either a row object or a dict-like tenant."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _backup_error(message):
    """Construct a BackupError without assuming a custom constructor."""
    return BackupError(message)


def _staged_path(staged_root, path):
    return os.path.join(staged_root, str(path).lstrip('/'))


def _write_atomic(path, data, mode=None):
    parent = os.path.dirname(path) or '.'
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.wo-restore-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _rewrite_wp_config(path, site_row, app):
    """Rewrite only current DB identity values in a staged wp-config."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            content = fh.read()
    except OSError as exc:
        raise _backup_error(f'could not read staged wp-config.php: {exc}')

    values = {
        'DB_NAME': _value(site_row, 'db_name'),
        'DB_USER': _value(site_row, 'db_user'),
        'DB_PASSWORD': _value(site_row, 'db_password'),
        'DB_HOST': _value(site_row, 'db_host'),
    }
    for name, value in values.items():
        pattern = _DB_DEFINE_RE[name]
        # A callable replacement is required: credentials can contain '$' and
        # backslashes, which are special in a replacement template.
        content, count = pattern.subn(
            lambda match, replacement='' if value is None else str(value):
                match.group(1) + replacement + match.group(3),
            content,
            count=1,
        )
        if count == 0:
            Log.warn(app, f"staged wp-config.php has no define('{name}', ...) line")

    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o640
    _write_atomic(path, content.encode('utf-8'), mode=mode)


def _owner_ids(user, group):
    """Resolve a Unix owner pair, returning ``None`` on non-Linux dev hosts."""
    try:
        return pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid
    except KeyError:
        return None


def _set_owner_mode(app, path, user, group, mode):
    if not os.path.lexists(path):
        return
    ids = _owner_ids(user, group)
    if ids is None:
        Log.warn(app, f"cannot resolve owner {user}:{group} for {path}")
    else:
        try:
            os.chown(path, ids[0], ids[1])
        except OSError as exc:
            raise _backup_error(f'could not set owner on {path}: {exc}')
    try:
        if not os.path.islink(path):
            os.chmod(path, mode)
    except OSError as exc:
        raise _backup_error(f'could not set mode on {path}: {exc}')


def _set_tree_owner_mode(app, path, user, group, dir_mode=0o755,
                         file_mode=0o644):
    if not os.path.lexists(path):
        return
    if os.path.islink(path):
        _set_owner_mode(app, path, user, group, file_mode)
        return
    _set_owner_mode(app, path, user, group, dir_mode)
    for root, dirs, files in os.walk(path):
        for name in dirs:
            _set_owner_mode(app, os.path.join(root, name), user, group, dir_mode)
        for name in files:
            _set_owner_mode(app, os.path.join(root, name), user, group, file_mode)


def _copy_file_replace(staged_path, live_path):
    parent = os.path.dirname(live_path) or '.'
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.wo-restore-', dir=parent)
    os.close(fd)
    try:
        shutil.copyfile(staged_path, tmp)
        try:
            shutil.copymode(staged_path, tmp)
        except OSError:
            pass
        os.replace(tmp, live_path)
    except OSError as exc:
        raise _backup_error(f'could not install restored file {live_path}: {exc}')
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _run_rsync(src, dst, excludes=()):
    argv = ['rsync', '-a', '--delete']
    for exclude in excludes:
        argv.extend(['--exclude', exclude])
    argv.extend([src.rstrip('/') + '/', dst.rstrip('/') + '/'])
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise _backup_error(f'rsync restore timed out: {exc}')
    except OSError as exc:
        raise _backup_error(f'could not run rsync: {exc}')
    if result.returncode != 0:
        detail = (getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or
                  f'exit {result.returncode}').strip()
        raise _backup_error(f'rsync restore failed: {detail}')


def _nginx_test_only(app):
    try:
        result = subprocess.run(
            ['nginx', '-t'],
            capture_output=True,
            text=True,
            check=False,
            timeout=_NGINX_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise _backup_error(f'nginx -t timed out: {exc}')
    except OSError as exc:
        raise _backup_error(f'could not run nginx -t: {exc}')
    if result.returncode != 0:
        detail = (getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or
                  f'exit {result.returncode}').strip()
        raise _backup_error(f'nginx -t failed: {detail}')


def restore_site_files(app, domain, snapshot_id, staging_dir, site_row):
    """Restore one tenant's file write-set with replacement semantics."""
    staged = os.path.join(staging_dir, 'files')
    os.makedirs(staged, mode=0o700, exist_ok=True)
    try:
        os.chmod(staging_dir, 0o700)
        os.chmod(staged, 0o700)
    except OSError:
        pass

    run_restic(['restore', snapshot_id, '--target', staged])
    paths = site_file_paths(domain)

    staged_wp_config = _staged_path(staged, paths['wp_config'])
    if os.path.isfile(staged_wp_config):
        _rewrite_wp_config(staged_wp_config, site_row, app)

    # Validate required single-file members before changing any live path.  A
    # missing directory is intentionally represented by an empty staged dir so
    # rsync --delete removes live extras.
    for key in ('wp_config', 'vhost'):
        staged_file = _staged_path(staged, paths[key])
        if not os.path.isfile(staged_file):
            raise _backup_error(
                "snapshot doesn't contain this site's files — wrong snapshot"
            )
    for key in ('uploads', 'conf_nginx'):
        staged_dir = _staged_path(staged, paths[key])
        if os.path.lexists(staged_dir) and not os.path.isdir(staged_dir):
            raise _backup_error(f'staged {key} is not a directory')
        os.makedirs(staged_dir, mode=0o700, exist_ok=True)

    # Apply directories first, then atomic single-file replacements.
    for key in ('uploads', 'conf_nginx'):
        live_dir = paths[key]
        os.makedirs(live_dir, mode=0o755, exist_ok=True)
        _run_rsync(
            _staged_path(staged, paths[key]),
            live_dir,
            excludes=('multitenancy-maintenance.conf',)
            if key == 'conf_nginx' else (),
        )

    for key in ('wp_config', 'vhost'):
        _copy_file_replace(
            _staged_path(staged, paths[key]),
            paths[key],
        )

    # force_ssl is optional and therefore follows exact replacement semantics.
    staged_force_ssl = _staged_path(staged, paths['force_ssl'])
    live_force_ssl = paths['force_ssl']
    if os.path.isfile(staged_force_ssl):
        _copy_file_replace(staged_force_ssl, live_force_ssl)
    elif os.path.lexists(live_force_ssl):
        try:
            os.remove(live_force_ssl)
        except OSError as exc:
            raise _backup_error(f'could not remove stale force-ssl config: {exc}')

    # Ownership is deliberately path-scoped.  In particular, never recurse
    # through /etc: only the restored vhost/force-ssl files are root-owned.
    _set_tree_owner_mode(app, paths['uploads'], 'www-data', 'www-data')
    _set_tree_owner_mode(app, paths['conf_nginx'], 'root', 'root')
    _set_owner_mode(app, paths['wp_config'], 'www-data', 'www-data', 0o640)
    _set_owner_mode(app, paths['vhost'], 'root', 'root', 0o644)
    if os.path.lexists(live_force_ssl):
        _set_owner_mode(app, live_force_ssl, 'root', 'root', 0o644)
    # Test after applying the nginx write-set, but leave reload orchestration
    # to the caller so fleet restore can attribute failures per tenant.
    _nginx_test_only(app)


def _stats_total_size(snapshot_id, app):
    try:
        result = run_restic(['stats', snapshot_id, '--json'])
        raw = getattr(result, 'stdout', '') or ''
        payload = None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            # Some restic versions can emit JSON lines even for stats.
            for line in str(raw).splitlines()[::-1]:
                try:
                    payload = json.loads(line)
                    break
                except (TypeError, ValueError):
                    continue
        if isinstance(payload, list):
            payload = payload[-1] if payload else None
        total = payload.get('total_size') if isinstance(payload, dict) else None
        if total is not None and int(total) > 0:
            return int(total)
    except Exception as exc:
        Log.debug(app, f'could not estimate restore size from restic stats: {exc}')
    return None


def _mysql_database_sql(db_name):
    if not db_name:
        raise _backup_error('site has no current database name')
    ident = str(db_name).replace('`', '``')
    return (
        f'DROP DATABASE IF EXISTS `{ident}`; '
        f'CREATE DATABASE `{ident}` CHARACTER SET utf8mb4 '
        'COLLATE utf8mb4_unicode_ci;'
    )


def _run_mysql_replace(app, db_name):
    sql = _mysql_database_sql(db_name)
    try:
        result = subprocess.run(
            mysql_argv() + ['-e', sql],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise _backup_error(f'could not run database command: {exc}')
    if result.returncode != 0:
        detail = (getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or
                  f'exit {result.returncode}').strip()
        raise _backup_error(f'could not recreate database {db_name}: {detail}')


def _run_mysql_import(app, db_name, dump_path):
    try:
        with open(dump_path, 'rb') as dump_fh:
            result = subprocess.run(
                mysql_argv(db_name),
                stdin=dump_fh,
                capture_output=True,
                check=False,
            )
    except OSError as exc:
        raise _backup_error(f'could not open SQL dump {dump_path}: {exc}')
    if result.returncode != 0:
        detail = (getattr(result, 'stderr', '') or getattr(result, 'stdout', '') or
                  f'exit {result.returncode}')
        if isinstance(detail, bytes):
            detail = detail.decode(errors='replace')
        raise _backup_error(f'database import failed: {str(detail).strip()}')


def restore_site_db(app, domain, snapshot_id, staging_dir, site_row):
    """Stage, verify, drop/recreate, and import a tenant DB snapshot."""
    os.makedirs(staging_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(staging_dir, 0o700)
    except OSError:
        pass
    total_size = _stats_total_size(snapshot_id, app)
    need = total_size * 2 if total_size else 1024 ** 3
    if total_size is None:
        Log.debug(app, 'restore size estimate unavailable; requiring 1 GiB free space')
    free_space_check(staging_dir, need)

    target = os.path.join(staging_dir, 'target.sql')
    command = [RESTIC_BIN, '--retry-lock', '5m', 'dump', snapshot_id,
               f'/{domain}.sql']
    try:
        with open(target, 'wb') as target_fh:
            result = subprocess.run(
                command,
                stdout=target_fh,
                stderr=subprocess.PIPE,
                env=restic_env(),
                check=False,
            )
    except OSError as exc:
        raise _backup_error(f'could not run restic dump: {exc}')
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    if result.returncode != 0 or not os.path.isfile(target) or os.path.getsize(target) == 0:
        detail = getattr(result, 'stderr', '') or f'exit {result.returncode}'
        if isinstance(detail, bytes):
            detail = detail.decode(errors='replace')
        raise _backup_error(f'restic dump failed or was empty: {str(detail).strip()}')

    db_name = _value(site_row, 'db_name')
    _run_mysql_replace(app, db_name)
    _run_mysql_import(app, db_name, target)


def _flush_redis(app, domain, tenant):
    prefix = _value(tenant, 'redis_prefix')
    redis_db = _value(tenant, 'redis_db')
    if not prefix:
        Log.warn(app, f'cannot flush Redis cache for {domain}: no redis prefix')
        return
    base = ['redis-cli']
    if redis_db is not None:
        base += ['-n', str(redis_db)]
    scan = subprocess.run(
        base + ['--scan', '--pattern', str(prefix) + '*'],
        capture_output=True,
        text=True,
        check=False,
    )
    if scan.returncode != 0:
        detail = (getattr(scan, 'stderr', '') or 'redis scan failed').strip()
        raise _backup_error(detail)
    keys = [key for key in (scan.stdout or '').splitlines() if key]
    for offset in range(0, len(keys), 500):
        result = subprocess.run(
            base + ['unlink'] + keys[offset:offset + 500],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (getattr(result, 'stderr', '') or 'redis unlink failed').strip()
            raise _backup_error(detail)


def flush_site_caches(app, domain, site_row_or_tenant):
    """Best-effort tenant-scoped Redis/FastCGI cache invalidation."""
    cache_type = _value(site_row_or_tenant, 'cache_type')
    if cache_type in ('redis', 'wpredis'):
        try:
            _flush_redis(app, domain, site_row_or_tenant)
        except Exception as exc:
            Log.warn(app, f'Redis cache flush for {domain} failed: {exc}')
    if cache_type == 'wpfc':
        try:
            purge_result = WOShellExec.cmd_exec(app, 'wo clean --fastcgi')
            if purge_result is False:
                Log.warn(app, f'FastCGI cache purge for {domain} failed')
        except Exception as exc:
            Log.warn(app, f'FastCGI cache purge for {domain} failed: {exc}')


def nginx_test_and_reload(app):
    """Validate nginx before reloading it, preserving the gate on failure."""
    _nginx_test_only(app)
    try:
        reload_result = subprocess.run(
            ['systemctl', 'reload', 'nginx'],
            capture_output=True,
            text=True,
            check=False,
            timeout=_NGINX_RELOAD_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise _backup_error(f'nginx reload timed out: {exc}')
    except OSError as exc:
        raise _backup_error(f'could not reload nginx: {exc}')
    if reload_result.returncode != 0:
        detail = (getattr(reload_result, 'stderr', '') or
                  getattr(reload_result, 'stdout', '') or
                  f'exit {reload_result.returncode}').strip()
        raise _backup_error(f'nginx reload failed: {detail}')


def _snapshot_tags(snapshot):
    tags = snapshot.get('tags', []) if isinstance(snapshot, dict) else []
    if isinstance(tags, str):
        return set(tag.strip() for tag in tags.split(',') if tag.strip())
    return set(tags or [])


def _snapshot_id(snapshot):
    if not isinstance(snapshot, dict):
        return None
    return snapshot.get('id') or snapshot.get('snapshot_id') or snapshot.get('short_id')


def _manifest_ids(value):
    """Extract component snapshot IDs from the published manifest shape."""
    found = []

    def collect_strings(node):
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            for child in node.values():
                collect_strings(child)
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                collect_strings(child)

    # Fleet's committed manifest stores IDs under ``expected``:
    # {domain: {files: id, db: id}, global: id}.  Keep the keyed form below
    # as a compatibility check for manifests written by older operators.
    if isinstance(value, dict) and 'expected' in value:
        collect_strings(value.get('expected'))
    else:
        def walk(node):
            if isinstance(node, dict):
                for child_key, child in node.items():
                    lower = str(child_key).lower()
                    if (lower in ('snapshot_id', 'snapshot_ids') or
                            lower.endswith('_snapshot_id')):
                        if isinstance(child, str):
                            found.append(child)
                        elif isinstance(child, (list, tuple, set)):
                            found.extend(str(item) for item in child)
                    else:
                        walk(child)
            elif isinstance(node, (list, tuple)):
                for child in node:
                    walk(child)
        walk(value)
    return set(item for item in found if item)


def _verify_fleet_manifest(app, operation_id, operation_snapshots):
    manifest_snapshots = [
        snapshot for snapshot in operation_snapshots
        if 'manifest' in _snapshot_tags(snapshot)
    ]
    if len(manifest_snapshots) != 1:
        raise _backup_error(
            f'operation {operation_id} has no complete operation-manifest.json'
        )
    manifest_id = _snapshot_id(manifest_snapshots[0])
    if not manifest_id:
        raise _backup_error(f'operation {operation_id} manifest has no snapshot ID')
    try:
        result = run_restic(['dump', manifest_id, '/operation-manifest.json'])
        payload = json.loads(getattr(result, 'stdout', '') or '')
    except Exception as exc:
        raise _backup_error(f'could not load operation manifest: {exc}')
    component_ids = _manifest_ids(payload)
    if not component_ids:
        raise _backup_error(f'operation {operation_id} manifest has no components')
    try:
        existing = list_snapshots()
    except Exception as exc:
        raise _backup_error(f'could not verify operation manifest: {exc}')
    existing_ids = set()
    for snapshot in existing:
        sid = _snapshot_id(snapshot)
        if sid:
            existing_ids.add(sid)
            short = snapshot.get('short_id') if isinstance(snapshot, dict) else None
            if short:
                existing_ids.add(short)
    missing = sorted(component_ids - existing_ids)
    if missing:
        raise _backup_error(
            f'operation {operation_id} is partially pruned; missing snapshots: '
            + ', '.join(missing)
        )
    return payload


def _resolve_operation_snapshots(app, domain, operation_id, families):
    try:
        operation_snapshots = list_snapshots(tags=[f'operation:{operation_id}'])
    except Exception as exc:
        raise _backup_error(f'could not list operation snapshots: {exc}')
    if not operation_snapshots:
        raise _backup_error(f'no snapshots found for operation {operation_id}')
    if any('fleet' in _snapshot_tags(snapshot) for snapshot in operation_snapshots):
        _verify_fleet_manifest(app, operation_id, operation_snapshots)

    resolved = {}
    site_tag = f'site:{domain}'
    for family in families:
        family_members = [
            snapshot for snapshot in operation_snapshots
            if family in _snapshot_tags(snapshot)
        ]
        matches = [snapshot for snapshot in family_members
                   if site_tag in _snapshot_tags(snapshot)]
        if len(matches) != 1:
            raise _backup_error(
                f'operation {operation_id} must contain exactly one {family} '
                f'snapshot for site {domain}'
            )
        snapshot = matches[0]
        if not _snapshot_id(snapshot):
            raise _backup_error(f'operation {operation_id} has an invalid {family} snapshot')
        resolved[family] = snapshot
    return resolved


def _resolve_restore_snapshots(app, domain, families, at=None, snapshot_id=None,
                               operation_id=None):
    if operation_id:
        return _resolve_operation_snapshots(app, domain, operation_id, families)
    resolved = {}
    for family in families:
        # Daily files snapshots are fleet-wide and intentionally carry no site
        # tag.  DB snapshots are per-site and must carry the site's tag.
        # Core resolution excludes pre-restore captures for implicit latest/--at.
        family_domain = domain if family == 'db' else None
        resolved[family] = resolve_snapshot(
            family,
            domain=family_domain,
            at=at,
            snapshot_id=snapshot_id,
        )
        if (family == 'files' and snapshot_id and
                'pre-restore' in _snapshot_tags(resolved[family]) and
                f'site:{domain}' not in _snapshot_tags(resolved[family])):
            raise _backup_error(
                f'snapshot {snapshot_id} is a safety capture for another site'
            )
    return resolved


def _snapshot_time(snapshot):
    value = snapshot.get('time') if isinstance(snapshot, dict) else None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not value:
        return None
    text = str(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _humanize_seconds(seconds):
    seconds = int(abs(seconds))
    if seconds < 60:
        return f'{seconds}s'
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m {seconds}s'
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f'{hours}h {minutes}m'
    days, hours = divmod(hours, 24)
    return f'{days}d {hours}h'


def _snapshot_summary(resolved, families):
    lines = []
    times = []
    for family in families:
        snapshot = resolved[family]
        timestamp = snapshot.get('time', 'unknown')
        lines.append(f'  {family}: {_snapshot_id(snapshot)} at {timestamp}')
        parsed = _snapshot_time(snapshot)
        if parsed is not None:
            times.append(parsed)
    if len(times) == 2:
        skew = abs((times[0] - times[1]).total_seconds())
        lines.append(f'  skew: {_humanize_seconds(skew)}')
    return lines

def _collect_absolute_paths(value):
    found = set()
    if isinstance(value, str):
        path = value.strip()
        if path.startswith('/'):
            found.add(os.path.normpath(path))
    elif isinstance(value, dict):
        for key, child in value.items():
            found.update(_collect_absolute_paths(key))
            found.update(_collect_absolute_paths(child))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found.update(_collect_absolute_paths(child))
    return found


def _safety_snapshot_paths(capture):
    for key in ('paths', 'files', 'members'):
        if key in capture:
            return _collect_absolute_paths(capture.get(key))
    return None


def _verify_files_safety_capture(app, domain, operation_id, capture):
    paths = site_file_paths(domain)
    live_paths = [path for path in paths.values() if os.path.exists(path)]
    # Validate every live write-set member; wp-config/vhost are mandatory
    # single-file members and must never be omitted from the safety capture.
    required = list(live_paths)
    if capture is None:
        if live_paths:
            raise _backup_error(
                f'file safety capture for {domain} was empty despite live paths: '
                + ', '.join(live_paths)
            )
        print(
            f'No live file write-set exists for {domain}; no file safety snapshot '
            "was created. Rollback will restore files to 'absent'."
        )
        return
    if not isinstance(capture, dict) or not _snapshot_id(capture):
        raise _backup_error(
            f'file safety capture for {domain} has no usable snapshot ID'
        )
    if not required:
        return

    captured = _safety_snapshot_paths(capture)
    if captured is None:
        try:
            result = run_restic(
                ['ls', _snapshot_id(capture)],
                timeout=_SUBPROCESS_TIMEOUT,
            )
            raw = getattr(result, 'stdout', '') or ''
            if isinstance(raw, bytes):
                raw = raw.decode(errors='replace')
            captured = {
                os.path.normpath(line.strip())
                for line in str(raw).splitlines()
                if line.strip().startswith('/')
            }
        except Exception as exc:
            raise _backup_error(
                f'could not verify file safety capture for operation '
                f'{operation_id}: {exc}'
            )
    missing = [
        path for path in required
        if not any(
            path == member or path.startswith(member.rstrip('/') + os.sep)
            for member in captured
        )
    ]
    if missing:
        raise _backup_error(
            f'file safety capture for {domain} is missing required live paths: '
            + ', '.join(missing)
        )


def _log_failure(controller, message):
    Log.error(controller, message, exit=False)
    try:
        controller.app.close(1)
    except AttributeError:
        pass


def _tenant_for_restore(domain, mt_site, site_row):
    return TenantInfo(
        domain,
        _value(mt_site, 'site_path') or _value(site_row, 'site_path') or f'/var/www/{domain}',
        _value(mt_site, 'cache_type') or _value(site_row, 'cache_type'),
        _value(mt_site, 'php_version') or _value(site_row, 'php_version'),
        bool(_value(mt_site, 'is_enabled', True)),
        bool(_value(mt_site, 'is_ssl', False)),
        _value(mt_site, 'redis_prefix'),
        _value(mt_site, 'redis_db'),
        _value(site_row, 'db_name'),
        _value(site_row, 'db_user'),
        _value(site_row, 'db_password'),
        _value(site_row, 'db_host'),
    )


def _rollback_local_db(app, site_row, staging_dir):
    pre_restore = os.path.join(staging_dir, 'pre-restore.sql')
    if not os.path.isfile(pre_restore) or os.path.getsize(pre_restore) == 0:
        raise _backup_error(f'local rollback dump is missing: {pre_restore}')
    db_name = _value(site_row, 'db_name')
    _run_mysql_replace(app, db_name)
    _run_mysql_import(app, db_name, pre_restore)


def _manual_db_recovery_commands(site_row, pre_restore):
    db_name = _value(site_row, 'db_name') or '<db_name>'
    recreate = _shell_command(
        mysql_argv() + ['-e', _mysql_database_sql(db_name)]
    )
    import_dump = (
        _shell_command(mysql_argv(db_name))
        + ' < '
        + shlex.quote(str(pre_restore))
    )
    return recreate, import_dump


def _shell_command(argv):
    return ' '.join(shlex.quote(str(argument)) for argument in argv)


def cmd_restore_site(controller):
    """Implement ``wo multitenancy backup restore <domain>``."""
    pargs = controller.app.pargs
    domain = getattr(pargs, 'site_name', None)
    db_flag = bool(getattr(pargs, 'db', False))
    files_flag = bool(getattr(pargs, 'files', False))
    all_flag = bool(getattr(pargs, 'all', False))
    at = getattr(pargs, 'at', None)
    snapshot_id = getattr(pargs, 'snapshot', None)
    operation_id = getattr(pargs, 'operation', None)
    force = bool(getattr(pargs, 'force', False))

    if not domain:
        _log_failure(
            controller,
            'Usage: wo multitenancy backup restore <domain> --db|--files|--all',
        )
        return None
    scopes = [name for name, selected in (
        ('db', db_flag), ('files', files_flag), ('all', all_flag)
    ) if selected]
    if len(scopes) != 1:
        _log_failure(
            controller,
            'Usage: choose exactly one of --db, --files, or --all',
        )
        return None
    scope = scopes[0]
    if snapshot_id and scope == 'all':
        _log_failure(
            controller,
            '--snapshot is valid only with --db or --files',
        )
        return None
    if snapshot_id and at:
        _log_failure(
            controller,
            '--at and --snapshot are mutually exclusive',
        )
        return None
    if operation_id and (snapshot_id or at):
        _log_failure(
            controller,
            '--operation cannot be combined with --at or --snapshot',
        )
        return None

    # Query both stores directly: disabled rows remain valid restore targets,
    # while a missing row must be recreated explicitly through multitenancy
    # create rather than fabricated by restore.
    try:
        from wo.core.database import db_session
        from wo.cli.plugins.multitenancy_db import MultitenancySite
        mt_site = (db_session.query(MultitenancySite)
                   .filter_by(domain=domain).first())
    except Exception as exc:
        _log_failure(controller, f'Unable to query multitenancy tracking: {exc}')
        return None
    if mt_site is None:
        _log_failure(
            controller,
            f'Site {domain} not found in multitenancy tracking; recreate with '
            'wo multitenancy create first',
        )
        return None
    try:
        site_row = getSiteInfo(controller, domain)
    except Exception as exc:
        _log_failure(controller, f'Unable to query site database: {exc}')
        return None
    if site_row is None:
        _log_failure(
            controller,
            f'Site {domain} not found in WordOps database; recreate with '
            'wo multitenancy create first',
        )
        return None

    families = ['db', 'files'] if scope == 'all' else [scope]
    try:
        resolved = _resolve_restore_snapshots(
            controller, domain, families, at=at, snapshot_id=snapshot_id,
            operation_id=operation_id,
        )
    except Exception as exc:
        _log_failure(controller, str(exc))
        return None

    summary = _snapshot_summary(resolved, families)
    if not force:
        print(f'Restore {domain} ({scope}) from:')
        for line in summary:
            print(line)
        answer = input('Proceed with in-place restore? [y/N] ').strip().lower()
        if answer not in ('y', 'yes'):
            Log.info(controller, 'Restore cancelled')
            return None

    try:
        with operation_lock('backup restore', blocking=False):
            return _cmd_restore_site_locked(
                controller, domain, scope, families, resolved, mt_site, site_row,
                summary,
            )
    except OperationLockBusy as exc:
        holder = getattr(exc, 'holder', None) or str(exc)
        _log_failure(controller, f'Backup operation is busy (held by {holder})')
        return None
    except Exception as exc:
        _log_failure(controller, f'Restore failed before applying changes: {exc}')
        return None


def _cmd_restore_site_locked(controller, domain, scope, families, resolved,
                             mt_site, site_row, summary):
    # Gate helpers stay lazy to avoid importing the parent controller during
    # plugin discovery (and to avoid a parent/child import cycle).
    from wo.cli.plugins.multitenancy import (
        _maintenance_disable,
        _maintenance_enable,
        _maintenance_gate_exists,
    )

    config = MTFunctions.load_config(controller)
    gate_created = False
    gate_released = False
    if _maintenance_gate_exists(domain):
        Log.warn(controller,
                 f'{domain} already has an operator maintenance gate; restore will not ungate it')
    else:
        if not _maintenance_enable(controller, domain, 'Restore in progress', config):
            raise _backup_error(f'could not enable maintenance gate for {domain}')
        gate_created = True
        gate_result = nginx_test_and_reload(controller)
        if gate_result is False:
            raise _backup_error(f'could not activate maintenance gate for {domain}')
    os.makedirs(RESTORE_ROOT, mode=0o700, exist_ok=True)
    try:
        os.chmod(RESTORE_ROOT, 0o700)
    except OSError:
        pass

    operation_id = new_operation_id()
    stamp = operation_id
    staging_dir = os.path.join(RESTORE_ROOT, f'{domain}-{stamp}')
    os.makedirs(staging_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(staging_dir, 0o700)
    except OSError:
        pass
    tenant = _tenant_for_restore(domain, mt_site, site_row)

    phase = 'safety'
    try:
        if scope in ('files', 'all'):
            phase = 'files'
            safety_capture = safety_snapshot_files(controller, domain, operation_id)
            _verify_files_safety_capture(
                controller, domain, operation_id, safety_capture
            )
        if scope in ('db', 'all'):
            phase = 'safety-db'
            safety_snapshot_db(controller, tenant, operation_id, staging_dir)

        print(f'Restore operation {operation_id} prepared for {domain}.')
        rollback_scope = 'all' if scope == 'all' else scope
        print('Rollback hint: wo multitenancy backup restore '
              f'{domain} --{rollback_scope} --operation={operation_id}')

        if scope in ('files', 'all'):
            phase = 'files'
            restore_site_files(controller, domain, _snapshot_id(resolved['files']),
                               staging_dir, site_row)
            nginx_test_and_reload(controller)
        if scope in ('db', 'all'):
            phase = 'db'
            restore_site_db(controller, domain, _snapshot_id(resolved['db']),
                            staging_dir, site_row)
        phase = 'post'
        # §7 step 6: flush caches (redis prefix / FastCGI purge) before the
        # gate comes down. Best-effort — never fail a completed restore.
        try:
            flush_site_caches(controller, domain, tenant)
        except Exception as flush_exc:
            Log.warn(controller,
                     f'Cache flush warning for {domain}: {flush_exc}')

        if gate_created:
            try:
                if not _maintenance_disable(controller, domain):
                    raise _backup_error(
                        f'could not disable maintenance gate for {domain}'
                    )
                gate_result = nginx_test_and_reload(controller)
                if gate_result is False:
                    raise _backup_error(
                        f'could not reload nginx after ungating {domain}'
                    )
                gate_released = True
            except Exception as ungate_exc:
                try:
                    if not _maintenance_enable(
                            controller, domain, 'Restore in progress', config):
                        raise _backup_error(
                            f'could not restore maintenance gate for {domain}'
                        )
                except Exception as rearm_exc:
                    raise _backup_error(
                        f'could not ungate {domain}; could not re-arm the '
                        f'maintenance gate: {rearm_exc}'
                    ) from ungate_exc
                raise
        shutil.rmtree(staging_dir)
        print(f'Restore completed for {domain}; operation {operation_id}.')
        return None
    except Exception as exc:
        if gate_created:
            try:
                if not _maintenance_enable(
                        controller, domain, 'Restore in progress', config):
                    raise _backup_error(
                        f'could not re-arm maintenance gate for {domain}'
                    )
                if gate_released:
                    gate_result = nginx_test_and_reload(controller)
                    if gate_result is False:
                        raise _backup_error(
                            f'could not reload nginx after re-arming {domain}'
                        )
                    gate_released = False
            except Exception as gate_exc:
                Log.warn(controller,
                         f'maintenance gate recovery for {domain} failed: {gate_exc}')
        if phase == 'db':
            try:
                _rollback_local_db(controller, site_row, staging_dir)
                Log.error(
                    controller,
                    f'Database restore failed: {exc}. Automatic rollback from '
                    f'{os.path.join(staging_dir, "pre-restore.sql")} succeeded; '
                    'maintenance gate remains enabled and staging is retained.',
                    exit=False,
                )
            except Exception as rollback_exc:
                pre_restore = os.path.join(staging_dir, 'pre-restore.sql')
                recreate_cmd, import_cmd = _manual_db_recovery_commands(
                    site_row, pre_restore
                )
                Log.error(
                    controller,
                    f'{rollback_exc}. Gate remains enabled. Manual recovery:\n'
                    f'  {recreate_cmd}\n'
                    f'  {import_cmd}',
                    exit=False,
                )
        elif phase == 'files':
            Log.error(
                controller,
                f'File restore failed: {exc}. Gate remains enabled. Roll back with '
                f'wo multitenancy backup restore {domain} --files '
                f'--operation={operation_id}. Manual ungate: '
                f'wo multitenancy maintenance {domain} --disable',
                exit=False,
            )
        elif phase == 'post':
            Log.error(
                controller,
                f'Restore finalization failed: {exc}. Gate remains enabled; '
                f'staging retained at {staging_dir}. Manual ungate: '
                f'wo multitenancy maintenance {domain} --disable',
                exit=False,
            )
        else:
            Log.error(
                controller,
                f'Restore safety capture failed: {exc}. Gate remains enabled; '
                f'staging retained at {staging_dir}. Manual ungate: '
                f'wo multitenancy maintenance {domain} --disable',
                exit=False,
            )
        try:
            controller.app.close(1)
        except AttributeError:
            pass
        return None
