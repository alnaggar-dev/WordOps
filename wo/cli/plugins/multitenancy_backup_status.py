"""Status, listing, and explicit orphan-site purge for fleet backups."""

import json
import os
from datetime import datetime, timezone

from wo.core.logging import Log


DB_PERIOD_SECONDS = 3600
DB_STALE_SECONDS = 2 * DB_PERIOD_SECONDS
CHECK_WARNING_SECONDS = 35 * 86400


def _backup_api():
    """Load the backup engine lazily so controller imports stay lightweight."""
    from wo.cli.plugins.multitenancy_backup_functions import (
        QUARANTINE_ROOT,
        backup_is_configured,
        enumerate_enabled_tenants,
        forget_snapshot_ids,
        list_snapshots,
        load_backup_config,
        load_state,
        read_tombstones,
        run_restic,
    )

    return {
        'QUARANTINE_ROOT': QUARANTINE_ROOT,
        'backup_is_configured': backup_is_configured,
        'enumerate_enabled_tenants': enumerate_enabled_tenants,
        'forget_snapshot_ids': forget_snapshot_ids,
        'list_snapshots': list_snapshots,
        'load_backup_config': load_backup_config,
        'load_state': load_state,
        'read_tombstones': read_tombstones,
        'run_restic': run_restic,
    }


def _info(controller, message):
    Log.info(controller, message)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', 'ok', 'success')
    return bool(value)


def _parse_time(value):
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
        if not text:
            return None
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


def _timestamp(value):
    if value is None or value == '':
        return '-'
    if isinstance(value, datetime):
        parsed = _parse_time(value)
        return parsed.isoformat().replace('+00:00', 'Z') if parsed else str(value)
    return str(value)


def _age_seconds(value, now=None):
    parsed = _parse_time(value)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


def _human_seconds(value):
    if value is None:
        return '-'
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return '-'
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f'{seconds:.1f}s'
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f'{int(minutes)}m {int(remainder)}s'
    hours, remainder = divmod(minutes, 60)
    return f'{int(hours)}h {int(remainder)}m'


def _human_bytes(value):
    """Render bytes using binary units, preserving the measured data-added value."""
    if value is None or value == '':
        return '-'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '-'
    if number < 0:
        number = 0.0
    units = ('B', 'KiB', 'MiB', 'GiB', 'TiB')
    unit_index = 0
    while number >= 1024 and unit_index < len(units) - 1:
        number /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f'{int(number)} B'
    return f'{number:.1f} {units[unit_index]}'


def _duration(record):
    if not isinstance(record, dict):
        return None
    if record.get('duration') is not None:
        try:
            return float(record.get('duration'))
        except (TypeError, ValueError):
            pass
    started = _parse_time(record.get('started'))
    finished = _parse_time(record.get('finished'))
    if started is not None and finished is not None:
        return max(0.0, (finished - started).total_seconds())
    return None


def _domain(value):
    if isinstance(value, dict):
        return value.get('domain')
    return getattr(value, 'domain', None)


def _tags(snapshot):
    if not isinstance(snapshot, dict):
        return []
    raw = snapshot.get('tags') or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(tag) for tag in raw]


def _snapshot_id(snapshot):
    if not isinstance(snapshot, dict):
        return ''
    return str(snapshot.get('id') or snapshot.get('snapshot_id') or '')


def _snapshot_time(snapshot):
    if not isinstance(snapshot, dict):
        return None
    return _parse_time(snapshot.get('time') or snapshot.get('timestamp'))


def _snapshot_time_text(snapshot):
    if not isinstance(snapshot, dict):
        return '-'
    return _timestamp(snapshot.get('time') or snapshot.get('timestamp'))


def _snapshot_summary(snapshot):
    if not isinstance(snapshot, dict):
        return '-'
    paths = snapshot.get('paths') or snapshot.get('files') or []
    if isinstance(paths, str):
        paths = [paths]
    if paths:
        rendered = []
        for path in paths[:4]:
            rendered.append(os.path.basename(str(path).rstrip('/')) or str(path))
        if len(paths) > 4:
            rendered.append(f'+{len(paths) - 4} more')
        return ','.join(rendered)
    for key in ('filename', 'stdin_filename', 'name'):
        if snapshot.get(key):
            return str(snapshot[key])
    return '-'


def _operation_id(snapshot):
    for tag in _tags(snapshot):
        if tag.startswith('operation:'):
            return tag.split(':', 1)[1]
    return None


