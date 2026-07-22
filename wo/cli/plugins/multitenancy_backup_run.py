"""Initialisation, scheduled runs, retention, and repository checks."""

import bz2
import getpass
import hashlib
import os
import platform
import re
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from wo.core.logging import Log
from wo.cli.plugins.multitenancy_db import MultitenancySite
from wo.core.database import db_session
from wo.cli.plugins.multitenancy_functions import MTFunctions
from wo.cli.plugins.multitenancy_backup_functions import (
    BACKUP_ENV_FILE,
    CACHE_DIR,
    GATE_EXCLUDES,
    RESTIC_BIN,
    RESTIC_SHA256,
    RESTIC_URL,
    RESTIC_VERSION,
    BackupConfigError,
    BackupError,
    OperationLockBusy,
    backup_is_configured,
    ensure_backup_dirs,
    enumerate_enabled_tenants,
    forget_snapshot_ids,
    list_snapshots,
    load_backup_config,
    load_backup_env,
    load_state,
    mariadb_dump_argv,
    operation_lock,
    parse_keep_policy,
    ping_deadman,
    record_run,
    remove_tombstone,
    restic_backup_paths,
    restic_backup_stdin_command,
    read_tombstones,
    run_restic,
    save_state,
    site_file_paths,
    stage_sqlite_copy,
    write_backup_cron,
)


