"""Multitenancy fleet-backup helper tests (stdlib unittest, no live stack).

The suite exercises only pure or near-pure retention, restore-resolution,
locking, tombstone, and path helpers. Run with:

    python3 -m unittest tests.cli.41_test_multitenancy_backup -v
"""

import datetime
import multiprocessing
import os
import shutil
import tempfile
import unittest
from unittest import mock

from distro import distro as _distro

# Importing WordOps modules on non-Debian development hosts can run the distro
# setup code in ``wo.core.variables``. Keep the import isolated and restore all
# process-global shims before the tests run.
_copy2 = shutil.copy2
_distro_id = _distro.id
_distro_version = _distro.version
_distro_codename = _distro.codename
shutil.copy2 = lambda *args, **kwargs: None
_distro.id = lambda: "debian"
_distro.version = lambda: "12"
_distro.codename = lambda: "bookworm"

import sys as _sys
import types as _types
try:
    from wo.core.aptget import WOAptGet  # noqa: F401
except Exception:
    _aptget_stub = _types.ModuleType("wo.core.aptget")
    _aptget_stub.WOAptGet = type("WOAptGet", (), {})
    _sys.modules["wo.core.aptget"] = _aptget_stub
try:
    from wo.cli.plugins import multitenancy_backup_functions as backup_functions
    from wo.cli.plugins import multitenancy_backup_run as backup_run
    from wo.cli.plugins import multitenancy_backup_fleet as backup_fleet
finally:
    shutil.copy2 = _copy2
    _distro.id = _distro_id
    _distro.version = _distro_version
    _distro.codename = _distro_codename



def _try_lock_in_child(lock_path, pipe):
    """Attempt the parent's lock in a forked process and report the result."""
    backup_functions.LOCK_FILE = lock_path
    try:
        with backup_functions.operation_lock("child-holder", blocking=False):
            pipe.send(("acquired", "", ""))
    except backup_functions.OperationLockBusy as exc:
        pipe.send(("busy", str(exc), exc.holder))
    except Exception as exc:  # pragma: no cover - makes child failures visible
        pipe.send(("error", type(exc).__name__, str(exc)))
    finally:
        pipe.close()


class MultitenancyBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def _snapshot(snapshot_id, operation, when, *tags):
        return {
            "id": snapshot_id,
            "time": when,
            "tags": list(tags) + ["operation:" + operation],
        }

    def test_parse_keep_policy(self):
        self.assertEqual(
            backup_functions.parse_keep_policy("24h,7d,4w,3m"),
            [
                "--keep-hourly", "24",
                "--keep-daily", "7",
                "--keep-weekly", "4",
                "--keep-monthly", "3",
            ],
        )
        self.assertEqual(
            backup_functions.parse_keep_policy("7d,4w,6m"),
            ["--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6"],
        )
        for invalid in ("", "24x", "7d,,4w", "0d"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(Exception):
                    backup_functions.parse_keep_policy(invalid)

    def test_select_pre_restore_forgets_old_per_site_operations(self):
        snapshots = [
            self._snapshot(
                "site-op-{}".format(index),
                "site-op-{}".format(index),
                "2026-01-{:02d}T00:00:00Z".format(index),
                "pre-restore", "files", "site:alpha.example",
            )
            for index in range(1, 7)
        ]

        forgotten = backup_run._select_pre_restore_forgets(snapshots, keep=5)

        self.assertEqual(forgotten, ["site-op-1"])

    def test_select_pre_restore_fleet_operation_is_indivisible(self):
        snapshots = [
            self._snapshot(
                "fleet-old", "fleet-old", "2026-01-01T00:00:00Z",
                "pre-restore", "files", "fleet",
            ),
            self._snapshot(
                "fleet-old-member", "fleet-old", "2026-01-02T00:00:00Z",
                "pre-restore", "db", "site:alpha.example",
            ),
        ]
        for index in range(1, 6):
            snapshots.append(
                self._snapshot(
                    "fleet-new-{}".format(index),
                    "fleet-new-{}".format(index),
                    "2026-02-{:02d}T00:00:00Z".format(index),
                    "pre-restore", "files", "fleet",
                )
            )

        forgotten = backup_run._select_pre_restore_forgets(snapshots, keep=5)

        self.assertEqual(forgotten, ["fleet-old", "fleet-old-member"])

    def test_select_pre_restore_collapses_members_into_one_operation(self):
        snapshots = [
            self._snapshot(
                "old-member-a", "old-operation", "2026-01-01T00:00:00Z",
                "pre-restore", "files", "site:alpha.example",
            ),
            self._snapshot(
                "old-member-b", "old-operation", "2026-01-01T00:01:00Z",
                "pre-restore", "db", "site:alpha.example",
            ),
        ]
        for index in range(1, 6):
            snapshots.append(
                self._snapshot(
                    "new-operation-{}".format(index),
                    "new-operation-{}".format(index),
                    "2026-02-{:02d}T00:00:00Z".format(index),
                    "pre-restore", "files", "site:alpha.example",
                )
            )

        forgotten = backup_run._select_pre_restore_forgets(snapshots, keep=5)

        self.assertEqual(forgotten, ["old-member-a", "old-member-b"])

    def test_select_abandoned_fleet_requires_old_manifestless_operation(self):
        now = datetime.datetime(2026, 3, 10, tzinfo=datetime.timezone.utc)
        old = "2026-03-01T00:00:00Z"
        young = "2026-03-08T00:00:00Z"
        snapshots = [
            self._snapshot("old-files", "old-fleet", old, "pre-restore", "files", "fleet"),
            self._snapshot("old-db", "old-fleet", old, "pre-restore", "db", "fleet"),
            self._snapshot(
                "manifest-files", "with-manifest", old,
                "pre-restore", "files", "fleet",
            ),
            self._snapshot(
                "manifest", "with-manifest", old,
                "pre-restore", "manifest", "fleet",
            ),
            self._snapshot("young", "young-fleet", young, "pre-restore", "files", "fleet"),
        ]

        forgotten = backup_run._select_abandoned_fleet(snapshots, now, grace_days=7)

        self.assertEqual(forgotten, ["old-db", "old-files"])

    def test_operation_lock_is_reentrant_and_releases_when_outer_scope_exits(self):
        lock_path = os.path.join(self.tmp, "operation.lock")
        with mock.patch.object(backup_functions, "LOCK_FILE", lock_path):
            with backup_functions.operation_lock("outer-holder", blocking=False):
                self.assertEqual(backup_functions._LOCK_STATE["count"], 1)
                with backup_functions.operation_lock("same-holder", blocking=False):
                    self.assertEqual(backup_functions._LOCK_STATE["count"], 2)
                with backup_functions.operation_lock("other-holder", blocking=False):
                    self.assertEqual(backup_functions._LOCK_STATE["count"], 2)
                self.assertEqual(backup_functions._LOCK_STATE["count"], 1)
            self.assertEqual(backup_functions._LOCK_STATE["count"], 0)
            self.assertIsNone(backup_functions._LOCK_STATE["fd"])
            self.assertIsNone(backup_functions._LOCK_STATE["pid"])

    def test_operation_lock_nonblocking_second_process_reports_holder(self):
        lock_path = os.path.join(self.tmp, "operation.lock")
        context = multiprocessing.get_context("fork")
        parent_conn, child_conn = context.Pipe(duplex=False)
        with mock.patch.object(backup_functions, "LOCK_FILE", lock_path):
            with backup_functions.operation_lock("parent-holder", blocking=False):
                child = context.Process(
                    target=_try_lock_in_child,
                    args=(lock_path, child_conn),
                )
                child.start()
                child.join(timeout=5)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
                    self.fail("child lock attempt did not finish")
                self.assertEqual(child.exitcode, 0)
                self.assertTrue(parent_conn.poll(2), "child did not report lock result")
                result = parent_conn.recv()

        self.assertEqual(result[0], "busy")
        self.assertIn("parent-holder", result[1])
        self.assertIn("parent-holder", result[2])

    def test_tombstone_round_trip_permissions_and_utc_timestamp(self):
        tombstone_dir = os.path.join(self.tmp, "tombstones")
        with mock.patch.object(backup_functions, "TOMBSTONE_DIR", tombstone_dir):
            self.assertTrue(backup_functions.write_tombstone("example.com"))
            path = os.path.join(tombstone_dir, "example.com.json")
            self.assertEqual(stat_mode(path), 0o600)
            self.assertEqual(stat_mode(tombstone_dir), 0o700)

            tombstones = backup_functions.read_tombstones()
            self.assertEqual(len(tombstones), 1)
            self.assertEqual(tombstones[0]["domain"], "example.com")
            deleted_at = datetime.datetime.fromisoformat(
                tombstones[0]["deleted_at"].replace("Z", "+00:00")
            )
            self.assertIsNotNone(deleted_at.tzinfo)
            self.assertEqual(deleted_at.utcoffset(), datetime.timedelta(0))

            backup_functions.remove_tombstone("example.com")
            self.assertFalse(os.path.exists(path))
            self.assertEqual(backup_functions.read_tombstones(), [])

    def test_resolve_snapshot_excludes_pre_restore_for_implicit_and_at_selection(self):
        snapshots = [
            {
                "id": "daily-old",
                "time": "2026-01-01T00:00:00Z",
                "tags": ["files", "site:example.com"],
            },
            {
                "id": "daily-new",
                "time": "2026-01-03T00:00:00Z",
                "tags": ["files", "site:example.com"],
            },
            {
                "id": "pre-restore-newest",
                "time": "2026-01-04T00:00:00Z",
                "tags": ["pre-restore", "files", "site:example.com"],
            },
        ]
        with mock.patch.object(
            backup_functions, "list_snapshots", return_value=snapshots
        ) as listed:
            implicit = backup_functions.resolve_snapshot("files", domain="example.com")
            at = backup_functions.resolve_snapshot(
                "files", domain="example.com", at="2026-01-05"
            )
            explicit = backup_functions.resolve_snapshot(
                "files", domain="example.com", snapshot_id="pre-restore-newest"
            )

        self.assertEqual(implicit["id"], "daily-new")
        self.assertEqual(at["id"], "daily-new")
        self.assertEqual(explicit["id"], "pre-restore-newest")
        self.assertEqual(listed.call_count, 3)

    def test_site_file_paths_include_optional_force_ssl_path(self):
        self.assertEqual(
            backup_functions.site_file_paths("example.com"),
            {
                "uploads": "/var/www/example.com/htdocs/wp-content/uploads",
                "wp_config": "/var/www/example.com/htdocs/wp-config.php",
                "conf_nginx": "/var/www/example.com/conf/nginx",
                "vhost": "/etc/nginx/sites-available/example.com",
                "force_ssl": "/etc/nginx/conf.d/force-ssl-example.com.conf",
            },
        )


class ContentBackupTests(unittest.TestCase):
    release_name = "20261025-120000-abcdef0"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(self.shared, "config"))
        self.sqlite = os.path.join(self.tmp, "dbase.db")
        self._write(self.sqlite, "sqlite")

    @staticmethod
    def _write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)

    def _layout(self, root, released):
        if released:
            release = os.path.join(root, "content", self.release_name)
            self._write(os.path.join(release, "wp-content", "plugin.php"), "snapshot")
            self._write(os.path.join(release, "baseline.json"), "{}")
            self._write(os.path.join(release, "RELEASE"), "{}")
            os.symlink(self.release_name, os.path.join(root, "content", "current"))
            os.symlink("content/current/wp-content", os.path.join(root, "wp-content"))
            os.makedirs(os.path.join(root, "config"), exist_ok=True)
            os.symlink("../content/current/baseline.json",
                       os.path.join(root, "config", "baseline.json"))
            return release
        self._write(os.path.join(root, "wp-content", "plugin.php"), "snapshot")
        self._write(os.path.join(root, "config", "baseline.json"), "{}")
        return os.path.join(root, "wp-content")

    def _files_paths(self):
        with mock.patch.object(backup_run.MTFunctions, "load_config",
                               return_value={"shared_root": self.shared}), \
                mock.patch.object(backup_run, "stage_sqlite_copy", return_value=self.sqlite):
            return backup_run._files_paths(mock.Mock(), [])

    def test_files_paths_include_resolved_release_and_identity(self):
        release = self._layout(self.shared, True)
        paths, excludes = self._files_paths()
        self.assertIn(release, paths)
        self.assertIn(os.path.join(self.shared, "content", "current"), paths)
        self.assertNotIn(os.path.join(self.shared, "wp-content"), paths)
        self.assertNotIn(os.path.join(self.shared, ".git"), paths)
        self.assertIn(os.path.join(release, "wp-content", "cache", "**"), excludes)

    def test_files_paths_keep_old_real_folder_without_git(self):
        folder = self._layout(self.shared, False)
        os.makedirs(os.path.join(self.shared, ".git"))
        paths, _ = self._files_paths()
        self.assertIn(folder, paths)
        self.assertNotIn(os.path.join(self.shared, ".git"), paths)

    def test_files_paths_reject_missing_live_release(self):
        self._layout(self.shared, True)
        shutil.rmtree(os.path.join(self.shared, "content", self.release_name))
        with self.assertRaises(backup_functions.BackupError):
            self._files_paths()

    def test_files_snapshot_records_release_tag_and_state(self):
        release = self._layout(self.shared, True)
        with mock.patch.object(backup_run.MTFunctions, "load_config",
                               return_value={"shared_root": self.shared}), \
                mock.patch.object(backup_run, "enumerate_enabled_tenants", return_value=[]), \
                mock.patch.object(backup_run, "_files_paths", return_value=([release], [])), \
                mock.patch.object(backup_run, "restic_backup_paths",
                                  return_value={"snapshot_id": "snapshot"}) as backup, \
                mock.patch.object(backup_run, "_run_retention", return_value={"ok": True}), \
                mock.patch.object(backup_run, "record_run"):
            result = backup_run._run_files(mock.Mock(), {})
        self.assertTrue(result["ok"])
        self.assertIn("content-release:" + self.release_name, backup.call_args.args[1])
        self.assertEqual(result["payload"]["content_release"], self.release_name)

    def test_fleet_safety_captures_release_not_git(self):
        release = self._layout(self.shared, True)
        os.makedirs(os.path.join(self.shared, ".git"))
        with mock.patch.object(backup_fleet, "stage_sqlite_copy", return_value=self.sqlite), \
                mock.patch.object(backup_fleet, "restic_backup_paths",
                                  return_value={"snapshot_id": "global"}) as backup, \
                mock.patch.object(backup_fleet, "restic_backup_stdin_command",
                                  return_value={"snapshot_id": "manifest"}):
            backup_fleet._capture_safety(
                mock.Mock(), {}, "operation", self.tmp, {"shared_root": self.shared})
        self.assertIn(release, backup.call_args.args[0])
        self.assertIn(os.path.join(self.shared, "content", "current"), backup.call_args.args[0])
        self.assertNotIn(os.path.join(self.shared, ".git"), backup.call_args.args[0])
        self.assertIn("content-release:" + self.release_name, backup.call_args.args[1])

    def test_fleet_restore_rejects_missing_content_without_removing_live_folder(self):
        self._layout(self.shared, False)
        staged = tempfile.mkdtemp(dir=self.tmp)
        with mock.patch.object(backup_fleet, "run_restic"):
            with self.assertRaises(backup_functions.BackupError):
                backup_fleet._restore_global_paths(
                    "snapshot", staged, {"shared_root": self.shared})
        self.assertTrue(os.path.isfile(
            os.path.join(self.shared, "wp-content", "plugin.php")))

    def test_directory_replacement_recovers_original_after_install_failure(self):
        destination = self._layout(self.shared, False)
        prepared = os.path.join(self.tmp, "prepared")
        self._write(os.path.join(prepared, "replacement.php"), "replacement")
        replace = os.replace

        def fail_install(source, target):
            if source == prepared:
                raise OSError("install failed")
            replace(source, target)

        with mock.patch.object(backup_fleet.os, "replace", side_effect=fail_install):
            with self.assertRaisesRegex(OSError, "install failed"):
                backup_fleet._replace_global_entry(prepared, destination)
        self.assertTrue(os.path.isfile(os.path.join(destination, "plugin.php")))
        self.assertTrue(os.path.isdir(prepared))

    @staticmethod
    def _copy_directory(source, destination, **kwargs):
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)

    def test_fleet_restore_recreates_each_snapshot_layout(self):
        for live_released in (False, True):
            for snapshot_released in (False, True):
                with self.subTest(live=live_released, snapshot=snapshot_released):
                    if os.path.exists(self.shared):
                        shutil.rmtree(self.shared)
                    self._layout(self.shared, live_released)
                    staged = tempfile.mkdtemp(dir=self.tmp)
                    snapshot_root = os.path.join(staged, self.shared.lstrip("/"))
                    self._layout(snapshot_root, snapshot_released)
                    # Shared files are real temporary files. Other globals are absent.
                    with mock.patch.object(backup_fleet, "run_restic") as restore, \
                            mock.patch.object(backup_fleet, "_rsync_directory",
                                              side_effect=self._copy_directory), \
                            mock.patch.object(backup_fleet, "_lexists",
                                              side_effect=lambda path: path.startswith(self.tmp)
                                              and os.path.lexists(path)), \
                            mock.patch.object(backup_fleet, "_remove_path",
                                              wraps=backup_fleet._remove_path) as remove:
                        backup_fleet._restore_global_paths(
                            "snapshot", staged, {"shared_root": self.shared})
                    self.assertTrue(os.path.isfile(
                        os.path.join(self.shared, "wp-content", "plugin.php")))
                    self.assertEqual(os.path.islink(
                        os.path.join(self.shared, "wp-content")), snapshot_released)
                    self.assertEqual(os.path.islink(
                        os.path.join(self.shared, "config", "baseline.json")), snapshot_released)
                    if snapshot_released:
                        self.assertEqual(os.readlink(
                            os.path.join(self.shared, "content", "current")), self.release_name)
                    self.assertNotIn(os.path.join(self.shared, ".git"),
                                     restore.call_args.args[0])
                    for call in remove.call_args_list:
                        self.assertTrue(call.args[0].startswith(self.tmp))


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