def _grace_days(config):
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


def _read_operation_id(directory):
    candidates = ('manifest.json', 'quarantine-manifest.json', 'operation-manifest.json')
    paths = []
    for name in candidates:
        paths.append(os.path.join(directory, name))
    try:
        for name in os.listdir(directory):
            if name.endswith('.json'):
                path = os.path.join(directory, name)
                if path not in paths:
                    paths.append(path)
    except OSError:
        return '-'

    def find_id(value):
        if isinstance(value, dict):
            for key in ('op_id', 'operation_id', 'operation'):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
                if isinstance(candidate, dict):
                    found = find_id(candidate)
                    if found:
                        return found
            for nested in value.values():
                found = find_id(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = find_id(nested)
                if found:
                    return found
        return None

    for path in paths:
        try:
            with open(path) as handle:
                operation_id = find_id(json.load(handle))
            if operation_id:
                return operation_id
        except (OSError, ValueError, TypeError):
            continue
    return '-'


def _quarantine_entries(root):
    entries = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return entries
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path):
                entries.append((name, _read_operation_id(path)))
        except OSError:
            continue
    return entries


def _stats_payload(result):
    raw = getattr(result, 'stdout', result)
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='replace')
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return raw[-1] if raw else {}
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed[-1] if parsed else {}
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        for line in reversed(raw.splitlines()):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue
    return {}


def _last_success_age(record, fallback_finished=None):
    if not isinstance(record, dict) or not _as_bool(record.get('ok')):
        return None
    for key in ('finished', 'success_at', 'last_success', 'completed_at', 'timestamp'):
        if record.get(key):
            age = _age_seconds(record.get(key))
            if age is not None:
                return age
    return _age_seconds(fallback_finished)


def _print_runs(controller, runs):
    _info(controller, 'RUNS:')
    for family in ('db', 'files', 'retention', 'prune', 'check'):
        record = runs.get(family)
        if not isinstance(record, dict):
            _info(controller, f'  {family}: no runs recorded')
            continue
        ok = 'yes' if _as_bool(record.get('ok')) else 'no'
        _info(
            controller,
            f'  {family}: started={_timestamp(record.get("started"))} '
            f'finished={_timestamp(record.get("finished"))} '
            f'duration={_human_seconds(_duration(record))} ok={ok}',
        )
    check = runs.get('check')
    if not isinstance(check, dict) or not check.get('finished'):
        _info(controller, '  last-check age: never')
        _info(controller, '  WARNING: last-check has never completed')
    else:
        age = _age_seconds(check.get('finished'))
        if age is None:
            _info(controller, '  last-check age: unknown')
        else:
            _info(controller, f'  last-check age: {_human_seconds(age)}')
            if age > CHECK_WARNING_SECONDS:
                _info(controller, '  WARNING: last-check is older than 35 days')


def _print_tenants(controller, app, api, db_run):
    try:
        enabled = api['enumerate_enabled_tenants'](app)
        enabled = list(enabled or [])
        tenant_error = None
    except Exception as exc:
        enabled = []
        tenant_error = str(exc)

    tenant_records = db_run.get('tenants') if isinstance(db_run, dict) else {}
    if not isinstance(tenant_records, dict):
        tenant_records = {}
    domains = {_domain(tenant) for tenant in enabled}
    domains.update(str(domain) for domain in tenant_records if domain)
    domains = sorted(domain for domain in domains if domain)

    _info(controller, 'TENANTS (DB):')
    _info(controller, '  domain  status  duration  data_added  details')
    if tenant_error:
        _info(controller, f'  unable to enumerate enabled tenants: {tenant_error}')
    if not domains:
        _info(controller, '  no enabled tenants')
        return

    finished = db_run.get('finished') if isinstance(db_run, dict) else None
    for domain in domains:
        record = tenant_records.get(domain)
        if not isinstance(record, dict):
            record = None
        ok = record is not None and _as_bool(record.get('ok'))
        age = _last_success_age(record, finished)
        stale = record is None or not ok or (age is not None and age > DB_STALE_SECONDS)
        if record is None:
            status = 'missing'
            duration = '-'
            data_added = '-'
            detail = 'missing from last run'
        elif ok:
            status = 'ok'
            duration = _human_seconds(record.get('duration'))
            data_added = _human_bytes(record.get('data_added'))
            detail = ''
        else:
            status = 'error'
            duration = _human_seconds(record.get('duration'))
            data_added = _human_bytes(record.get('data_added'))
            detail = str(record.get('error') or 'snapshot failed')
        if stale:
            status += ' STALE'
            if age is not None and age > DB_STALE_SECONDS:
                detail = (detail + '; ' if detail else '') + 'last success older than 2h'
        _info(
            controller,
            f'  {domain}  {status}  {duration}  {data_added}  {detail}'.rstrip(),
        )