_RESTIC_FLOOR = (0, 17, 0)
_PASSWORD_WARNING = (
    "The repository password MUST live somewhere besides the server; "
    "losing it loses every backup."
)


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value=None):
    value = value or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_datetime(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    result = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    result = None
            if result is None:
                return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _snapshot_tags(snapshot):
    tags = snapshot.get("tags", ()) if isinstance(snapshot, dict) else ()
    return {str(tag) for tag in (tags or ())}


def _snapshot_id(snapshot):
    if not isinstance(snapshot, dict):
        return None
    return snapshot.get("id") or snapshot.get("short_id")


def _snapshot_time(snapshot):
    if not isinstance(snapshot, dict):
        return None
    return _as_datetime(snapshot.get("time"))


def _operation_groups(snapshots):
    groups = {}
    for snapshot in snapshots or ():
        tags = _snapshot_tags(snapshot)
        operation = next(
            (tag.split(":", 1)[1] for tag in tags if tag.startswith("operation:")),
            None,
        )
        if operation:
            groups.setdefault(operation, []).append(snapshot)
    return groups


def _operation_scope(members):
    if any("fleet" in _snapshot_tags(snapshot) for snapshot in members):
        return "fleet"
    sites = sorted(
        tag for snapshot in members for tag in _snapshot_tags(snapshot)
        if tag.startswith("site:")
    )
    return sites[0] if sites else "unknown"


def _operation_newest(members):
    times = [_snapshot_time(snapshot) for snapshot in members]
    times = [value for value in times if value is not None]
    return max(times) if times else datetime.min.replace(tzinfo=timezone.utc)


def _operation_member_ids(members):
    return [snapshot_id for snapshot_id in (_snapshot_id(s) for s in members)
            if snapshot_id]


def _select_pre_restore_forgets(snapshots, keep=5):
    """Return IDs belonging to pre-restore operations beyond the retention tail."""
    try:
        keep = max(0, int(keep))
    except (TypeError, ValueError):
        keep = 5

    by_scope = {}
    for operation, members in _operation_groups(snapshots).items():
        scope = _operation_scope(members)
        by_scope.setdefault(scope, []).append((
            operation,
            _operation_newest(members),
            members,
        ))

    forgotten = []
    for operations in by_scope.values():
        operations.sort(key=lambda item: (item[1], item[0]), reverse=True)
        for _operation, _newest, members in operations[keep:]:
            forgotten.extend(_operation_member_ids(members))
    return sorted(set(forgotten))


def _select_abandoned_fleet(snapshots, now, grace_days=7):
    """Return IDs from old fleet safety captures without a manifest snapshot."""
    now = _as_datetime(now) or _utc_now()
    try:
        grace_days = float(grace_days)
    except (TypeError, ValueError):
        grace_days = 7
    cutoff = now - timedelta(days=grace_days)
    forgotten = []
    for _operation, members in _operation_groups(snapshots).items():
        tags = [_snapshot_tags(snapshot) for snapshot in members]
        if not any("fleet" in values for values in tags):
            continue
        if any("manifest" in values for values in tags):
            continue
        newest = _operation_newest(members)
        if newest < cutoff:
            forgotten.extend(_operation_member_ids(members))
    return sorted(set(forgotten))


def _abandoned_fleet_operations(snapshots, now, grace_days=7):
    """Return operation IDs selected by _select_abandoned_fleet."""
    now = _as_datetime(now) or _utc_now()
    try:
        grace_days = float(grace_days)
    except (TypeError, ValueError):
        grace_days = 7
    cutoff = now - timedelta(days=grace_days)
    result = []
    for operation, members in _operation_groups(snapshots).items():
        tags = [_snapshot_tags(snapshot) for snapshot in members]
        if (any("fleet" in values for values in tags)
                and not any("manifest" in values for values in tags)
                and _operation_newest(members) < cutoff):
            result.append(operation)
    return sorted(result)


def _config_error(controller, key, error):
    message = "Invalid backup configuration {}: {}".format(key, error)
    Log.error(controller, message, exit=False)
    raise BackupConfigError(message)


def _keep_flags(controller, config, key):
    try:
        flags = parse_keep_policy(str(config.get(key, "")))
    except Exception as error:
        return _config_error(controller, key, error)
    if not flags:
        return _config_error(controller, key, "policy is empty")
    return flags


def _grace_days(controller, config):
    value = config.get("deleted_tenant_grace", 30)
    if isinstance(value, int):
        if value >= 0:
            return value
    else:
        match = re.fullmatch(r"\s*(\d+)d\s*", str(value))
        if match:
            return int(match.group(1))
    return _config_error(controller, "deleted_tenant_grace", value)


def _config_enabled(config):
    value = config.get("enable_backup", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def _load_config_or_error(controller):
    try:
        return load_backup_config(controller)
    except Exception as error:
        Log.error(controller, "Unable to load backup configuration: {}".format(error))
        return {}


def _atomic_write(path, content, mode=0o600):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".wo-backup-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        try:
            os.chown(temporary, 0, 0)
        except (AttributeError, PermissionError, OSError):
            pass
        os.replace(temporary, path)
        os.chmod(path, mode)
        try:
            os.chown(path, 0, 0)
        except (AttributeError, PermissionError, OSError):
            pass
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _endpoint_host(value):
    value = (value or "").strip()
    parsed = urlparse(value if "://" in value else "//" + value)
    host = parsed.netloc or parsed.path
    host = host.strip().rstrip("/")
    if "/" in host:
        host = host.split("/", 1)[0]
    if not host:
        raise ValueError("endpoint host is empty")
    return host


def _env_complete(values):
    required = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
    )
    return isinstance(values, dict) and all(values.get(key) for key in required)


def _credentials(controller):
    if os.path.exists(BACKUP_ENV_FILE):
        try:
            values = load_backup_env()
            if _env_complete(values):
                Log.info(controller, "Using the existing complete {}.".format(
                    BACKUP_ENV_FILE))
                return values, None
        except Exception:
            pass
        Log.warn(controller, "Existing backup credentials are incomplete; prompting for replacement.")

    access_key = input("Cloudflare R2 Access Key ID: ").strip()
    secret_key = getpass.getpass("Cloudflare R2 Secret Access Key: ").strip()
    endpoint = input("Cloudflare R2 S3 endpoint URL: ").strip()
    bucket = input("Cloudflare R2 bucket name: ").strip()
    if not access_key or not secret_key or not endpoint or not bucket:
        Log.error(controller, "All R2 credential fields are required.", exit=False)
        raise BackupConfigError("incomplete R2 credentials")
    try:
        endpoint = _endpoint_host(endpoint)
    except ValueError as error:
        Log.error(controller, "Invalid R2 endpoint: {}".format(error), exit=False)
        raise BackupConfigError(str(error))

    password = secrets.token_urlsafe(32)
    values = {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "RESTIC_REPOSITORY": "s3:https://{}/{}".format(endpoint, bucket),
        "RESTIC_PASSWORD": password,
        "RESTIC_CACHE_DIR": CACHE_DIR,
    }
    content = "".join("{}={}\n".format(key, value) for key, value in values.items())
    _atomic_write(BACKUP_ENV_FILE, content, 0o600)
    return values, password


def _restic_version():
    try:
        result = subprocess.run(
            [RESTIC_BIN, "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = "{}\n{}".format(result.stdout or "", result.stderr or "")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _install_restic(controller):
    version = _restic_version()
    if version is not None and version >= _RESTIC_FLOOR:
        Log.info(controller, "Using restic {} at {}.".format(".".join(map(str, version)), RESTIC_BIN))
        return

    machine = platform.machine().lower()
    architectures = {"x86_64": "amd64", "aarch64": "arm64"}
    arch = architectures.get(machine)
    if not arch:
        Log.error(controller, "Unsupported restic architecture: {}".format(machine))
        raise BackupError("unsupported architecture {}".format(machine))
    url = RESTIC_URL.format(v=RESTIC_VERSION, arch=arch)
    try:
        response = requests.get(url, timeout=(10, 300))
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        elif getattr(response, "status_code", 200) >= 400:
            raise RuntimeError("HTTP {}".format(response.status_code))
        compressed = response.content
        expected = RESTIC_SHA256[arch]
        actual = hashlib.sha256(compressed).hexdigest()
        if actual != expected:
            raise BackupError(
                "restic download SHA256 mismatch (expected {}, got {})".format(
                    expected, actual))
        binary = bz2.decompress(compressed)
    except Exception as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError("unable to download restic: {}".format(error))

    directory = os.path.dirname(RESTIC_BIN) or "."
    os.makedirs(directory, mode=0o755, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".restic-", dir=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(binary)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755)
        try:
            os.chown(temporary, 0, 0)
        except (AttributeError, PermissionError, OSError):
            pass
        os.replace(temporary, RESTIC_BIN)
        os.chmod(RESTIC_BIN, 0o755)
        Log.info(controller, "Installed restic {}.".format(RESTIC_VERSION))
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _restic_init(controller):
    try:
        result = run_restic(["init"], check=False)
    except Exception as error:
        raise BackupError("restic init failed: {}".format(error))
    return_code = getattr(result, "returncode", 0)
    output = "{}\n{}".format(
        getattr(result, "stdout", "") or "",
        getattr(result, "stderr", "") or "",
    ).lower()
    already = (
        "already initialized" in output
        or "config file already exists" in output
        or "config already exists" in output
    )
    if return_code and not already:
        raise BackupError((getattr(result, "stderr", "") or output).strip()
                          or "restic init failed")
    if already:
        Log.info(controller, "Restic repository is already initialized; reusing it.")
    try:
        run_restic(["cat", "config"])
    except Exception as error:
        raise BackupError("restic repository connectivity check failed: {}".format(error))


def _print_password_warning(controller, password=None):
    if password:
        print("RESTIC_PASSWORD={}".format(password))
        Log.warn(controller, _PASSWORD_WARNING)
    else:
        Log.warn(controller, "Reminder: {}".format(_PASSWORD_WARNING))


def _tenant_value(tenant, name, default=None):
    if isinstance(tenant, dict):
        return tenant.get(name, default)
    return getattr(tenant, name, default)


def _run_db(controller, config, cron=False):
    started_at = _utc_now()
    started = time.monotonic()
    if cron:
        ping_deadman(config.get("db_ping_url", ""), "start")
    previous_state = load_state() or {}
    previous_tenants = {}
    if isinstance(previous_state, dict):
        previous_runs = previous_state.get("runs")
        if isinstance(previous_runs, dict):
            previous_db = previous_runs.get("db")
            if isinstance(previous_db, dict):
                candidate = previous_db.get("tenants")
                if isinstance(candidate, dict):
                    previous_tenants = candidate
    tenants = {}
    family_ok = True
    family_error = None
    try:
        tenant_rows = enumerate_enabled_tenants(controller)
        if not tenant_rows:
            Log.info(controller, "No enabled tenants found; DB backup is a successful no-op.")
        for tenant in tenant_rows:
            domain = _tenant_value(tenant, "domain", "<unknown>")
            tenant_started = time.monotonic()
            detail = {
                "ok": False,
                "duration": 0,
                "data_added": 0,
                "snapshot_id": None,
                "last_success": None,
                "last_success_snapshot_id": None,
                "error": None,
            }
            db_name = _tenant_value(tenant, "db_name")
            if not db_name:
                detail["error"] = "tenant has no database name"
                family_ok = False
            else:
                try:
                    summary = restic_backup_stdin_command(
                        "{}.sql".format(domain),
                        mariadb_dump_argv(db_name),
                        ["db", "site:{}".format(domain)],
                    ) or {}
                    detail["ok"] = True
                    detail["data_added"] = summary.get("data_added", 0)
                    detail["snapshot_id"] = summary.get("snapshot_id")
                except Exception as error:
                    detail["error"] = str(error)
                    family_ok = False
            detail["duration"] = time.monotonic() - tenant_started
            tenants[domain] = detail
            if not detail["ok"]:
                Log.warn(controller, "DB backup failed for {}: {}".format(
                    domain, detail["error"]))
    except Exception as error:
        family_ok = False
        family_error = str(error)
        Log.warn(controller, "DB backup family failed: {}".format(error))

    finished_at = _utc_now()
    finished_iso = _iso(finished_at)
    for domain, detail in tenants.items():
        if detail.get("ok"):
            detail["last_success"] = finished_iso
            detail["last_success_snapshot_id"] = detail.get("snapshot_id")
            continue
        previous = previous_tenants.get(domain)
        if isinstance(previous, dict):
            detail["last_success"] = previous.get("last_success")
            detail["last_success_snapshot_id"] = previous.get(
                "last_success_snapshot_id")
    payload = {
        "started": _iso(started_at),
        "finished": finished_iso,
        "duration": time.monotonic() - started,
        "ok": family_ok,
        "tenants": tenants,
    }
    if family_error:
        payload["error"] = family_error
    try:
        record_run("db", payload)
    except Exception as error:
        Log.warn(controller, "Could not record DB backup state: {}".format(error))
    if cron:
        ping_deadman(config.get("db_ping_url", ""), "" if family_ok else "fail")
    return {"ok": family_ok, "payload": payload, "error": family_error}


def _existing_path(path):
    return bool(path) and os.path.exists(path)


def _files_paths(controller, tenants):
    config = MTFunctions.load_config(controller) or {}
    shared_root = config.get("shared_root", "/var/www/shared")
    paths = []
    seen = set()

    def add(path):
        if path and path not in seen:
            seen.add(path)
            paths.append(path)

    def add_existing_global(path):
        if _existing_path(path):
            add(path)
        else:
            Log.warn(controller, "Backup path does not exist; skipping {}".format(path))

    for tenant in tenants:
        domain = _tenant_value(tenant, "domain")
        if not domain:
            continue
        for key, path in (site_file_paths(domain) or {}).items():
            if key == "force_ssl" and not _existing_path(path):
                continue
            if _existing_path(path):
                add(path)

    add_existing_global(os.path.join(shared_root, "config"))
    add_existing_global(os.path.join(shared_root, ".git"))
    add_existing_global(os.path.join(shared_root, "wp-content"))
    add_existing_global("/etc/wo/plugins.d/multitenancy.conf")
    staged = stage_sqlite_copy()
    if not staged:
        raise BackupError("stage_sqlite_copy returned no path")
    if not _existing_path(staged):
        raise BackupError("staged SQLite database copy does not exist: {}".format(staged))
    add(staged)
    if _existing_path("/etc/letsencrypt"):
        add("/etc/letsencrypt")

    excludes = list(GATE_EXCLUDES)
    excludes.extend([
        "/var/www/*/htdocs/wp-content/cache",
        "/var/www/*/htdocs/wp-content/cache/**",
        "/var/www/*/htdocs/wp-content/upgrade",
        "/var/www/*/htdocs/wp-content/upgrade/**",
        os.path.join(shared_root, "wp-content/cache"),
        os.path.join(shared_root, "wp-content/cache/**"),
        os.path.join(shared_root, "wp-content/upgrade"),
        os.path.join(shared_root, "wp-content/upgrade/**"),
        os.path.join(shared_root, "backups"),
        os.path.join(shared_root, "backups/**"),
    ])
    return paths, excludes


def _run_files(controller, config, cron=False):
    started_at = _utc_now()
    started = time.monotonic()
    if cron:
        ping_deadman(config.get("files_ping_url", ""), "start")
    payload = {
        "started": _iso(started_at),
        "finished": None,
        "duration": 0,
        "ok": False,
        "data_added": 0,
        "snapshot_id": None,
    }
    family_error = None
    try:
        tenants = enumerate_enabled_tenants(controller)
        paths, excludes = _files_paths(controller, tenants)
        summary = restic_backup_paths(paths, ["files"], excludes) or {}
        payload["data_added"] = summary.get("data_added", 0)
        payload["snapshot_id"] = summary.get("snapshot_id")
        retention = _run_retention(controller, config)
        if not retention.get("ok", False):
            family_error = "daily retention failed"
        else:
            payload["ok"] = True
    except Exception as error:
        family_error = str(error)
        Log.warn(controller, "Files backup family failed: {}".format(error))
    payload["finished"] = _iso(_utc_now())
    payload["duration"] = time.monotonic() - started
    if family_error:
        payload["error"] = family_error
    try:
        record_run("files", payload)
    except Exception as error:
        Log.warn(controller, "Could not record files backup state: {}".format(error))
    if cron:
        ping_deadman(config.get("files_ping_url", ""), "" if payload["ok"] else "fail")
    return {"ok": payload["ok"], "payload": payload, "error": family_error}


def _tracked_tenant_domains():
    if MultitenancySite is None or db_session is None:
        raise BackupError("multi-tenancy database modules are unavailable")
    try:
        rows = db_session.query(MultitenancySite).all()
    except Exception as error:
        try:
            db_session.rollback()
        except Exception:
            pass
        raise BackupError(
            "unable to enumerate tracked tenants: {}".format(error)) from error
    return {
        _tenant_value(row, "domain")
        for row in rows
        if _tenant_value(row, "domain")
    }

def _run_retention(controller, config):
    started = time.monotonic()
    started_at = _utc_now()
    notes = []
    ok = True
    now = _utc_now()
    try:
        db_flags = _keep_flags(controller, config, "keep_db")
        files_flags = _keep_flags(controller, config, "keep_files")
        run_restic([
            "forget", "--group-by", "host,tags", "--tag", "db",
        ] + db_flags)
        run_restic([
            "forget", "--group-by", "host,tags", "--tag", "files",
        ] + files_flags)

        pre_restore = list_snapshots(tags=["pre-restore"])
        pre_ids = _select_pre_restore_forgets(pre_restore, keep=5)
        abandoned_ids = _select_abandoned_fleet(pre_restore, now, grace_days=7)
        abandoned_ops = _abandoned_fleet_operations(pre_restore, now, grace_days=7)
        if abandoned_ops:
            Log.warn(controller, "Abandoned fleet captures: {}".format(
                ", ".join(abandoned_ops)))
            notes.append("abandoned fleet operations: {}".format(
                ", ".join(abandoned_ops)))
        safety_ids = sorted(set(pre_ids + abandoned_ids))
        if safety_ids:
            forget_snapshot_ids(safety_ids)

        tracked_domains = _tracked_tenant_domains()
        tombstones = read_tombstones()
        tombstone_domains = {
            item.get("domain") for item in (tombstones or ())
            if isinstance(item, dict) and item.get("domain")
        }
        grace_days = _grace_days(controller, config)
        for tombstone in tombstones or ():
            domain = tombstone.get("domain") if isinstance(tombstone, dict) else None
            if not domain:
                continue
            if domain in tracked_domains:
                remove_tombstone(domain)
                tombstone_domains.discard(domain)
                Log.warn(controller, "Retaining snapshots for recreated live tenant {}; removed stale tombstone.".format(domain))
                continue
            deleted_at = _as_datetime(tombstone.get("deleted_at"))
            if deleted_at is None or now - deleted_at <= timedelta(days=grace_days):
                continue
            try:
                site_snapshots = list_snapshots(tags=["site:{}".format(domain)])
                ids = [
                    snapshot_id for snapshot in site_snapshots or ()
                    if "fleet" not in _snapshot_tags(snapshot)
                    for snapshot_id in [_snapshot_id(snapshot)]
                    if snapshot_id
                ]
                if ids:
                    forget_snapshot_ids(sorted(set(ids)))
                remove_tombstone(domain)
                tombstone_domains.discard(domain)
                notes.append("removed tombstone {}".format(domain))
            except Exception as error:
                ok = False
                Log.warn(controller, "Could not sweep tombstone {}: {}; retry tomorrow.".format(
                    domain, error))
                notes.append("tombstone {} failed".format(domain))

        all_snapshots = list_snapshots()
        orphan_domains = set()
        for snapshot in all_snapshots or ():
            if "fleet" in _snapshot_tags(snapshot):
                continue
            for tag in _snapshot_tags(snapshot):
                if tag.startswith("site:"):
                    domain = tag.split(":", 1)[1]
                    if domain not in tracked_domains and domain not in tombstone_domains:
                        orphan_domains.add(domain)
        state = load_state() or {}
        if not isinstance(state, dict):
            state = {}
        anomalies = state.get("anomalies")
        if not isinstance(anomalies, dict):
            anomalies = {}
            state["anomalies"] = anomalies
        anomalies["orphan_sites"] = sorted(orphan_domains)
        save_state(state)
        if orphan_domains:
            notes.append("orphan site tags: {}".format(
                ", ".join(sorted(orphan_domains))))
    except Exception as error:
        ok = False
        notes.append(str(error))
        Log.warn(controller, "Retention failed: {}".format(error))

    payload = {
        "started": _iso(started_at),
        "finished": _iso(_utc_now()),
        "duration": time.monotonic() - started,
        "ok": ok,
        "notes": notes,
    }
    try:
        record_run("retention", payload)
    except Exception as error:
        Log.warn(controller, "Could not record retention state: {}".format(error))
    return {"ok": ok, "payload": payload, "notes": notes}


def _run_selected(controller, config, families, cron=False):
    results = {}
    if "db" in families:
        results["db"] = _run_db(controller, config, cron=cron)
    if "files" in families:
        results["files"] = _run_files(controller, config, cron=cron)
    return results


def _close_failed(controller, message):
    Log.error(controller, message)
    try:
        controller.app.close(1)
    except Exception:
        pass


def cmd_init(controller):
    """Install restic, configure the repository, and run the first backup."""
    try:
        ensure_backup_dirs()
        _install_restic(controller)
        _values, password = _credentials(controller)
        if password:
            _print_password_warning(controller, password=password)
        _restic_init(controller)
        config = _load_config_or_error(controller)
        write_backup_cron(controller)

        try:
            tenants = enumerate_enabled_tenants(controller)
        except Exception as error:
            raise BackupError("cannot determine fleet for first run: {}".format(error))
        if not tenants:
            Log.info(controller, "Fleet is empty; skipping the initial end-to-end backup run.")
        else:
            try:
                with operation_lock("backup run", blocking=False):
                    results = _run_selected(controller, config, ["db", "files"], cron=False)
            except OperationLockBusy as error:
                raise BackupError("backup run is already in progress ({})".format(
                    getattr(error, "holder", "unknown")))
            if any(not result.get("ok", False) for result in results.values()):
                raise BackupError("initial end-to-end backup run failed")
            Log.info(controller, "Initial DB and files backups completed successfully.")
        Log.info(controller, "Backup initialization complete.")
        _print_password_warning(controller)
    except Exception as error:
        _close_failed(controller, "Backup initialization failed: {}".format(error))


def cmd_run(controller):
    """Run the requested DB/files backup families under the fleet lock."""
    pargs = getattr(controller.app, "pargs", None)
    cron = bool(getattr(pargs, "cron", False))
    config = _load_config_or_error(controller)
    if not backup_is_configured():
        _close_failed(controller, "Backups are not configured; run `wo multitenancy backup init` first.")
        return
    if not _config_enabled(config) and cron:
        Log.info(controller, "Backups are disabled; skipping scheduled run.")
        return
    if not _config_enabled(config) and not cron:
        _close_failed(controller, "Backups are disabled in the [backup] configuration.")
        return

    families = []
    if bool(getattr(pargs, "all", False) or getattr(pargs, "all_flag", False)):
        families = ["db", "files"]
    else:
        if bool(getattr(pargs, "db", False) or getattr(pargs, "db_flag", False)):
            families.append("db")
        if bool(getattr(pargs, "files", False) or getattr(pargs, "files_flag", False)):
            families.append("files")
        if not families:
            families = ["db", "files"]

    try:
        with operation_lock("backup run", blocking=False):
            results = _run_selected(controller, config, families, cron=cron)
    except OperationLockBusy as error:
        holder = getattr(error, "holder", "another operation")
        if cron:
            Log.info(controller, "Backup run skipped; {} is holding the operation lock.".format(holder))
            return
        _close_failed(controller, "Backup run is already in progress ({})".format(holder))
        return
    except Exception as error:
        _close_failed(controller, "Backup run failed: {}".format(error))
        return

    failed = [family for family, result in results.items()
              if not result.get("ok", False)]
    if failed:
        _close_failed(controller, "Backup family failure: {}".format(", ".join(failed)))
    else:
        Log.info(controller, "Backup run completed successfully: {}.".format(
            ", ".join(families)))


def cmd_prune(controller):
    """Apply family retention policies and prune unreferenced repository data."""
    pargs = getattr(controller.app, "pargs", None)
    cron = bool(getattr(pargs, "cron", False))
    if not backup_is_configured():
        _close_failed(controller, "Backups are not configured; run `wo multitenancy backup init` first.")
        return
    config = _load_config_or_error(controller)
    started_at = _utc_now()
    started = time.monotonic()
    if cron:
        ping_deadman(config.get("prune_ping_url", ""), "start")
    notes = []
    ok = True
    try:
        db_flags = _keep_flags(controller, config, "keep_db")
        files_flags = _keep_flags(controller, config, "keep_files")
        run_restic([
            "forget", "--group-by", "host,tags", "--tag", "db",
        ] + db_flags)
        run_restic([
            "forget", "--group-by", "host,tags", "--tag", "files",
        ] + files_flags)
        run_restic(["prune"])
    except Exception as error:
        ok = False
        notes.append(str(error))
        Log.warn(controller, "Backup prune failed: {}".format(error))
    payload = {
        "started": _iso(started_at),
        "finished": _iso(_utc_now()),
        "duration": time.monotonic() - started,
        "ok": ok,
        "notes": notes,
    }
    try:
        record_run("prune", payload)
    except Exception as error:
        Log.warn(controller, "Could not record prune state: {}".format(error))
    if cron:
        ping_deadman(config.get("prune_ping_url", ""), "" if ok else "fail")
    if not ok:
        _close_failed(controller, "Backup prune failed.")
    else:
        Log.info(controller, "Backup prune completed successfully.")


def cmd_check(controller):
    """Run restic's metadata-only repository check."""
    pargs = getattr(controller.app, "pargs", None)
    cron = bool(getattr(pargs, "cron", False))
    if not backup_is_configured():
        _close_failed(controller, "Backups are not configured; run `wo multitenancy backup init` first.")
        return
    config = _load_config_or_error(controller)
    started_at = _utc_now()
    started = time.monotonic()
    if cron:
        ping_deadman(config.get("check_ping_url", ""), "start")
    notes = []
    ok = True
    try:
        run_restic(["check"])
    except Exception as error:
        ok = False
        notes.append(str(error))
        Log.warn(controller, "Backup repository check failed: {}".format(error))
    payload = {
        "started": _iso(started_at),
        "finished": _iso(_utc_now()),
        "duration": time.monotonic() - started,
        "ok": ok,
        "notes": notes,
    }
    try:
        record_run("check", payload)
    except Exception as error:
        Log.warn(controller, "Could not record check state: {}".format(error))
    if cron:
        ping_deadman(config.get("check_ping_url", ""), "" if ok else "fail")
    if not ok:
        _close_failed(controller, "Backup repository check failed.")
    else:
        Log.info(controller, "Backup repository check completed successfully.")
