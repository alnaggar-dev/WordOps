"""Fleet-wide restore orchestration for multi-tenancy backups.

The fleet restore deliberately owns the complete transaction: it resolves and
verifies the snapshot database, captures a reversible safety set, quarantines
unknown tenants, cuts over global state, and only then applies each snapshot
site.  The single-site restore helpers are intentionally not used for the
per-site file phase because they would restore the fleet-wide files snapshot
once per tenant.
"""

import datetime
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile

from wo.core.logging import Log
from wo.core.database import db_session
from wo.cli.plugins.sitedb import getSiteInfo
from wo.cli.plugins.multitenancy_db import MultitenancySite
from wo.cli.plugins.multitenancy_functions import MTFunctions

from wo.cli.plugins.multitenancy_backup_functions import (
    BackupError,
    GATE_EXCLUDES,
    OperationLockBusy,
    QUARANTINE_ROOT,
    RESTORE_ROOT,
    TenantInfo,
    backup_is_configured,
    mariadb_dump_argv,
    mysql_argv,
    new_operation_id,
    operation_lock,
    repair_backup_cron,
    resolve_snapshot,
    restic_backup_paths,
    restic_backup_stdin_command,
    run_restic,
    safety_snapshot_db,
    safety_snapshot_files,
    site_file_paths,
    sqlite_integrity_ok,
    stage_sqlite_copy,
    write_tombstone,
)
from wo.cli.plugins.multitenancy_backup_restore import (
    flush_site_caches,
    nginx_test_and_reload,
    restore_site_db,
)


class _SnapshotSiteRow(dict):
    """Mapping with attribute access for restore primitives and SQL rows."""

    def __getattr__(self, name):
        return self.get(name)


class _FleetAbort(BackupError):
    """Abort a fleet restore before or during the global cutover."""
def _maintenance_helpers():
    """Load gate functions lazily to avoid the multitenancy import cycle."""
    from wo.cli.plugins.multitenancy import (
        _maintenance_disable,
        _maintenance_enable,
    )
    return _maintenance_enable, _maintenance_disable




def _value(row, name, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _snapshot_id(summary):
    if isinstance(summary, str):
        return summary
    if isinstance(summary, dict):
        for key in ('snapshot_id', 'id', 'short_id'):
            value = summary.get(key)
            if value:
                return value
    return None


def _snapshot_time(summary):
    if isinstance(summary, dict):
        value = summary.get('time') or summary.get('timestamp') or summary.get('created')
    else:
        value = None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value) if value else 'unknown'


def _parse_snapshot_time(value):
    if not value or value == 'unknown':
        return None
    try:
        text = str(value).replace('Z', '+00:00')
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _snapshot_skew(first, second):
    left = _parse_snapshot_time(first)
    right = _parse_snapshot_time(second)
    if left is None or right is None:
        return 'unknown'
    seconds = abs((left - right).total_seconds())
    return str(datetime.timedelta(seconds=int(seconds)))


def _iso_utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _ensure_mode(path, mode):
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise _FleetAbort("cannot set mode on %s: %s" % (path, exc))