def _print_capacity(controller, db_run):
    duration = _duration(db_run)
    _info(controller, 'CAPACITY:')
    if duration is None:
        _info(controller, '  db run duration: not measured')
        return
    ratio = duration / float(DB_PERIOD_SECONDS)
    line = (
        f'  db run duration: {_human_seconds(duration)} / '
        f'{_human_seconds(DB_PERIOD_SECONDS)} ({ratio * 100:.1f}% of hourly period)'
    )
    _info(controller, line)
    if duration > DB_PERIOD_SECONDS * 0.5:
        _info(controller, '  WARNING: db run exceeds 50% of the hourly period')


def _print_repo(controller, api):
    _info(controller, 'REPOSITORY:')
    try:
        stats = _stats_payload(api['run_restic'](['stats', '--json']))
        total_size = stats.get('total_size', stats.get('total_size_bytes'))
        total_files = stats.get('total_file_count', stats.get('total_files'))
        _info(controller, f'  total size: {_human_bytes(total_size)}')
        _info(controller, f'  total file count: {total_files if total_files is not None else "-"}')
    except Exception as exc:
        _info(controller, f'  stats unavailable: {exc}')

    for family in ('db', 'files'):
        try:
            snapshots = api['list_snapshots']([family]) or []
            _info(controller, f'  {family} snapshots: {len(snapshots)}')
        except Exception as exc:
            _info(controller, f'  {family} snapshots: unavailable ({exc})')


def _print_tombstones(controller, api, config):
    _info(controller, 'PENDING TOMBSTONES:')
    try:
        tombstones = api['read_tombstones']() or []
    except Exception as exc:
        _info(controller, f'  unavailable: {exc}')
        return
    if not tombstones:
        _info(controller, '  none')
        return
    grace_seconds = _grace_days(config) * 86400
    for tombstone in tombstones:
        if not isinstance(tombstone, dict):
            continue
        domain = tombstone.get('domain') or '-'
        deleted_at = tombstone.get('deleted_at') or tombstone.get('timestamp')
        age = _age_seconds(deleted_at)
        if age is None:
            age_text = 'unknown age'
            remaining = 'unknown'
        else:
            age_text = _human_seconds(age)
            remaining_seconds = grace_seconds - age
            remaining = 'expired' if remaining_seconds <= 0 else _human_seconds(remaining_seconds)
        _info(
            controller,
            f'  {domain}: age={age_text} grace remaining={remaining}',
        )


def _print_quarantine(controller, api):
    _info(controller, 'QUARANTINE:')
    entries = _quarantine_entries(api['QUARANTINE_ROOT'])
    if not entries:
        _info(controller, '  none')
        return
    for name, operation_id in entries:
        _info(controller, f'  {name}: manifest op_id={operation_id}')


def _print_anomalies(controller, state):
    anomalies = state.get('anomalies') if isinstance(state, dict) else {}
    if not isinstance(anomalies, dict):
        anomalies = {}
    orphan_sites = anomalies.get('orphan_sites') or []
    _info(controller, 'ANOMALIES:')
    if not orphan_sites:
        _info(controller, '  none')
        return
    for entry in orphan_sites:
        if isinstance(entry, dict):
            domain = entry.get('domain') or entry.get('site') or str(entry)
        else:
            domain = str(entry)
        _info(
            controller,
            f'  orphan site: {domain} — purge hint: '
            f'wo multitenancy backup forget-site {domain}',
        )


