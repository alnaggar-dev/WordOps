"""Content deployment tests using temporary layouts, never a live stack."""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from distro import distro as _distro

# Match the neighboring fleet tests' non-Debian import isolation.
with mock.patch.object(shutil, 'copy2'), \
        mock.patch.object(_distro, 'id', return_value='debian'), \
        mock.patch.object(_distro, 'version', return_value='12'), \
        mock.patch.object(_distro, 'codename', return_value='bookworm'):
    try:
        from wo.core.aptget import WOAptGet  # noqa: F401
    except Exception:
        _aptget_stub = types.ModuleType('wo.core.aptget')
        _aptget_stub.WOAptGet = type('WOAptGet', (), {})
        sys.modules['wo.core.aptget'] = _aptget_stub
    from wo.cli.plugins import multitenancy_deploy as deploy


class ContentDeploymentTests(unittest.TestCase):
    old = '20261024-120000-aaaaaaa'
    new = '20261025-120000-bbbbbbb'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = os.path.realpath(directory.name)
        self.controller = mock.Mock()
        self.patch(deploy.MTFunctions, 'load_config', return_value={'shared_root': self.root})
        self.patch(deploy.MTDatabase, 'get_shared_sites', return_value=[{
            'domain': 'example.test',
            'site_path': '/var/www/example.test',
            'php_version': '8.4',
        }])
        self.patch(deploy, 'Log')
        self.run = self.patch(
            deploy.subprocess, 'run',
            return_value=types.SimpleNamespace(returncode=0, stdout='', stderr=''),
        )
        self.operation = deploy.ContentDeployment(self.controller)
        self.events = mock.Mock()
        for name in ('_maintenance', '_drain', '_restore_point', '_prepare',
                     '_log_mark', '_reset_caches', '_database_updates', '_apply',
                     '_bypass', '_checks', '_release_maintenance', '_unlock',
                     '_recovery_message'):
            method = self.patch(self.operation, name)
            self.events.attach_mock(method, name)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    @staticmethod
    def write_json(path, value):
        with open(path, 'w') as handle:
            json.dump(value, handle)

    def release(self, name, plugins=None, themes=None, baseline=None):
        path = os.path.join(self.root, 'content', name)
        for folder in ('plugins', 'themes', 'mu-plugins'):
            os.makedirs(os.path.join(path, 'wp-content', folder))
        self.write_json(os.path.join(path, 'RELEASE'), {
            'plugins': plugins or {'woocommerce': '10.0'},
            'themes': themes or {'storefront': '4.6'},
        })
        self.write_json(os.path.join(path, 'baseline.json'), baseline or {'options': {}})
        return path

    def layout(self):
        old = self.release(self.old)
        new = self.release(self.new)
        os.makedirs(os.path.join(self.root, 'config'))
        os.symlink(self.old, deploy.current_link(self.root))
        os.symlink('content/current/wp-content', os.path.join(self.root, 'wp-content'))
        os.symlink('../content/current/baseline.json', os.path.join(self.root, 'config', 'baseline.json'))
        return old, new

    def records(self):
        with open(os.path.join(self.root, 'content', deploy.HISTORY_FILE)) as handle:
            return [json.loads(line) for line in handle]

    def assert_live(self, name):
        self.assertEqual(os.readlink(deploy.current_link(self.root)), name)

    def test_default_deploy_is_safe_and_orders_maintenance_drain_restore(self):
        old, new = self.layout()
        self.operation.deploy(self.new)
        self.assert_live(self.new)
        self.assertEqual(self.events.mock_calls, [
            mock.call._maintenance(), mock.call._drain(), mock.call._restore_point(),
            mock.call._prepare(new), mock.call._log_mark(), mock.call._reset_caches(),
            mock.call._database_updates(old, new), mock.call._apply(old, new),
            mock.call._bypass(True), mock.call._checks(loopback=True),
            mock.call._bypass(False), mock.call._release_maintenance(), mock.call._unlock(),
        ])
        record = self.records()[-1]
        self.assertEqual((record['release'], record['previous'], record['path'], record['result']),
                         (self.new, self.old, 'safe', 'pass'))

    def test_explicit_code_only_skips_maintenance_and_database_changes(self):
        self.layout()
        self.operation.deploy(self.new, code_only=True)
        self.assert_live(self.new)
        for method in ('_maintenance', '_drain', '_database_updates', '_apply', '_bypass', '_release_maintenance'):
            getattr(self.operation, method).assert_not_called()
        self.operation._restore_point.assert_called_once_with()
        self.operation._checks.assert_called_once_with(loopback=False)
        self.assertEqual(self.records()[-1]['path'], 'code-only')

    def test_drain_timeout_never_switches_and_releases_maintenance(self):
        self.layout()
        self.operation._drain.side_effect = deploy.DeployError('drain timeout')
        with self.assertRaisesRegex(deploy.DeployError, 'drain timeout'):
            self.operation.deploy(self.new)
        self.assert_live(self.old)
        self.operation._restore_point.assert_not_called()
        self.operation._prepare.assert_not_called()
        self.operation._release_maintenance.assert_called_once_with()
        self.operation._unlock.assert_called_once_with()
        self.assertEqual(self.records()[-1]['result'], 'aborted')

    def test_safe_failed_checks_leave_new_release_in_maintenance_and_record_restore(self):
        self.layout()
        restore = {'dumps': '/local/dumps', 'command': 'restore local dumps'}
        self.operation.restore = restore
        self.operation._checks.side_effect = deploy.DeployError('unhealthy')
        with self.assertRaisesRegex(deploy.DeployError, 'unhealthy'):
            self.operation.deploy(self.new)
        self.assert_live(self.new)
        self.operation._release_maintenance.assert_not_called()
        self.assertEqual(self.operation._bypass.call_args_list, [mock.call(True), mock.call(False)])
        self.operation._recovery_message.assert_called_once_with()
        self.operation._unlock.assert_called_once_with()
        record = self.records()[-1]
        self.assertEqual(record['result'], 'fail')
        self.assertEqual(record['restore_point'], restore)
        self.assertEqual(record['previous'], self.old)

    def test_code_only_failed_checks_restore_previous_and_check_it(self):
        self.layout()
        checked = []

        def checks(**kwargs):
            checked.append((os.readlink(deploy.current_link(self.root)), kwargs))
            if len(checked) == 1:
                raise deploy.DeployError('new release unhealthy')

        self.operation._checks.side_effect = checks
        with self.assertRaisesRegex(deploy.DeployError, 'new release unhealthy'):
            self.operation.deploy(self.new, code_only=True)
        self.assert_live(self.old)
        self.assertEqual(checked, [(self.new, {'loopback': False}), (self.old, {})])
        self.assertEqual(self.operation._reset_caches.call_count, 2)
        self.operation._maintenance.assert_not_called()
        self.operation._unlock.assert_called_once_with()
        self.assertEqual(self.records()[-1]['result'], 'fail')

    def test_code_only_rejects_plugin_theme_and_baseline_changes_before_restore(self):
        old, new = self.layout()
        for field in ('plugins', 'themes', 'baseline'):
            with self.subTest(field=field):
                shutil.copyfile(os.path.join(old, 'RELEASE'), os.path.join(new, 'RELEASE'))
                shutil.copyfile(os.path.join(old, 'baseline.json'), os.path.join(new, 'baseline.json'))
                if field == 'baseline':
                    self.write_json(os.path.join(new, 'baseline.json'), {'changed': True})
                else:
                    with open(os.path.join(new, 'RELEASE')) as handle:
                        info = json.load(handle)
                    info[field]['different'] = '2.0'
                    self.write_json(os.path.join(new, 'RELEASE'), info)
                with self.assertRaisesRegex(deploy.DeployError, 'safe path'):
                    self.operation.deploy(self.new, code_only=True)
                self.assert_live(self.old)
        self.operation._restore_point.assert_not_called()
        self.operation._prepare.assert_not_called()

    def test_already_live_release_is_rejected(self):
        self.layout()
        with self.assertRaisesRegex(deploy.DeployError, 'already live'):
            self.operation.deploy(self.old)
        self.operation._maintenance.assert_not_called()
        self.operation._restore_point.assert_not_called()

    def seed_back(self, path):
        self.layout()
        deploy.switch_current(self.root, self.new)
        self.operation.restore = {
            'command': 'restore databases', 'dumps': '/local/dumps',
            'safe': path == 'safe',
        }
        self.operation._history(self.new, self.old, path, 'pass')

    def test_safe_back_refuses_without_force(self):
        self.seed_back('safe')
        with self.assertRaisesRegex(deploy.DeployError, '--back --force'):
            self.operation.back()
        self.assert_live(self.new)
        self.operation._maintenance.assert_not_called()
        self.operation._reset_caches.assert_not_called()
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(
            (records[-1]['release'], records[-1]['previous'],
             records[-1]['path'], records[-1]['result']),
            (self.old, self.new, 'back', 'aborted'),
        )
        self.assertTrue(records[-1]['restore_point']['safe'])

    def test_forced_safe_back_drains_and_checks_but_retains_maintenance(self):
        self.seed_back('safe')
        self.operation.back(force=True)
        self.assert_live(self.old)
        self.operation._maintenance.assert_called_once_with()
        self.operation._drain.assert_called_once_with()
        self.operation._checks.assert_called_once_with(loopback=True)
        self.assertEqual(self.operation._bypass.call_args_list,
                         [mock.call(True), mock.call(False)])
        self.operation._release_maintenance.assert_not_called()
        self.operation._unlock.assert_called_once_with()
        self.assertEqual(self.records()[-1]['path'], 'back')
        self.assertEqual(self.records()[-1]['result'], 'pass')

    def test_code_only_back_needs_no_force_or_maintenance(self):
        self.seed_back('code-only')
        self.operation.back()
        self.assert_live(self.old)
        self.operation._maintenance.assert_not_called()
        self.operation._drain.assert_not_called()
        self.operation._checks.assert_called_once_with(loopback=False)

    def test_failed_forced_safe_back_retains_maintenance(self):
        self.seed_back('safe')
        self.operation._checks.side_effect = deploy.DeployError('back unhealthy')
        with self.assertRaisesRegex(deploy.DeployError, 'back unhealthy'):
            self.operation.back(force=True)
        self.assert_live(self.old)
        self.operation._release_maintenance.assert_not_called()
        self.operation._recovery_message.assert_called_once_with()
        self.assertEqual(self.records()[-1]['result'], 'fail')

    def test_prepare_breaks_logger_hardlink_before_rendering(self):
        old, new = self.layout()
        relative = os.path.join('wp-content', 'mu-plugins', 'wo-login-log.php')
        live_logger, staged_logger = os.path.join(old, relative), os.path.join(new, relative)
        with open(live_logger, 'w') as handle:
            handle.write('live logger')
        os.link(live_logger, staged_logger)

        def install(controller, release):
            self.assertFalse(os.path.lexists(staged_logger))
            with open(os.path.join(release, relative), 'w') as handle:
                handle.write('new logger')
            return True

        installer = self.patch(deploy.MTFunctions, 'install_login_failure_logger', side_effect=install)
        run = self.patch(deploy.subprocess, 'run')
        deploy.ContentDeployment._prepare(self.operation, new)
        installer.assert_called_once_with(self.controller, new)
        with open(live_logger) as handle:
            self.assertEqual(handle.read(), 'live logger')
        with open(staged_logger) as handle:
            self.assertEqual(handle.read(), 'new logger')
        self.assertNotEqual(os.stat(live_logger).st_ino, os.stat(staged_logger).st_ino)
        run.assert_called_once_with(['chown', '-R', 'www-data:www-data', new], check=True)

    def legacy_layout(self):
        content = os.path.join(self.root, 'wp-content')
        for folder in ('plugins', 'themes'):
            os.makedirs(os.path.join(content, folder))
        with open(os.path.join(content, 'plugins', 'sample.php'), 'w') as handle:
            handle.write('<?php\n/* Plugin Name: Sample\nVersion: 1.2\n*/')
        os.makedirs(os.path.join(content, 'themes', 'sample'))
        with open(os.path.join(content, 'themes', 'sample', 'style.css'), 'w') as handle:
            handle.write('/* Theme Name: Sample\nVersion: 3.4\n*/')
        os.makedirs(os.path.join(self.root, 'config'))
        self.write_json(os.path.join(self.root, 'config', 'baseline.json'), {'legacy': True})
        return content

    def test_cutover_moves_content_and_installs_fixed_relative_links(self):
        content = self.legacy_layout()
        inode = os.stat(content).st_ino
        self.operation.cutover()
        release = deploy.current_release(self.root)
        self.assertEqual(os.stat(os.path.join(release, 'wp-content')).st_ino, inode)
        self.assertEqual(os.readlink(content), 'content/current/wp-content')
        self.assertEqual(os.readlink(os.path.join(self.root, 'config', 'baseline.json')),
                         '../content/current/baseline.json')
        self.assertFalse(os.path.isabs(os.readlink(deploy.current_link(self.root))))
        info = deploy.read_release_info(release)
        self.assertEqual(info['plugins'], {'sample': '1.2'})
        self.assertEqual(info['themes'], {'sample': '3.4'})
        self.assertEqual(info['commit'], 'pre-cutover')
        self.assertEqual(deploy.validate_release(self.root, os.path.basename(release)), release)
        self.assertEqual(self.records()[-1]['result'], 'pass')

    def test_cutover_refuses_existing_cutover_without_side_effects(self):
        self.layout()
        with self.assertRaisesRegex(deploy.DeployError, 'already cut over'):
            self.operation.cutover()
        self.assert_live(self.old)
        self.assertEqual(self.events.mock_calls, [])

    def test_cutover_failed_checks_undo_moves_and_links(self):
        content = self.legacy_layout()
        inode = os.stat(content).st_ino
        self.operation._checks.side_effect = deploy.DeployError('cutover unhealthy')
        with self.assertRaisesRegex(deploy.DeployError, 'cutover unhealthy'):
            self.operation.cutover()
        self.assertFalse(os.path.islink(content))
        self.assertEqual(os.stat(content).st_ino, inode)
        baseline = os.path.join(self.root, 'config', 'baseline.json')
        self.assertFalse(os.path.islink(baseline))
        with open(baseline) as handle:
            self.assertEqual(json.load(handle), {'legacy': True})
        self.assertFalse(os.path.lexists(deploy.current_link(self.root)))
        self.assertEqual(deploy.list_releases(self.root), [])
        self.assertEqual(self.operation._reset_caches.call_count, 2)
        self.assertEqual(self.records()[-1]['result'], 'fail')

    def test_prune_retains_live_previous_and_newest_releases(self):
        names = ['202610{:02d}-120000-aaaaaaa'.format(day) for day in range(1, 11)]
        for name in names:
            self.release(name)
        os.symlink(names[0], deploy.current_link(self.root))
        unrelated = os.path.join(self.root, 'content', 'operator-notes')
        os.makedirs(unrelated)
        deploy.prune_releases(self.root, names[1])
        expected = set(names[-deploy.KEEP_RELEASES:] + names[:2])
        self.assertEqual(set(deploy.list_releases(self.root)), expected)
        self.assertTrue(os.path.isdir(unrelated))
        self.assert_live(names[0])

    def test_validate_release_accepts_complete_real_layout(self):
        path = self.release(self.new)
        self.assertEqual(deploy.validate_release(self.root, self.new), path)

    def test_validate_release_rejects_invalid_names(self):
        for name in ('', None, '../outside', '/tmp/release', 'current', self.new + '/extra'):
            with self.subTest(name=name):
                with self.assertRaises(deploy.DeployError):
                    deploy.validate_release(self.root, name)

    def test_validate_release_rejects_symlinked_release(self):
        path = self.release(self.old)
        os.symlink(path, os.path.join(self.root, 'content', self.new))
        with self.assertRaisesRegex(deploy.DeployError, 'real folder'):
            deploy.validate_release(self.root, self.new)

    def test_validate_release_requires_each_layout_component(self):
        path = self.release(self.new)
        for relative in ('wp-content/plugins', 'wp-content/themes', 'baseline.json', 'RELEASE'):
            with self.subTest(relative=relative):
                source = os.path.join(path, relative)
                os.rename(source, source + '.saved')
                try:
                    with self.assertRaisesRegex(deploy.DeployError, 'missing'):
                        deploy.validate_release(self.root, self.new)
                finally:
                    os.rename(source + '.saved', source)

    def test_validate_release_rejects_invalid_metadata(self):
        path = self.release(self.new)
        for value in ([], {}, {'plugins': [], 'themes': {}}, {'plugins': {}, 'themes': 'invalid'}):
            with self.subTest(value=value):
                self.write_json(os.path.join(path, 'RELEASE'), value)
                with self.assertRaisesRegex(deploy.DeployError, 'metadata'):
                    deploy.validate_release(self.root, self.new)
        with open(os.path.join(path, 'RELEASE'), 'w') as handle:
            handle.write('{broken')
        with self.assertRaisesRegex(deploy.DeployError, 'metadata'):
            deploy.validate_release(self.root, self.new)

    def test_validate_release_rejects_non_object_or_malformed_baseline(self):
        path = self.release(self.new)
        baseline = os.path.join(path, 'baseline.json')
        self.write_json(baseline, [])
        with self.assertRaisesRegex(deploy.DeployError, 'baseline'):
            deploy.validate_release(self.root, self.new)
        with open(baseline, 'w') as handle:
            handle.write('{broken')
        with self.assertRaises(ValueError):
            deploy.validate_release(self.root, self.new)

    def test_safe_back_drain_failure_keeps_current_and_releases_maintenance(self):
        self.seed_back('safe')
        self.operation._drain.side_effect = deploy.DeployError('drain timeout')
        with self.assertRaisesRegex(deploy.DeployError, 'drain timeout'):
            self.operation.back(force=True)
        self.assert_live(self.new)
        self.operation._reset_caches.assert_not_called()
        self.operation._release_maintenance.assert_called_once_with()
        self.operation._unlock.assert_called_once_with()
        self.assertEqual(self.records()[-1]['result'], 'aborted')

    def test_cutover_partial_move_failure_restores_original_folder(self):
        content = self.legacy_layout()
        original_rename = os.rename
        baseline = os.path.join(self.root, 'config', 'baseline.json')

        def rename(source, target):
            if source == baseline:
                raise OSError('cannot move baseline')
            return original_rename(source, target)

        self.patch(deploy.os, 'rename', side_effect=rename)
        with self.assertRaisesRegex(OSError, 'cannot move baseline'):
            self.operation.cutover()
        self.assertTrue(os.path.isdir(content))
        self.assertFalse(os.path.islink(content))
        self.assertTrue(os.path.isfile(os.path.join(content, 'plugins', 'sample.php')))
        self.assertTrue(os.path.isfile(baseline))
        self.assertFalse(os.path.lexists(deploy.current_link(self.root)))
        self.assertEqual(deploy.list_releases(self.root), [])
        self.operation._release_maintenance.assert_called_once_with()
        self.assertEqual(self.records()[-1]['result'], 'fail')

    def test_deploy_requires_cutover_before_side_effects(self):
        self.legacy_layout()
        self.release(self.new)
        with self.assertRaisesRegex(deploy.DeployError, 'cutover'):
            self.operation.deploy(self.new)
        self.operation._maintenance.assert_not_called()
        self.operation._restore_point.assert_not_called()
        self.assertFalse(os.path.lexists(deploy.current_link(self.root)))

    def test_back_requires_recorded_previous_release(self):
        self.layout()
        with self.assertRaisesRegex(deploy.DeployError, 'No previous'):
            self.operation.back()
        self.assert_live(self.old)
        self.assertEqual(self.events.mock_calls, [])

    def test_repeated_back_preserves_safe_guard_and_ignores_aborted_history(self):
        self.seed_back('safe')
        self.operation.back(force=True)
        self.assert_live(self.old)
        self.assertTrue(self.records()[-1]['restore_point']['safe'])
        self.events.reset_mock()
        with self.assertRaisesRegex(deploy.DeployError, '--back --force'):
            self.operation.back()
        self.assert_live(self.old)
        self.assertEqual(self.events.mock_calls, [])
        self.assertEqual(self.records()[-1]['result'], 'aborted')
        self.operation.back(force=True)
        self.assert_live(self.new)
        self.operation._maintenance.assert_called_once_with()
        self.operation._drain.assert_called_once_with()
        self.assertEqual(self.records()[-1]['result'], 'pass')
        self.assertTrue(self.records()[-1]['restore_point']['safe'])

    def test_previous_ignores_aborted_records_for_live_release(self):
        self.seed_back('code-only')
        self.operation._history(self.new, '20261001-120000-ccccccc', 'safe', 'aborted')
        self.operation.back()
        self.assert_live(self.old)
        self.operation._maintenance.assert_not_called()
        self.assertEqual(self.records()[-1]['result'], 'pass')

    def test_initialization_refuses_empty_fleet_before_any_operation(self):
        deploy.MTDatabase.get_shared_sites.return_value = []
        with self.assertRaises(deploy.DeployError):
            deploy.ContentDeployment(self.controller)
        self.assertEqual(self.events.mock_calls, [])
        self.assertFalse(os.path.exists(deploy.content_root(self.root)))

    def test_cutover_drain_timeout_preserves_original_layout(self):
        content = self.legacy_layout()
        inode = os.stat(content).st_ino
        self.operation._drain.side_effect = deploy.DeployError('cutover drain timeout')
        with self.assertRaisesRegex(deploy.DeployError, 'cutover drain timeout'):
            self.operation.cutover()
        self.assertFalse(os.path.islink(content))
        self.assertEqual(os.stat(content).st_ino, inode)
        baseline = os.path.join(self.root, 'config', 'baseline.json')
        self.assertFalse(os.path.islink(baseline))
        with open(baseline) as handle:
            self.assertEqual(json.load(handle), {'legacy': True})
        self.assertFalse(os.path.lexists(deploy.current_link(self.root)))
        self.assertEqual(deploy.list_releases(self.root), [])
        self.operation._reset_caches.assert_not_called()
        self.operation._release_maintenance.assert_called_once_with()
        self.operation._unlock.assert_called_once_with()

    def test_same_release_version_resumes_outdated_database_migration(self):
        old, new = self.layout()
        wp = self.patch(self.operation, '_wp', return_value='9.9')
        deploy.ContentDeployment._database_updates(self.operation, old, new)
        site = self.operation.sites[0]
        self.assertEqual(wp.call_args_list, [
            mock.call(site, ['option', 'get', 'woocommerce_db_version']),
            mock.call(site, ['wc', 'update', '--yes']),
        ])

    def test_same_release_and_database_version_needs_no_migration(self):
        old, new = self.layout()
        wp = self.patch(self.operation, '_wp', return_value='10.0')
        deploy.ContentDeployment._database_updates(self.operation, old, new)
        wp.assert_called_once_with(
            self.operation.sites[0], ['option', 'get', 'woocommerce_db_version'])

    def test_changed_woocommerce_release_runs_migration(self):
        old, new = self.layout()
        self.write_json(os.path.join(new, 'RELEASE'), {
            'plugins': {'woocommerce': '10.1'}, 'themes': {'storefront': '4.6'},
        })
        wp = self.patch(self.operation, '_wp', return_value='10.1')
        deploy.ContentDeployment._database_updates(self.operation, old, new)
        wp.assert_any_call(self.operation.sites[0], ['wc', 'update', '--yes'])

    def test_apply_retries_failed_safe_release_with_unchanged_baseline(self):
        from wo.cli.plugins.multitenancy import WOMultitenancyController

        old, new = self.layout()
        self.operation._history(self.old, None, 'safe', 'fail')
        self.operation._history(self.old, None, 'safe', 'aborted')
        apply = self.patch(WOMultitenancyController, '_apply_impl')
        deploy.ContentDeployment._apply(self.operation, old, new)
        apply.assert_called_once_with(self.controller)

    def test_apply_skips_unchanged_successful_release(self):
        from wo.cli.plugins.multitenancy import WOMultitenancyController

        old, new = self.layout()
        self.operation._history(self.old, None, 'safe', 'fail')
        self.operation._history(self.old, None, 'safe', 'pass')
        apply = self.patch(WOMultitenancyController, '_apply_impl')
        deploy.ContentDeployment._apply(self.operation, old, new)
        apply.assert_not_called()
