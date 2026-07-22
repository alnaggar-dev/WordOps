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


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