def _mkdir_private(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    _ensure_mode(path, 0o700)


def _lexists(path):
    return os.path.lexists(path)


def _remove_path(path):
    if not _lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _write_json_private(path, payload):
    parent = os.path.dirname(path)
    _mkdir_private(parent)
    fd, tmp_path = tempfile.mkstemp(prefix='.wo-fleet-', dir=parent, text=True)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
        _ensure_mode(tmp_path, 0o600)
        os.replace(tmp_path, path)
        _ensure_mode(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _row_from_snapshot(mt_row, sites_row):
    row = _SnapshotSiteRow()
    if mt_row:
        row.update(dict(mt_row))
    if sites_row:
        row.update({
            'db_name': _value(sites_row, 'db_name'),
            'db_user': _value(sites_row, 'db_user'),
            'db_password': _value(sites_row, 'db_password'),
            'db_host': _value(sites_row, 'db_host') or 'localhost',
        })
    domain = row.get('domain')
    row['domain'] = domain
    row.setdefault('site_path', '/var/www/%s' % domain)
    row.setdefault('cache_type', None)
    row.setdefault('php_version', None)
    row.setdefault('is_enabled', True)
    row.setdefault('is_ssl', False)
    row.setdefault('redis_prefix', None)
    row.setdefault('redis_db', None)
    row.setdefault('db_host', 'localhost')
    return row


def _load_snapshot_manifest(path):
    """Read the snapshot dbase.db with plain SQL, never the live ORM."""
    uri = 'file:%s?mode=ro' % os.path.abspath(path)
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if 'multitenancy_sites' not in tables:
            raise _FleetAbort('snapshot dbase.db has no multitenancy_sites table')
        if 'sites' not in tables:
            raise _FleetAbort('snapshot dbase.db has no sites table')

        site_columns = {
            row[1] for row in connection.execute('PRAGMA table_info(sites)')
        }
        site_name_column = 'sitename' if 'sitename' in site_columns else 'domain'
        mt_rows = connection.execute(
            'SELECT * FROM multitenancy_sites ORDER BY domain'
        ).fetchall()
        manifest = {}
        for mt_sql_row in mt_rows:
            mt_row = dict(mt_sql_row)
            domain = mt_row.get('domain')
            if not domain:
                raise _FleetAbort('snapshot multitenancy_sites row has no domain')
            if domain in manifest:
                raise _FleetAbort('snapshot manifest contains duplicate domain %s' % domain)
            site_row = connection.execute(
                'SELECT * FROM sites WHERE %s = ? LIMIT 1' % site_name_column,
                (domain,),
            ).fetchone()
            manifest[domain] = _row_from_snapshot(
                mt_row,
                dict(site_row) if site_row is not None else None,
            )
        return manifest
    finally:
        connection.close()


def _load_current_manifest(controller):
    """Build current rows while retaining tenants with missing sites rows."""
    if MultitenancySite is None or db_session is None:
        raise _FleetAbort('current multitenancy database modules are unavailable')
    try:
        raw_sites = db_session.query(MultitenancySite).all()
    except Exception as exc:
        try:
            db_session.rollback()
        except Exception:
            pass
        raise _FleetAbort('could not read current multitenancy sites: %s' % exc)
    if raw_sites is None:
        raise _FleetAbort('could not read current multitenancy sites')

    manifest = {}
    for raw in raw_sites:
        domain = _value(raw, 'domain')
        if not domain:
            raise _FleetAbort('current multitenancy row has no domain')
        if domain in manifest:
            raise _FleetAbort('current manifest contains duplicate domain %s' % domain)
        row = _SnapshotSiteRow()
        for key in (
            'domain', 'site_type', 'cache_type', 'site_path', 'php_version',
            'is_enabled', 'is_ssl', 'redis_prefix', 'redis_db',
        ):
            value = _value(raw, key)
            if value is not None:
                row[key] = value
        row['domain'] = domain
        row.setdefault('site_path', '/var/www/%s' % domain)
        try:
            site_info = getSiteInfo(controller, domain)
        except Exception as exc:
            raise _FleetAbort(
                'could not read credentials for %s: %s' % (domain, exc)
            )
        if site_info is not None:
            row.update({
                'db_name': _value(site_info, 'db_name'),
                'db_user': _value(site_info, 'db_user'),
                'db_password': _value(site_info, 'db_password'),
                'db_host': _value(site_info, 'db_host') or 'localhost',
            })
        else:
            Log.warn(controller, 'Could not read credentials for %s' % domain)
            row.update({
                'db_name': None,
                'db_user': None,
                'db_password': None,
                'db_host': 'localhost',
            })
        manifest[domain] = row
    return manifest


def _find_staged_file(root, wanted):
    direct = os.path.join(root, wanted.lstrip('/'))
    if _lexists(direct):
        return direct
    basename = os.path.basename(wanted)
    for current, dirs, files in os.walk(root):
        if basename in files:
            candidate = os.path.join(current, basename)
            if candidate.endswith(wanted.lstrip('/')) or wanted.endswith(basename):
                return candidate
    return None


def _stage_snapshot_dbase(files_snapshot_id):
    _mkdir_private(RESTORE_ROOT)
    target = tempfile.mkdtemp(prefix='fleet-dbase-', dir=RESTORE_ROOT)
    _ensure_mode(target, 0o700)
    run_restic([
        'restore',
        files_snapshot_id,
        '--target',
        target,
        '--include',
        '/var/lib/wo-backup/staging/dbase.db',
    ])
    source = _find_staged_file(target, '/var/lib/wo-backup/staging/dbase.db')
    if source is None or not os.path.isfile(source):
        raise _FleetAbort('files snapshot did not contain staging dbase.db')
    verified = os.path.join(target, 'verified-dbase.db')
    shutil.copy2(source, verified)
    _ensure_mode(verified, 0o600)
    if not sqlite_integrity_ok(verified):
        raise _FleetAbort('snapshot dbase.db failed PRAGMA integrity_check')
    return verified, target


def _tenant_for_safety(row):
    if (not _value(row, 'db_name') or not _value(row, 'db_user') or
            _value(row, 'db_password') is None):
        return None
    return TenantInfo(
        domain=_value(row, 'domain'),
        site_path=_value(row, 'site_path') or '/var/www/%s' % _value(row, 'domain'),
        cache_type=_value(row, 'cache_type'),
        php_version=_value(row, 'php_version'),
        is_enabled=bool(_value(row, 'is_enabled', True)),
        is_ssl=bool(_value(row, 'is_ssl', False)),
        redis_prefix=_value(row, 'redis_prefix'),
        redis_db=_value(row, 'redis_db'),
        db_name=_value(row, 'db_name'),
        db_user=_value(row, 'db_user'),
        db_password=_value(row, 'db_password'),
        db_host=_value(row, 'db_host') or 'localhost',
    )


def _summary_snapshot_id_or_abort(summary, label):
    value = _snapshot_id(summary)
    if not value:
        raise _FleetAbort('%s did not return a snapshot id' % label)
    return value


def _capture_safety(controller, current_manifest, operation_id, operation_root, config):
    expected = {}
    for domain in sorted(current_manifest):
        row = current_manifest[domain]
        local_dir = os.path.join(operation_root, domain)
        _mkdir_private(local_dir)
        missing = []
        try:
            files_summary = safety_snapshot_files(
                controller,
                domain,
                operation_id,
                extra_tags=['fleet'],
            )
        except Exception as exc:
            raise _FleetAbort(
                'file safety capture for %s failed: %s' % (domain, exc)
            )
        files_id = None
        if files_summary is None:
            missing.append('files snapshot (no existing file paths)')
        else:
            files_id = _snapshot_id(files_summary)
            if not files_id:
                raise _FleetAbort(
                    'file safety capture for %s returned no snapshot id' % domain
                )

        tenant = _tenant_for_safety(row)
        if tenant is None:
            missing.append('DB snapshot (missing current sites credentials)')
        if missing:
            raise _FleetAbort(
                'safety capture for %s is incomplete: missing %s' %
                (domain, ', '.join(missing))
            )

        try:
            db_summary = safety_snapshot_db(
                controller,
                tenant,
                operation_id,
                local_dir=local_dir,
                extra_tags=['fleet'],
            )
        except Exception as exc:
            raise _FleetAbort(
                'DB safety capture for %s failed: %s' % (domain, exc)
            )
        db_id = _summary_snapshot_id_or_abort(
            db_summary, 'DB safety capture for %s' % domain
        )
        expected[domain] = {'files': files_id, 'db': db_id}

    shared_root = config.get('shared_root', '/var/www/shared')
    current_sqlite = stage_sqlite_copy()
    if not current_sqlite or not os.path.isfile(current_sqlite):
        raise _FleetAbort('could not stage the current dbase.db for safety capture')
    global_paths = [
        os.path.join(shared_root, 'config'),
        os.path.join(shared_root, '.git'),
        os.path.join(shared_root, 'wp-content'),
        '/etc/wo/plugins.d/multitenancy.conf',
        current_sqlite,
        '/etc/letsencrypt',
    ]
    global_paths = [path for path in global_paths if _lexists(path)]
    if not global_paths:
        raise _FleetAbort('no global paths exist for fleet safety capture')
    global_summary = restic_backup_paths(
        global_paths,
        [
            'pre-restore',
            'files',
            'fleet',
            'global',
            'operation:%s' % operation_id,
        ],
        excludes=GATE_EXCLUDES,
    )
    global_id = _summary_snapshot_id_or_abort(global_summary, 'global safety capture')

    expected['global'] = global_id
    manifest_path = os.path.join(operation_root, 'operation-manifest.json')
    _write_json_private(manifest_path, {
        'operation': 'backup restore --all-sites',
        'created': _iso_utc(),
        'expected': expected,
    })
    manifest_summary = restic_backup_stdin_command(
        'operation-manifest.json',
        ['cat', manifest_path],
        [
            'pre-restore',
            'fleet',
            'manifest',
            'operation:%s' % operation_id,
        ],
    )
    _summary_snapshot_id_or_abort(manifest_summary, 'operation manifest publish')
    return expected


def _display_diff(controller, files_snapshot, db_snapshots, snapshot_manifest,
                  current_manifest):
    snapshot_only = sorted(set(snapshot_manifest) - set(current_manifest))
    current_only = sorted(set(current_manifest) - set(snapshot_manifest))
    Log.info(controller, 'Fleet restore files snapshot %s @ %s' % (
        _snapshot_id(files_snapshot), _snapshot_time(files_snapshot)))
    for domain in sorted(db_snapshots):
        db_snapshot = db_snapshots[domain]
        Log.info(controller, '  %s DB snapshot %s @ %s (skew %s)' % (
            domain,
            _snapshot_id(db_snapshot),
            _snapshot_time(db_snapshot),
            _snapshot_skew(_snapshot_time(files_snapshot), _snapshot_time(db_snapshot)),
        ))
    Log.info(controller, 'Snapshot-not-current (will be restored): %s' % (
        ', '.join(snapshot_only) if snapshot_only else '(none)'
    ))
    Log.info(controller, 'Current-not-snapshot (WILL BE QUARANTINED): %s' % (
        ', '.join(current_only) if current_only else '(none)'
    ))
    Log.info(controller, 'Current-only tenants are quarantined before the dbase.db cutover.')
    return snapshot_only, current_only


def _confirm(controller, pargs, snapshot_only, current_only):
    if getattr(pargs, 'force', False):
        return
    prompt = (
        '\nThis replaces the fleet from the selected files snapshot; '
        'current-only tenants will be QUARANTINED.\n'
        'Continue fleet restore? [y/N]: '
    )
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, IOError) as exc:
        raise _FleetAbort('confirmation input failed: %s' % exc)
    if answer not in ('y', 'yes'):
        raise _FleetAbort('fleet restore cancelled')


def _run_mysql(controller, sql):
    try:
        result = subprocess.run(
            mysql_argv() + ['-e', sql],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except OSError as exc:
        raise _FleetAbort('MariaDB command failed to start: %s' % exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise _FleetAbort('MariaDB command failed: %s' % detail[-1000:])
    return result


def _sql_string(value):
    text = str(value)
    return "'%s'" % text.replace('\\', '\\\\').replace("'", "''")


def _sql_identifier(value):
    text = str(value)
    if '\x00' in text:
        raise _FleetAbort('NUL in SQL identifier')
    return '`%s`' % text.replace('`', '``')


def _host_part(value):
    host = str(value or 'localhost').strip()
    if '@' in host:
        host = host.rsplit('@', 1)[1]
    return host.strip("'\"") or 'localhost'


def _db_principal(row):
    db_name = _value(row, 'db_name')
    db_user = _value(row, 'db_user')
    db_password = _value(row, 'db_password')
    if not db_name or not db_user or db_password is None:
        return None
    host = _host_part(_value(row, 'db_host'))
    principal = '%s@%s' % (_sql_string(db_user), _sql_string(host))
    return db_name, principal, db_password


def _ensure_snapshot_databases(controller, snapshot_manifest, failed):
    valid = []
    for domain in sorted(snapshot_manifest):
        row = snapshot_manifest[domain]
        principal = _db_principal(row)
        if principal is None:
            failed.setdefault(domain, 'snapshot row has incomplete database credentials')
            continue
        db_name, user_at_host, password = principal
        sql = (
            'CREATE DATABASE IF NOT EXISTS %s; '
            'CREATE USER IF NOT EXISTS %s IDENTIFIED BY %s; '
            'ALTER USER %s IDENTIFIED BY %s; '
            'GRANT ALL PRIVILEGES ON %s.* TO %s;'
        ) % (
            _sql_identifier(db_name),
            user_at_host,
            _sql_string(password),
            user_at_host,
            _sql_string(password),
            _sql_identifier(db_name),
            user_at_host,
        )
        try:
            _run_mysql(controller, sql)
            valid.append(domain)
        except Exception as exc:
            failed.setdefault(domain, 'ensure-db failed: %s' % exc)
            Log.error(controller, 'ensure-db failed for %s: %s' % (domain, exc), exit=False)

    if valid:
        try:
            _run_mysql(controller, 'FLUSH PRIVILEGES;')
        except Exception as exc:
            for domain in valid:
                failed.setdefault(domain, 'FLUSH PRIVILEGES failed: %s' % exc)
            Log.error(controller, 'ensure-db FLUSH PRIVILEGES failed: %s' % exc, exit=False)


def _dump_database_to(path, row):
    db_name = _value(row, 'db_name')
    if not db_name:
        return False
    _ensure_mode(os.path.dirname(path), 0o700)
    try:
        with open(path, 'wb') as dump_handle:
            result = subprocess.run(
                mariadb_dump_argv(db_name),
                stdout=dump_handle,
                stderr=subprocess.PIPE,
                check=False,
                timeout=300,
            )
    except OSError as exc:
        raise _FleetAbort('could not start MariaDB dump for %s: %s' % (
            _value(row, 'domain'), exc
        ))
    if result.returncode != 0:
        stderr = result.stderr or b''
        if isinstance(stderr, bytes):
            detail = stderr.decode(errors='replace').strip()
        else:
            detail = str(stderr).strip()
        raise _FleetAbort('MariaDB dump failed for %s: %s' % (
            _value(row, 'domain'), detail[-1000:]
        ))
    _ensure_mode(path, 0o600)
    return True


def _remove_cron_line(domain, quarantine_dir):
    cron_path = '/etc/cron.d/wo-multitenancy'
    if not os.path.isfile(cron_path):
        return None
    with open(cron_path, 'r') as handle:
        lines = handle.readlines()
    matched = []
    kept = []
    marker = '/var/www/%s/' % domain
    lock_marker = 'wo-cron-%s.lock' % domain
    for line in lines:
        if marker in line or lock_marker in line:
            matched.append(line)
        else:
            kept.append(line)
    if not matched:
        return None
    entry_path = os.path.join(quarantine_dir, 'cron-entry')
    with open(entry_path, 'w') as handle:
        handle.writelines(matched)
    _ensure_mode(entry_path, 0o600)
    fd, tmp_path = tempfile.mkstemp(prefix='.wo-cron-', dir=os.path.dirname(cron_path), text=True)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.writelines(kept)
        _ensure_mode(tmp_path, stat.S_IMODE(os.stat(cron_path).st_mode))
        os.replace(tmp_path, cron_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return entry_path


def _quarantine_current_only(controller, current_manifest, current_only,
                             operation_id, stamp):
    if not current_only:
        return []

    # Make every current-only site unreachable as one batch before any move or
    # DROP USER/DATABASE action. A failed reload leaves all sites gated.
    for domain in current_only:
        enabled = '/etc/nginx/sites-enabled/%s' % domain
        if _lexists(enabled):
            os.unlink(enabled)
    try:
        reload_result = nginx_test_and_reload(controller)
    except Exception as exc:
        raise _FleetAbort('nginx reload after current-only unlink failed: %s' % exc)
    if reload_result is False:
        raise _FleetAbort('nginx reload after current-only unlink failed')

    quarantined = []
    for domain in current_only:
        row = current_manifest[domain]
        quarantine_dir = os.path.join(QUARANTINE_ROOT, '%s-%s' % (domain, stamp))
        _mkdir_private(quarantine_dir)
        paths = {}
        site_root = _value(row, 'site_path') or '/var/www/%s' % domain

        dump_path = os.path.join(quarantine_dir, 'database.sql')
        if _value(row, 'db_name'):
            _dump_database_to(dump_path, row)
            paths['database'] = {
                'original': _value(row, 'db_name'),
                'quarantined': dump_path,
            }

        moved = [
            ('site_root', site_root, os.path.join(quarantine_dir, 'site-root')),
            ('vhost', '/etc/nginx/sites-available/%s' % domain,
             os.path.join(quarantine_dir, 'vhost')),
            ('force_ssl', '/etc/nginx/conf.d/force-ssl-%s.conf' % domain,
             os.path.join(quarantine_dir, 'force-ssl.conf')),
        ]
        for label, original, destination in moved:
            if _lexists(original):
                shutil.move(original, destination)
                paths[label] = {'original': original, 'quarantined': destination}
        cron_entry = _remove_cron_line(domain, quarantine_dir)
        if cron_entry:
            paths['cron_entry'] = {
                'original': '/etc/cron.d/wo-multitenancy',
                'quarantined': cron_entry,
            }

        principal = _db_principal(row)
        if principal is not None:
            db_name, user_at_host, _password = principal
            _run_mysql(
                controller,
                'DROP DATABASE IF EXISTS %s; DROP USER IF EXISTS %s;'
                % (_sql_identifier(db_name), user_at_host),
            )
        else:
            Log.warn(controller, 'No database credentials for current-only %s; skipped DB drop' % domain)

        tombstone_ok = write_tombstone(domain)
        if tombstone_ok is False:
            Log.warn(controller, 'Could not write tombstone for quarantined %s' % domain)
        manifest_path = os.path.join(quarantine_dir, 'quarantine-manifest.json')
        _write_json_private(manifest_path, {
            'operation_id': operation_id,
            'domain': domain,
            'paths': paths,
            'creds': {
                'db_name': _value(row, 'db_name'),
                'db_user': _value(row, 'db_user'),
                'db_password': _value(row, 'db_password'),
                'db_host': _value(row, 'db_host'),
            },
        })
        quarantined.append({
            'domain': domain,
            'directory': quarantine_dir,
            'operation_id': operation_id,
        })
    return quarantined


def _staged_path(root, live_path):
    candidate = os.path.join(root, str(live_path).lstrip('/'))
    if _lexists(candidate):
        return candidate
    return None


def _remove_directory_contents_except(path, names):
    if not os.path.isdir(path) or os.path.islink(path):
        return
    for name in os.listdir(path):
        if name in names:
            continue
        _remove_path(os.path.join(path, name))


def _copy_file_atomic(source, destination):
    parent = os.path.dirname(destination)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.wo-restore-', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as out_handle:
            with open(source, 'rb') as in_handle:
                shutil.copyfileobj(in_handle, out_handle)
        try:
            _ensure_mode(tmp_path, stat.S_IMODE(os.stat(source).st_mode))
        except OSError:
            pass
        os.replace(tmp_path, destination)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _rewrite_wp_config(path, row):
    with open(path, 'r') as handle:
        contents = handle.read()
    values = {
        'DB_NAME': _value(row, 'db_name'),
        'DB_USER': _value(row, 'db_user'),
        'DB_PASSWORD': _value(row, 'db_password'),
        'DB_HOST': _value(row, 'db_host') or 'localhost',
    }
    for key, value in values.items():
        if value is None:
            raise _FleetAbort('snapshot row for %s has no %s' % (_value(row, 'domain'), key))
        pattern = re.compile(
            r"(^\s*define\s*\(\s*['\"]%s['\"]\s*,\s*['\"])(.*?)(['\"]\s*\)\s*;\s*$)" % key,
            re.MULTILINE,
        )
        replacement_value = str(value).replace('\\', '\\\\').replace("'", "\\'")
        contents, count = pattern.subn(r'\g<1>%s\g<3>' % replacement_value, contents)
        if count != 1:
            raise _FleetAbort('staged wp-config for %s has %s %s definitions' % (
                _value(row, 'domain'), count, key
            ))
    with open(path, 'w') as handle:
        handle.write(contents)


def _www_data_ids():
    try:
        import pwd
        import grp
        return pwd.getpwnam('www-data').pw_uid, grp.getgrnam('www-data').gr_gid
    except (ImportError, KeyError):
        return None, None


def _set_owner(path, uid, gid, mode=None):
    try:
        if uid is not None and gid is not None:
            os.chown(path, uid, gid)
        if mode is not None:
            os.chmod(path, mode)
    except OSError as exc:
        raise _FleetAbort('could not set ownership/mode for %s: %s' % (path, exc))


def _verify_site_ownership(domain, paths):
    www_uid, www_gid = _www_data_ids()
    root_uid, root_gid = 0, 0
    for path in (paths.get('vhost'), paths.get('force_ssl'), paths.get('wp_config')):
        if not path or not _lexists(path):
            continue
        if path == paths.get('wp_config'):
            _set_owner(path, www_uid, www_gid, 0o640)
        else:
            _set_owner(path, root_uid, root_gid, 0o644)
    conf_dir = paths.get('conf_nginx')
    if conf_dir and os.path.isdir(conf_dir):
        for current, dirs, files in os.walk(conf_dir, followlinks=False):
            for name in files:
                _set_owner(os.path.join(current, name), root_uid, root_gid, 0o644)
    uploads = paths.get('uploads')
    if uploads and os.path.isdir(uploads):
        for current, dirs, files in os.walk(uploads, followlinks=False):
            _set_owner(current, www_uid, www_gid)
            for name in files:
                _set_owner(os.path.join(current, name), www_uid, www_gid)


def _ensure_site_skeleton(row):
    domain = _value(row, 'domain')
    site_root = _value(row, 'site_path') or '/var/www/%s' % domain
    for path in (
        site_root,
        os.path.join(site_root, 'htdocs'),
        os.path.join(site_root, 'conf', 'nginx'),
        os.path.join(site_root, 'logs'),
    ):
        os.makedirs(path, mode=0o755, exist_ok=True)
    www_uid, www_gid = _www_data_ids()
    if www_uid is not None:
        _set_owner(site_root, www_uid, www_gid)
        _set_owner(os.path.join(site_root, 'htdocs'), www_uid, www_gid)
        _set_owner(os.path.join(site_root, 'logs'), www_uid, www_gid)
    _ensure_mode(site_root, 0o755)
    _ensure_mode(os.path.join(site_root, 'htdocs'), 0o755)
    _ensure_mode(os.path.join(site_root, 'conf'), 0o755)
    _ensure_mode(os.path.join(site_root, 'conf', 'nginx'), 0o755)
    _ensure_mode(os.path.join(site_root, 'logs'), 0o755)
    return site_root


def _rsync_directory(source, destination, excludes=()):
    os.makedirs(os.path.dirname(destination.rstrip('/')), mode=0o755, exist_ok=True)
    argv = ['rsync', '-a', '--delete']
    for exclude in excludes:
        argv.extend(['--exclude', exclude])
    argv.extend([source.rstrip('/') + '/', destination.rstrip('/') + '/'])
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except OSError as exc:
        raise _FleetAbort('rsync failed to start: %s' % exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise _FleetAbort('rsync failed: %s' % detail[-1000:])


def _apply_site_files_from_staged(controller, domain, staged_root, row):
    """Apply one site's files from the one shared fleet restic restore.

    This is the intentional fleet-only primitive-reuse deviation: calling the
    frozen restore_site_files() for every domain would restore the complete
    fleet snapshot N times. The implementation mirrors its staged rsync,
    wp-config rewrite, optional force-SSL exactness, and ownership checks.
    """
    snapshot_paths = site_file_paths(domain)
    live_paths = dict(snapshot_paths)
    default_root = '/var/www/%s' % domain
    site_root = _value(row, 'site_path') or default_root
    if site_root != default_root:
        for key, path in list(live_paths.items()):
            if path.startswith(default_root + '/'):
                live_paths[key] = site_root + path[len(default_root):]
    staged_paths = {}
    for key, snapshot_path in snapshot_paths.items():
        staged_paths[key] = _staged_path(staged_root, snapshot_path)

    wp_config = staged_paths.get('wp_config')
    if wp_config is None or not os.path.isfile(wp_config):
        raise _FleetAbort('fleet files snapshot has no wp-config.php for %s' % domain)
    _rewrite_wp_config(wp_config, row)

    for key, live_path in live_paths.items():
        source = staged_paths.get(key)
        if os.path.isdir(live_path) and os.path.islink(live_path):
            _remove_path(live_path)
        if source is not None and os.path.isdir(source) and not os.path.islink(source):
            _rsync_directory(
                source,
                live_path,
                excludes=('multitenancy-maintenance.conf',)
                if key == 'conf_nginx' else (),
            )
        elif source is not None and os.path.isfile(source):
            _copy_file_atomic(source, live_path)
        elif key == 'force_ssl':
            # OPTIONAL path (§4): absent from snapshot -> delete live copy if
            # present, no-op when absent on both sides. Never "required".
            if _lexists(live_path):
                _remove_path(live_path)
        elif source is None and key in ('uploads', 'conf_nginx'):
            if key == 'conf_nginx':
                _remove_directory_contents_except(
                    live_path,
                    {'multitenancy-maintenance.conf'},
                )
            elif _lexists(live_path):
                _remove_path(live_path)
        elif source is None:
            raise _FleetAbort('fleet files snapshot has no required %s for %s' % (key, domain))

    enabled = '/etc/nginx/sites-enabled/%s' % domain
    vhost = live_paths.get('vhost')
    if bool(_value(row, 'is_enabled', True)):
        if vhost and _lexists(vhost) and not _lexists(enabled):
            os.makedirs(os.path.dirname(enabled), mode=0o755, exist_ok=True)
            os.symlink(vhost, enabled)
    elif _lexists(enabled):
        _remove_path(enabled)
    _verify_site_ownership(domain, live_paths)


def _restore_global_paths(files_snapshot_id, staged_global, global_config):
    global_paths = [
        global_config.get('shared_root', '/var/www/shared') + '/config',
        global_config.get('shared_root', '/var/www/shared') + '/.git',
        global_config.get('shared_root', '/var/www/shared') + '/wp-content',
        '/etc/wo/plugins.d/multitenancy.conf',
        '/etc/letsencrypt',
    ]
    includes = [path for path in global_paths if _lexists(path)]
    # The source may not exist in a selected snapshot; include all known paths
    # so replacement semantics can remove a stale live optional path.
    args = ['restore', files_snapshot_id, '--target', staged_global]
    for path in global_paths:
        args.extend(['--include', path])
    run_restic(args)
    for live_path in global_paths:
        source = _staged_path(staged_global, live_path)
        if source is not None and os.path.isdir(source) and not os.path.islink(source):
            _rsync_directory(source, live_path)
        elif source is not None and os.path.isfile(source):
            _copy_file_atomic(source, live_path)
        elif live_path in includes and _lexists(live_path):
            # A path known to be part of the live set but absent from the
            # selected snapshot is removed for exact replacement semantics.
            _remove_path(live_path)


def _site_staging_dir(operation_root, domain):
    path = os.path.join(operation_root, domain)
    _mkdir_private(path)
    return path




def _apply_snapshot_sites(controller, operation_root, files_snapshot_id,
                          snapshot_manifest, db_snapshots, failed):
    staged_files = os.path.join(operation_root, 'fleet-files')
    _mkdir_private(staged_files)
    run_restic(['restore', files_snapshot_id, '--target', staged_files])

    gated_late = []
    config = MTFunctions.load_config(controller)
    maintenance_enable, _maintenance_disable = _maintenance_helpers()
    for domain in sorted(snapshot_manifest):
        row = snapshot_manifest[domain]
        preexisting_failure = failed.get(domain)
        try:
            site_root = _value(row, 'site_path') or '/var/www/%s' % domain
            if not os.path.isdir(site_root):
                _ensure_site_skeleton(row)
                if not maintenance_enable(
                        controller, domain,
                        'Fleet restore in progress', config):
                    raise _FleetAbort('could not enable maintenance gate')
                gated_late.append(domain)
            if domain not in db_snapshots:
                failed.setdefault(domain, 'no tenant DB snapshot at requested time')
                continue
            site_stage = _site_staging_dir(operation_root, domain)
            # Files are applied from the one shared staged restore; this avoids
            # N full restic restores while preserving replacement semantics.
            _apply_site_files_from_staged(controller, domain, staged_files, row)
            MTFunctions.create_shared_symlinks(
                controller,
                os.path.join(site_root, 'htdocs'),
                config.get('shared_root', '/var/www/shared'),
            )
            restore_site_db(
                controller,
                domain,
                _snapshot_id(db_snapshots[domain]),
                site_stage,
                row,
            )
            try:
                flush_site_caches(controller, domain, row)
            except Exception as exc:
                Log.warn(controller, 'Cache flush warning for %s: %s' % (domain, exc))
            if preexisting_failure is None:
                failed.pop(domain, None)
        except Exception as exc:
            failed.setdefault(domain, str(exc))
            Log.error(controller, 'Fleet restore failed for %s: %s' % (domain, exc), exit=False)
    return staged_files, gated_late


def _commit_fleet(controller, operation_root, snapshot_manifest,
                  failed, initially_gated, late_gated):
    commit_error = None
    try:
        nginx_result = nginx_test_and_reload(controller)
        if nginx_result is False:
            raise _FleetAbort('nginx test/reload failed at fleet commit')
        if MTFunctions.sync_wp_cron_entries(controller) is False:
            raise _FleetAbort('could not synchronize WP-Cron entries after fleet restore')
        try:
            repair_backup_cron(controller)
        except Exception as exc:
            Log.warn(controller, 'Could not repair backup cron: %s' % exc)
        _, maintenance_disable = _maintenance_helpers()
        successful = [domain for domain in snapshot_manifest if domain not in failed]
        ungated = []
        for domain in sorted(successful):
            try:
                if not maintenance_disable(controller, domain):
                    failed[domain] = 'could not disable maintenance gate'
                    continue
                ungated.append(domain)
            except Exception as exc:
                failed[domain] = 'could not finalize %s: %s' % (domain, exc)
                Log.error(
                    controller,
                    'Could not finalize fleet restore for %s: %s' % (domain, exc),
                    exit=False,
                )
        try:
            nginx_result = nginx_test_and_reload(controller)
            if nginx_result is False:
                raise _FleetAbort('nginx test/reload failed after fleet ungate')
        except Exception as exc:
            raise _FleetAbort('nginx test/reload failed after fleet ungate: %s' % exc)
        for domain in ungated:
            site_stage = os.path.join(operation_root, domain)
            if _lexists(site_stage):
                shutil.rmtree(site_stage)
        return None
    except Exception as exc:
        commit_error = str(exc)
    if commit_error:
        failed.setdefault('__fleet__', commit_error)
        Log.error(controller, 'Fleet restore commit failed: %s' % commit_error, exit=False)


def _print_summary(controller, results, quarantined, operation_id, domains=()):
    Log.info(controller, 'Fleet restore summary (operation %s):' % operation_id)
    all_domains = set(domains) | {
        d for d in results if d != '__fleet__'
    }
    for domain in sorted(all_domains):
        error = results.get(domain)
        if error:
            Log.info(controller, '  %-40s FAILED: %s' % (domain, error))
        else:
            Log.info(controller, '  %-40s SUCCESS' % domain)
    if results.get('__fleet__'):
        Log.info(controller, '  fleet commit: FAILED: %s' % results['__fleet__'])
    if quarantined:
        Log.info(controller, 'Quarantined current-only tenants:')
        for item in quarantined:
            domain = item['domain']
            hint = (
                'wo multitenancy create %s && wo multitenancy backup restore %s '
                '--all --operation=%s'
            ) % (domain, domain, operation_id)
            Log.info(controller, '  %s (%s); reinstate: %s' % (
                domain, item['directory'], hint
            ))


def _restore_fleet_locked(controller, pargs):
    at = getattr(pargs, 'at', None)
    files_snapshot = resolve_snapshot('files', at=at)
    files_snapshot_id = _summary_snapshot_id_or_abort(files_snapshot, 'files snapshot resolution')

    staged_dbase, dbase_target = _stage_snapshot_dbase(files_snapshot_id)
    try:
        snapshot_manifest = _load_snapshot_manifest(staged_dbase)
    except Exception:
        shutil.rmtree(dbase_target, ignore_errors=True)
        raise
    current_manifest = _load_current_manifest(controller)

    db_snapshots = {}
    resolve_errors = {}
    for domain in sorted(snapshot_manifest):
        try:
            db_snapshot = resolve_snapshot('db', domain=domain, at=at)
            if not _snapshot_id(db_snapshot):
                raise _FleetAbort('resolved DB snapshot has no id')
            db_snapshots[domain] = db_snapshot
        except Exception as exc:
            resolve_errors[domain] = 'no tenant DB snapshot at requested time: %s' % exc
            Log.error(controller, 'No DB snapshot for %s: %s' % (domain, exc), exit=False)

    snapshot_only, current_only = _display_diff(
        controller,
        files_snapshot,
        db_snapshots,
        snapshot_manifest,
        current_manifest,
    )
    _confirm(controller, pargs, snapshot_only, current_only)

    operation_id = new_operation_id()
    stamp = operation_id.replace(':', '').replace('/', '-').replace(' ', '-')
    operation_root = os.path.join(RESTORE_ROOT, 'fleet-%s' % stamp)
    _mkdir_private(operation_root)
    config = MTFunctions.load_config(controller)

    _capture_safety(
        controller,
        current_manifest,
        operation_id,
        operation_root,
        config,
    )

    affected = set(current_manifest)
    for domain, row in snapshot_manifest.items():
        site_root = _value(row, 'site_path') or '/var/www/%s' % domain
        if os.path.isdir(site_root):
            affected.add(domain)
    gated = []
    maintenance_enable, _maintenance_disable = _maintenance_helpers()
    for domain in sorted(affected):
        if not maintenance_enable(controller, domain,
                                  'Fleet restore in progress', config):
            raise _FleetAbort('could not enable maintenance gate for %s' % domain)
        gated.append(domain)
    try:
        nginx_result = nginx_test_and_reload(controller)
        if nginx_result is False:
            raise _FleetAbort('nginx test/reload failed after enabling fleet gates')
    except Exception as exc:
        raise _FleetAbort(
            'nginx test/reload failed after enabling fleet gates: %s' % exc
        )

    quarantined = _quarantine_current_only(
        controller,
        current_manifest,
        current_only,
        operation_id,
        stamp,
    )

    # The first live write is the verified snapshot database cutover.
    db_session.remove()
    os.replace(staged_dbase, '/var/lib/wo/dbase.db')
    _ensure_mode('/var/lib/wo/dbase.db', 0o600)
    shutil.rmtree(dbase_target, ignore_errors=True)

    staged_global = os.path.join(operation_root, 'staged-global')
    _mkdir_private(staged_global)
    _restore_global_paths(files_snapshot_id, staged_global, config)

    failures = dict(resolve_errors)
    _ensure_snapshot_databases(controller, snapshot_manifest, failures)
    _, late_gated = _apply_snapshot_sites(
        controller,
        operation_root,
        files_snapshot_id,
        snapshot_manifest,
        db_snapshots,
        failures,
    )
    _commit_fleet(
        controller,
        operation_root,
        snapshot_manifest,
        failures,
        gated,
        late_gated,
    )
    _print_summary(
        controller,
        failures,
        quarantined,
        operation_id,
        domains=snapshot_manifest,
    )
    return not failures

def _close_failure(controller, message):
    Log.error(controller, message, exit=False)
    try:
        controller.app.close(1)
    except AttributeError:
        pass


def cmd_restore_fleet(controller):
    """Run ``wo multitenancy backup restore --all-sites`` end to end."""
    pargs = controller.app.pargs
    invalid = []
    for option, attribute in (
        ('--db', 'db'),
        ('--files', 'files'),
        ('--all', 'all'),
        ('--snapshot', 'snapshot'),
        ('--operation', 'operation'),
        ('<site_name>', 'site_name'),
        ('--cron', 'cron'),
    ):
        if getattr(pargs, attribute, None):
            invalid.append(option)
    if invalid:
        _close_failure(
            controller,
            'Usage error: --all-sites accepts only --at and --force; '
            'unsupported options: %s' % ', '.join(invalid),
        )
        return None
    try:
        configured = backup_is_configured()
    except Exception as exc:
        _close_failure(controller, 'Could not verify backup configuration: %s' % exc)
        return None
    if not configured:
        _close_failure(controller, 'Backup is not configured; run `wo multitenancy backup init`')
        return None

    try:
        with operation_lock('backup restore --all-sites', blocking=False):
            ok = _restore_fleet_locked(controller, pargs)
    except OperationLockBusy as exc:
        holder = getattr(exc, 'holder', str(exc))
        _close_failure(controller, 'Backup restore is already running: %s' % holder)
        return None
    except Exception as exc:
        _close_failure(controller, 'Fleet restore aborted: %s' % exc)
        return None
    if not ok:
        try:
            controller.app.close(1)
        except AttributeError:
            pass
    return None