def cmd_status(controller):
    """Display local run state, tenant freshness, and repository visibility."""
    api = _backup_api()
    if not api['backup_is_configured']():
        _info(controller, 'Backup is not configured.')
        _info(controller, 'Run: wo multitenancy backup init')
        return

    app = controller.app
    try:
        config = api['load_backup_config'](app) or {}
    except Exception as exc:
        config = {}
        _info(controller, f'WARNING: unable to load backup config: {exc}')
    try:
        state = api['load_state']() or {}
    except Exception as exc:
        state = {}
        _info(controller, f'WARNING: unable to load backup state: {exc}')
    if not isinstance(state, dict):
        state = {}
    runs = state.get('runs') or {}
    if not isinstance(runs, dict):
        runs = {}

    _info(controller, '')
    _info(controller, '=== WordPress Multi-tenancy Backup Status ===')
    _info(controller, '')
    if not runs:
        _info(controller, 'no runs recorded yet')
        _info(controller, '')
    _print_runs(controller, runs)
    _info(controller, '')
    _print_tenants(controller, app, api, runs.get('db') or {})
    _info(controller, '')
    _print_capacity(controller, runs.get('db') or {})
    _info(controller, '')
    _print_repo(controller, api)
    _info(controller, '')
    _print_tombstones(controller, api, config)
    _info(controller, '')
    _print_quarantine(controller, api)
    _info(controller, '')
    _print_anomalies(controller, state)
    _info(controller, '')
    _info(controller, '===============================================')


def _list_filter_groups(site_name, db, files):
    selected = []
    if db:
        selected.append('db')
    if files:
        selected.append('files')
    if not selected:
        selected = ['db', 'files']
        if not site_name:
            selected.append('pre-restore')
    if site_name:
        site_tag = f'site:{site_name}'
        groups = []
        for family in selected:
            if family == 'files':
                # Daily files snapshots are fleet-wide; retain the compound
                # group too so site-tagged safety captures remain visible.
                groups.extend(['files', f'files,{site_tag}'])
            else:
                groups.append(f'{family},{site_tag}')
        return groups
    return selected


def _snapshot_matches(snapshot, site_name, db, files):
    tags = set(_tags(snapshot))
    families = []
    if db:
        families.append('db')
    if files:
        families.append('files')
    if not families:
        families = ['db', 'files']
        if not site_name:
            families.append('pre-restore')

    if not site_name:
        return any(family in tags for family in families)

    site_tag = f'site:{site_name}'
    site_tags = {tag for tag in tags if tag.startswith('site:')}
    if 'db' in families and 'db' in tags and site_tag in tags:
        return True
    return (
        'files' in families
        and 'files' in tags
        and (
            site_tag in site_tags
            or (not site_tags and 'pre-restore' not in tags)
        )
    )



def _print_snapshot_row(controller, snapshot, indent='  '):
    snapshot_id = _snapshot_id(snapshot)
    short_id = snapshot.get('short_id') if isinstance(snapshot, dict) else None
    short_id = str(short_id or snapshot_id[:8] or '-')
    tags = ','.join(_tags(snapshot)) or '-'
    _info(
        controller,
        f'{indent}{short_id:<10} {_snapshot_time_text(snapshot):<25} '
        f'{tags:<55} {_snapshot_summary(snapshot)}',
    )


def cmd_list(controller):
    """List matching restic snapshots in chronological order."""
    api = _backup_api()
    pargs = getattr(controller.app, 'pargs', None)
    site_name = getattr(pargs, 'site_name', None)
    site_name = site_name.strip() if isinstance(site_name, str) else site_name
    db = bool(getattr(pargs, 'db', False))
    files = bool(getattr(pargs, 'files', False))
    filters = _list_filter_groups(site_name, db, files)
    try:
        snapshots = api['list_snapshots'](filters) or []
    except Exception as exc:
        Log.error(controller, f'Unable to list backup snapshots: {exc}')
        return
    snapshots = [
        snapshot for snapshot in snapshots
        if isinstance(snapshot, dict) and _snapshot_matches(snapshot, site_name, db, files)
    ]
    snapshots.sort(key=lambda snapshot: _snapshot_time(snapshot) or datetime.min.replace(tzinfo=timezone.utc))

    _info(controller, 'BACKUP SNAPSHOTS:')
    if not snapshots:
        _info(controller, '  none')
        return
    _info(controller, '  id        time                      tags                                                    paths')

    groups = {}
    for snapshot in snapshots:
        operation = _operation_id(snapshot)
        key = f'operation:{operation}' if operation else f'snapshot:{_snapshot_id(snapshot)}'
        groups.setdefault(key, []).append(snapshot)
    grouped = sorted(
        groups.values(),
        key=lambda group: _snapshot_time(group[-1]) or datetime.min.replace(tzinfo=timezone.utc),
    )
    for group in grouped:
        operation = _operation_id(group[0])
        if operation:
            _info(controller, f'  OPERATION {operation}:')
        for snapshot in group:
            _print_snapshot_row(controller, snapshot, indent='    ' if operation else '  ')


def _live_tracked_domains(controller, api):
    """Return tracked domains, failing closed when the direct backup query fails."""
    try:
        tenants = api['enumerate_enabled_tenants'](controller.app)
    except Exception as exc:
        raise RuntimeError(f'unable to verify tracked tenants: {exc}')
    domains = {_domain(tenant) for tenant in (tenants or [])}

    # Include disabled rows too: disabled is still a live tracked tenant, not an
    # orphan.  The legacy helper is best-effort, while the direct backup query
    # above remains the fail-closed authority for enabled tenants.
    try:
        from wo.cli.plugins.multitenancy_db import MTDatabase
        rows = MTDatabase.get_shared_sites(controller) or []
        domains.update(_domain(row) for row in rows)
    except Exception:
        pass
    return {str(domain) for domain in domains if domain}


def cmd_forget_site(controller):
    """Explicitly purge non-fleet snapshots carrying one orphan site tag."""
    api = _backup_api()
    pargs = getattr(controller.app, 'pargs', None)
    site_name = getattr(pargs, 'site_name', None)
    site_name = site_name.strip() if isinstance(site_name, str) else site_name
    if not site_name:
        Log.error(controller, 'A site name is required: backup forget-site <domain>')
        return

    try:
        tracked = _live_tracked_domains(controller, api)
    except RuntimeError as exc:
        Log.error(controller, str(exc))
        return
    if site_name in tracked:
        Log.error(controller, f'Refusing to forget snapshots for live tracked tenant {site_name}')
        return

    try:
        tombstones = api['read_tombstones']() or []
    except Exception as exc:
        Log.error(controller, f'Unable to read backup tombstones: {exc}')
        return
    tombstone = next(
        (
            item for item in tombstones
            if isinstance(item, dict) and item.get('domain') == site_name
        ),
        None,
    )
    force = bool(getattr(pargs, 'force', False))
    if tombstone and not force:
        _info(
            controller,
            f'Tombstone exists for {site_name}; the daily sweep will handle it. '
            'Use --force to purge now.',
        )
        return
    if tombstone and force:
        _info(controller, f'Forcing purge despite the pending tombstone for {site_name}.')

    site_tag = f'site:{site_name}'
    try:
        snapshots = api['list_snapshots']([site_tag]) or []
    except Exception as exc:
        Log.error(controller, f'Unable to inspect snapshots for {site_name}: {exc}')
        return
    matching = []
    fleet_only = []
    seen = set()
    for snapshot in snapshots:
        tags = set(_tags(snapshot))
        if site_tag not in tags:
            continue
        snapshot_id = _snapshot_id(snapshot)
        if 'fleet' in tags:
            fleet_only.append(snapshot)
            continue
        if snapshot_id and snapshot_id not in seen:
            seen.add(snapshot_id)
            matching.append(snapshot)

    if not matching:
        if fleet_only:
            _info(
                controller,
                f'Only fleet-tagged snapshots exist for {site_name}; nothing to do. '
                'Fleet sets are indivisible and cannot be purged per site.',
            )
        else:
            _info(controller, f'No snapshots found for site:{site_name}.')
        return

    times = [snapshot_time for snapshot_time in (_snapshot_time(item) for item in matching) if snapshot_time]
    oldest = min(times).isoformat().replace('+00:00', 'Z') if times else '-'
    newest = max(times).isoformat().replace('+00:00', 'Z') if times else '-'
    _info(
        controller,
        f'Found {len(matching)} non-fleet snapshots for site:{site_name} '
        f'(oldest={oldest}, newest={newest}).',
    )
    if not force:
        try:
            answer = input('Forget these snapshots? [y/N]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ''
        if answer != 'y':
            _info(controller, 'Forget-site cancelled.')
            return

    ids = [_snapshot_id(snapshot) for snapshot in matching]
    try:
        api['forget_snapshot_ids'](ids, prune=False)
    except Exception as exc:
        Log.error(controller, f'Unable to forget snapshots for {site_name}: {exc}')
        return
    _info(controller, f'Forgot {len(ids)} snapshots for site:{site_name}.')
