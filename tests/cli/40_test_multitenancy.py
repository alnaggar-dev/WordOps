"""Multitenancy helper unit tests (stdlib unittest + mock, no live stack).

Covers shared-core lifecycle safety, deployed content layout, activation-only
baselines, and tenant helpers. Run with:

    python3 -m unittest tests.cli.40_test_multitenancy -v
"""

import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from distro import distro as _distro

_copy2 = shutil.copy2
_distro_id = _distro.id
_distro_version = _distro.version
_distro_codename = _distro.codename
shutil.copy2 = lambda *args, **kwargs: None
_distro.id = lambda: 'debian'
_distro.version = lambda: '12'
_distro.codename = lambda: 'bookworm'
_mt_import_error = None

# Non-Debian dev hosts (e.g. macOS) lack the ``apt`` / ``sh.apt_get`` layer that
# ``wo.core.aptget`` imports at module load. Without it the controller import
# fails and every controller test is skipped. Install a guarded dummy so the
# real module is still used on Linux/CI (where it imports cleanly).
import sys as _sys
import types as _types
try:
    from wo.core.aptget import WOAptGet  # noqa: F401
except Exception:
    _aptget_stub = _types.ModuleType('wo.core.aptget')
    _aptget_stub.WOAptGet = type('WOAptGet', (), {})
    _sys.modules['wo.core.aptget'] = _aptget_stub
try:
    from wo.cli.plugins.multitenancy_functions import MTFunctions, SharedInfrastructure, BaselineApplicator
    from wo.cli.plugins import multitenancy_functions as mtf
    try:
        from wo.cli.plugins import multitenancy as mt
    except ImportError as exc:
        mt = None
        _mt_import_error = exc
finally:
    shutil.copy2 = _copy2
    _distro.id = _distro_id
    _distro.version = _distro_version
    _distro.codename = _distro_codename



class MultitenancyCacheClearTests(unittest.TestCase):
    """Exercise the real clean dispatch without touching services or caches."""

    def setUp(self):
        from wo.cli.plugins import clean
        self.clean = clean
        self.controller = clean.WOCleanController.__new__(
            clean.WOCleanController)
        self.controller.app = mock.Mock()
        self.controller.app.pargs = _types.SimpleNamespace(
            all=True, fastcgi=False, opcache=False, redis=False, reload=True)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for method in ('info', 'debug', 'warn'):
            stack.enter_context(mock.patch.object(clean.Log, method))
        self.error = stack.enter_context(mock.patch.object(
            clean.Log, 'error', side_effect=SystemExit(1)))
        self.isdir = stack.enter_context(mock.patch.object(
            clean.os.path, 'isdir', return_value=True))
        stack.enter_context(mock.patch.object(
            clean.os.path, 'exists', return_value=True))
        stack.enter_context(mock.patch.object(
            clean.WOAptGet, 'is_installed', return_value=True, create=True))
        stack.enter_context(mock.patch.object(
            clean.WOVar, 'wo_php_versions', {'php84': '8.4'}))
        self.shell = stack.enter_context(mock.patch.object(
            clean.WOShellExec, 'cmd_exec', return_value=True))
        self.restart = stack.enter_context(mock.patch.object(
            clean.WOService, 'restart_service'))
        self.stop = stack.enter_context(mock.patch.object(
            clean.WOService, 'stop_service'))
        self.request = stack.enter_context(mock.patch.object(
            clean.requests, 'get', return_value=mock.Mock(status_code=200)))

    def test_all_caches_purged_with_checked_reload_not_restart(self):
        self.controller.default()
        self.assertEqual(self.shell.call_args_list, [
            mock.call(self.controller, 'rm -rf /var/run/nginx-cache/*'),
            mock.call(self.controller, 'nginx -t && service nginx reload'),
            mock.call(self.controller, 'redis-cli flushall'),
        ])
        self.request.assert_called_once_with(
            'http://127.0.0.1/cache/opcache/php84.php', timeout=5)
        self.restart.assert_not_called()
        self.stop.assert_not_called()

    def test_ordinary_fastcgi_clean_keeps_restart_policy(self):
        self.controller.app.pargs.reload = False
        self.controller.clean_fastcgi()
        self.shell.assert_called_once_with(
            self.controller, 'rm -rf /var/run/nginx-cache/*')
        self.restart.assert_called_once_with(self.controller, 'nginx')

    def test_failed_purge_does_not_reload_or_restart(self):
        self.shell.return_value = False
        with self.assertRaises(SystemExit):
            self.controller.clean_fastcgi()
        self.shell.assert_called_once_with(
            self.controller, 'rm -rf /var/run/nginx-cache/*')
        self.restart.assert_not_called()
        self.stop.assert_not_called()

    def test_failed_nginx_check_or_reload_has_no_restart_fallback(self):
        self.shell.side_effect = [True, False]
        with self.assertRaises(SystemExit):
            self.controller.clean_fastcgi()
        self.assertEqual(self.shell.call_count, 2)
        self.error.assert_called_once_with(
            self.controller, 'Unable to reload NGINX after cache purge')
        self.restart.assert_not_called()
        self.stop.assert_not_called()

    def test_missing_fastcgi_cache_reports_failure_without_service_change(self):
        self.isdir.return_value = False
        with self.assertRaises(SystemExit):
            self.controller.clean_fastcgi()
        self.shell.assert_not_called()
        self.restart.assert_not_called()
        self.stop.assert_not_called()

    def test_shared_cache_helper_opts_into_reload_and_returns_command_result(self):
        for result in (True, False):
            with self.subTest(result=result):
                self.shell.reset_mock()
                self.shell.return_value = result
                self.assertIs(
                    MTFunctions.clear_all_caches(self.controller), result)
                self.shell.assert_called_once_with(
                    self.controller, 'wo clean --all --reload')

    def test_shared_cache_helper_reports_exception_without_retry(self):
        self.shell.side_effect = RuntimeError('cache command failed')
        self.assertFalse(MTFunctions.clear_all_caches(self.controller))
        self.shell.assert_called_once_with(
            self.controller, 'wo clean --all --reload')

    def test_site_cache_helper_opts_into_reload(self):
        with mock.patch.object(mtf.subprocess, 'run') as run:
            MTFunctions.clear_cache(self.controller, 'example.com', 'wpfc')
        self.shell.assert_called_once_with(
            self.controller, 'wo clean --fastcgi --reload')
        run.assert_called_once_with(
            ['wp', 'cache', 'flush', '--allow-root'],
            cwd='/var/www/example.com/htdocs',
            capture_output=True, check=False)

    def test_restore_cache_helper_opts_into_reload_and_warns_on_failure(self):
        from wo.cli.plugins import multitenancy_backup_restore as restore
        self.shell.return_value = False
        with mock.patch.object(restore.Log, 'warn') as warn:
            restore.flush_site_caches(
                self.controller, 'example.com', {'cache_type': 'wpfc'})
        self.shell.assert_called_once_with(
            self.controller, 'wo clean --fastcgi --reload')
        warn.assert_called_once_with(
            self.controller, 'FastCGI cache purge for example.com failed')


class MultitenancyTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write_shared_config(self, content):
        cfg_dir = os.path.join(self.tmp, 'config')
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, 'wp-config-shared.php'), 'w') as fh:
            fh.write(content)


    def _run_create_impl_with_mocks(self, cache_type='wpfc', baseline=None, manager=None,
                                    install_fails=False, nginx_validate_results=None):
        domain = 'example.com'
        shared_root = '/var/www/shared'
        if baseline is None:
            baseline = {'plugins': ['nginx-helper'], 'theme': 't', 'options': {}}
        pargs = mock.Mock()
        pargs.site_name = domain
        pargs.letsencrypt = False
        pargs.admin_user = 'admin'
        pargs.admin_email = 'admin@example.com'
        pargs.wpredis = False
        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        ctrl.app = mock.Mock()
        ctrl.app.pargs = pargs

        with contextlib.ExitStack() as stack:
            log_mocks = {}
            for method in ('info', 'warn', 'error', 'debug'):
                log_mocks[method] = stack.enter_context(
                    mock.patch(f'wo.core.logging.Log.{method}'))
            stack.enter_context(mock.patch.object(mt.WODomain, 'validate', return_value=domain))
            stack.enter_context(mock.patch.object(mt, 'check_domain_exists', return_value=False))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch.object(mt, 'is_cut_over', return_value=True))
            stack.enter_context(mock.patch.object(
                mt, 'current_content_release', return_value='/var/www/shared/content/live',
            ))
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'load_config',
                return_value={'shared_root': shared_root, 'admin_email': 'fallback@example.com'},
            ))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'preflight_shared_config', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_php_version', return_value='8.4'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_cache_type', return_value=cache_type))
            spc_seen = {}

            def _spc_side_effect(app_self, stype):
                spc_seen['wpredis'] = app_self.app.pargs.wpredis

            stack.enter_context(mock.patch.object(
                mt, 'site_package_check', side_effect=_spc_side_effect))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'create_site_directories'))
            stack.enter_context(mock.patch.object(
                mt,
                'setupdatabase',
                return_value={
                    'wo_db_name': 'db',
                    'wo_db_user': 'user',
                    'wo_db_pass': 'pass',
                    'wo_db_host': 'localhost',
                },
            ))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'create_shared_symlinks'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'generate_redis_prefix', return_value='wp_example_'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'allocate_redis_db', return_value=3))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'ensure_redis_databases'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'generate_wp_config'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'generate_nginx_config', return_value='nginx.conf'))
            if install_fails:
                # Leave the real install_wordpress in place and fail its
                # wp-cli subprocess, exercising the helper's exit=False
                # log-then-raise path end to end.
                stack.enter_context(mock.patch.object(
                    mtf.subprocess, 'run',
                    side_effect=subprocess.CalledProcessError(
                        1, ['wp', 'core', 'install'], stderr='wp core install failed')))
            else:
                stack.enter_context(mock.patch.object(mt.MTFunctions, 'install_wordpress'))
            cleanup_mock = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'cleanup_failed_site'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'purge_site_cache'))
            stack.enter_context(mock.patch.object(mt.os.path, 'exists', return_value=True))
            stack.enter_context(mock.patch('builtins.open', mock.mock_open(read_data='{}')))
            stack.enter_context(mock.patch.object(mt.json, 'load', return_value=baseline))
            apply_baseline = stack.enter_context(mock.patch.object(
                mt.BaselineApplicator,
                'apply_baseline_to_site',
                return_value={'success': True, 'error': None},
            ))
            set_permalink = stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'set_permalink_structure',
            ))
            if manager is not None:
                manager.attach_mock(apply_baseline, 'apply_baseline_to_site')
                manager.attach_mock(set_permalink, 'set_permalink_structure')
                manager.attach_mock(cleanup_mock, 'cleanup_failed_site')
                manager.attach_mock(log_mocks['error'], 'log_error')
            stack.enter_context(mock.patch.object(mt, 'setwebrootpermissions'))
            if nginx_validate_results is None:
                stack.enter_context(mock.patch.object(
                    mt.MTFunctions, 'validate_nginx_config_recoverable',
                    return_value=True))
            else:
                stack.enter_context(mock.patch.object(
                    mt.MTFunctions, 'validate_nginx_config_recoverable',
                    side_effect=nginx_validate_results))
            os_remove = stack.enter_context(mock.patch.object(mt.os, 'remove'))
            stack.enter_context(mock.patch.object(mt.os, 'chmod'))
            stack.enter_context(mock.patch.object(mt.WOFileUtils, 'create_symlink'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'safe_nginx_reload', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'get_current_release', return_value='current'))
            stack.enter_context(mock.patch.object(mt, 'addNewSite'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'add_shared_site'))
            stack.enter_context(mock.patch.object(mt.WOGit, 'add'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_admin_password', return_value='secret'))

            ctrl._create_impl()

        return {
            'apply_baseline': apply_baseline,
            'set_permalink_structure': set_permalink,
            'pargs': pargs,
            'wpredis_during_package_check': spc_seen.get('wpredis'),
            'cleanup_failed_site': cleanup_mock,
            'log_error': log_mocks['error'],
            'os_remove': os_remove,
        }

    @unittest.skipIf(shutil.which('php') is None, 'php not on PATH')
    def test_preflight_rejects_bad_php(self):
        """A syntax error in the shared config must fail the preflight."""
        self._write_shared_config('<?php\n$x = ;\n')
        with mock.patch('wo.core.logging.Log.error') as log_error, \
                mock.patch('wo.core.logging.Log.debug'):
            result = MTFunctions.preflight_shared_config(mock.Mock(), self.tmp)
        self.assertFalse(result)
        self.assertTrue(log_error.called)

    def test_preflight_passes_when_file_absent(self):
        """First init has no shared config yet — preflight must not block."""
        with mock.patch('wo.core.logging.Log.error') as log_error, \
                mock.patch('wo.core.logging.Log.debug'):
            result = MTFunctions.preflight_shared_config(mock.Mock(), self.tmp)
        self.assertTrue(result)
        self.assertFalse(log_error.called)

    def test_lint_php_file_failure_paths_never_exit(self):
        """Every lint_php_file failure reports with exit=False (recoverable)."""
        app = mock.Mock()
        cfg = os.path.join(self.tmp, 'present.php')
        with open(cfg, 'w') as fh:
            fh.write('<?php\n')
        with mock.patch('wo.cli.plugins.multitenancy_functions.Log.error') as log_error, \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            # Required file absent
            self.assertFalse(mtf.lint_php_file(
                app, os.path.join(self.tmp, 'missing.php')))
            # php missing from PATH but required
            with mock.patch.object(mtf.shutil, 'which', return_value=None):
                self.assertFalse(mtf.lint_php_file(
                    app, cfg, php_missing_ok=False))
            # php -l crashes
            with mock.patch.object(mtf.shutil, 'which',
                                   return_value='/usr/bin/php'), \
                    mock.patch.object(mtf.subprocess, 'run',
                                      side_effect=OSError('boom')):
                self.assertFalse(mtf.lint_php_file(app, cfg))
            # Real syntax error
            with mock.patch.object(mtf.shutil, 'which',
                                   return_value='/usr/bin/php'), \
                    mock.patch.object(
                        mtf.subprocess, 'run',
                        return_value=mock.Mock(returncode=255,
                                               stderr='Parse error')):
                self.assertFalse(mtf.lint_php_file(app, cfg))
        self.assertEqual(len(log_error.call_args_list), 4)
        for call in log_error.call_args_list:
            exiting = call.kwargs.get(
                'exit', call.args[2] if len(call.args) > 2 else True)
            self.assertIs(exiting, False)

    def test_maintenance_enable_omits_admin_regex(self):
        """The nginx render context no longer carries admin-IP bypass keys."""
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")
        site_root = os.path.join(self.tmp, 'site')
        htdocs = os.path.join(site_root, 'htdocs')
        nginx_dir = os.path.join(site_root, 'conf', 'nginx')
        os.makedirs(htdocs, exist_ok=True)
        paths = {
            'site_root': site_root,
            'site_htdocs': htdocs,
            'nginx_include_dir': nginx_dir,
            'nginx_include_file': os.path.join(nginx_dir, 'maintenance.conf'),
            'maintenance_html': os.path.join(htdocs, 'maintenance.html'),
        }
        captured = {}

        class _App:
            def render(self, data, template, out=None):
                captured[template] = data
                if out is not None:
                    out.write('')

        class _Controller:
            app = _App()

        with mock.patch.object(mt, '_maintenance_paths', return_value=paths), \
                mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.info'):
            ok = mt._maintenance_enable(_Controller(), 'test.local', 'msg', {})

        self.assertTrue(ok)
        ctx = captured.get('multitenancy-maintenance.mustache')
        self.assertIsNotNone(ctx)
        self.assertNotIn('has_admin_ips', ctx)
        self.assertNotIn('admin_ips_regex', ctx)
        self.assertEqual(ctx.get('retry_after_seconds'), 600)

    def test_create_forwards_computed_cache_type_to_baseline_apply(self):
        """create threads the tenant cache type into baseline activation."""
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")
        cache_type = 'wpfc'

        calls = self._run_create_impl_with_mocks(cache_type=cache_type)

        apply_baseline = calls['apply_baseline']
        apply_baseline.assert_called_once()
        self.assertEqual(apply_baseline.call_args.kwargs['cache_type'], cache_type)

    def test_create_sets_permalink_after_baseline_application(self):
        """late permalink write must follow baseline activation to refresh Redis."""
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")
        manager = mock.Mock()

        self._run_create_impl_with_mocks(manager=manager)

        apply_index = next(
            index for index, call in enumerate(manager.mock_calls)
            if call[0] == 'apply_baseline_to_site'
        )
        permalink_index = next(
            index for index, call in enumerate(manager.mock_calls)
            if call[0] == 'set_permalink_structure'
        )
        self.assertLess(apply_index, permalink_index)

    def test_create_forces_redis_package_check_and_restores_flag(self):
        """Shared-core sites always ship the Object Cache Pro drop-in, so
        create must force the redis package check even for non-redis cache
        types, then restore the user's original --wpredis flag."""
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")

        calls = self._run_create_impl_with_mocks(cache_type='basic')

        self.assertIs(calls['wpredis_during_package_check'], True)
        self.assertIs(calls['pargs'].wpredis, False)

    @staticmethod
    def _exiting_error_index(manager):
        """Index of the first Log.error call that exits (no exit=False)."""
        return next(
            index for index, call in enumerate(manager.mock_calls)
            if call[0] == 'log_error' and call[2].get('exit', True)
        )

    def test_create_failure_runs_cleanup_before_exiting_error(self):
        """A real install_wordpress failure (wp-cli CalledProcessError, logged
        with exit=False then raised) must reach the create failure handler,
        which runs cleanup_failed_site before the exiting Log.error."""
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")
        manager = mock.Mock()

        calls = self._run_create_impl_with_mocks(manager=manager, install_fails=True)

        names = [call[0] for call in manager.mock_calls]
        self.assertIn('cleanup_failed_site', names)
        # Cleanup must receive the tenant DB credentials so it can drop them.
        cleanup_kwargs = calls['cleanup_failed_site'].call_args.kwargs
        self.assertEqual(cleanup_kwargs['db_name'], 'db')
        self.assertEqual(cleanup_kwargs['db_user'], 'user')
        self.assertEqual(cleanup_kwargs['db_grant_host'], 'localhost')
        # The helper must have logged its own diagnostic without exiting.
        self.assertTrue(any(
            call[0] == 'log_error' and call[2].get('exit') is False
            for call in manager.mock_calls))
        self.assertLess(names.index('cleanup_failed_site'),
                        self._exiting_error_index(manager))

    def test_create_nginx_validate_failure_recovers_then_cleans_up(self):
        """When nginx validation fails after the site symlink is enabled,
        create must not exit on the spot: it must remove the symlink it just
        created, then run cleanup_failed_site before the exiting Log.error.
        Guards against inner default-exit Log.error calls bypassing both."""
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")
        manager = mock.Mock()

        calls = self._run_create_impl_with_mocks(
            manager=manager, nginx_validate_results=[True, False])

        calls['os_remove'].assert_called_once_with(
            '/etc/nginx/sites-enabled/example.com')
        names = [call[0] for call in manager.mock_calls]
        self.assertIn('cleanup_failed_site', names)
        self.assertIn('log_error', names)
        self.assertLess(names.index('cleanup_failed_site'),
                        self._exiting_error_index(manager))

    @contextlib.contextmanager
    def _cleanup_failed_site_mocks(self, site_record=None):
        """Isolate cleanup_failed_site from FS, nginx, and the app DBs."""
        with contextlib.ExitStack() as stack:
            for method in ('info', 'warn', 'error', 'debug'):
                stack.enter_context(mock.patch(f'wo.core.logging.Log.{method}'))
            stack.enter_context(mock.patch.object(mtf.os.path, 'exists', return_value=False))
            stack.enter_context(mock.patch.object(mtf.os, 'listdir', return_value=[]))
            get_info = stack.enter_context(mock.patch(
                'wo.cli.plugins.sitedb.getSiteInfo', return_value=site_record))
            delete_info = stack.enter_context(mock.patch(
                'wo.cli.plugins.sitedb.deleteSiteInfo'))
            delete_db = stack.enter_context(mock.patch(
                'wo.cli.plugins.site_functions.deleteDB'))
            stack.enter_context(mock.patch(
                'wo.cli.plugins.multitenancy_db.MTDatabase.remove_shared_site'))
            stack.enter_context(mock.patch.object(
                MTFunctions, 'validate_nginx_config', return_value=False))
            yield {'getSiteInfo': get_info, 'deleteSiteInfo': delete_info,
                   'deleteDB': delete_db}

    def test_cleanup_failed_site_drops_db_user_at_grant_host(self):
        """Cleanup must drop the tenant DB/user via deleteDB(exit=False),
        using the grant host (DROP USER host), when credentials are known."""
        app = mock.Mock()
        with self._cleanup_failed_site_mocks(site_record=object()) as mocks:
            MTFunctions.cleanup_failed_site(
                app, 'example.com', '/var/www/example.com',
                db_name='dbn', db_user='dbu', db_grant_host='ghost')
        mocks['deleteDB'].assert_called_once_with(
            app, 'dbn', 'dbu', 'ghost', exit=False)
        mocks['deleteSiteInfo'].assert_called_once_with(app, 'example.com')

    def test_cleanup_failed_site_survives_missing_record_and_creds(self):
        """Failures before addNewSite/setupdatabase leave no sitedb record and
        no credentials: cleanup must skip deleteSiteInfo (which exits on a
        missing record) and deleteDB, and complete without raising."""
        app = mock.Mock()
        with self._cleanup_failed_site_mocks(site_record=None) as mocks:
            MTFunctions.cleanup_failed_site(
                app, 'example.com', '/var/www/example.com')
        mocks['deleteDB'].assert_not_called()
        mocks['deleteSiteInfo'].assert_not_called()


class InstallWordpressPermalinkTests(unittest.TestCase):

    def _wp_result(self, returncode=0, stdout='', stderr=''):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_set_permalink_structure_runs_wp_rewrite_with_path(self):
        site_htdocs = '/var/www/example.com/htdocs'
        expected_cmd = [
            'wp', 'rewrite', 'structure', '/%postname%/',
            '--path=' + site_htdocs,
            '--allow-root',
        ]

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                return_value=self._wp_result(),
        ) as run, \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            MTFunctions.set_permalink_structure(
                mock.Mock(),
                'example.com',
                site_htdocs,
            )

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], expected_cmd)
        self.assertIs(run.call_args.kwargs['check'], True)
        self.assertNotIn('cwd', run.call_args.kwargs)

    def test_set_permalink_structure_reraises_wp_cli_failure(self):
        site_htdocs = '/var/www/example.com/htdocs'
        rewrite_cmd = [
            'wp', 'rewrite', 'structure', '/%postname%/',
            '--path=' + site_htdocs,
            '--allow-root',
        ]
        rewrite_error = mtf.subprocess.CalledProcessError(
            1, rewrite_cmd, stderr='rewrite failed'
        )

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                side_effect=rewrite_error,
        ) as run, \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.error'):
            with self.assertRaises(mtf.subprocess.CalledProcessError) as raised:
                MTFunctions.set_permalink_structure(
                    mock.Mock(),
                    'example.com',
                    site_htdocs,
                )

        self.assertIs(raised.exception, rewrite_error)
        run.assert_called_once()

    def test_install_wordpress_runs_early_permalink_phase_after_core_install(self):
        site_htdocs = '/var/www/example.com/htdocs'
        app = mock.Mock()
        manager = mock.Mock()

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                return_value=self._wp_result(),
        ) as run, \
                mock.patch.object(MTFunctions, 'set_permalink_structure') as set_permalink, \
                mock.patch('wo.cli.plugins.multitenancy_functions.os.chmod'), \
                mock.patch('builtins.open', mock.mock_open()), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.error'):
            manager.attach_mock(run, 'run')
            manager.attach_mock(set_permalink, 'set_permalink_structure')

            MTFunctions.install_wordpress(
                app,
                'example.com',
                site_htdocs,
                'admin',
                'admin@example.com',
            )

        self.assertTrue(any(
            call.args[0][:3] == ['wp', 'core', 'install']
            for call in run.call_args_list
        ))
        core_index = next(
            index for index, call in enumerate(manager.mock_calls)
            if call[0] == 'run'
            and call.args[0][:3] == ['wp', 'core', 'install']
        )
        permalink_index = next(
            index for index, call in enumerate(manager.mock_calls)
            if call[0] == 'set_permalink_structure'
        )
        self.assertLess(core_index, permalink_index)
        set_permalink.assert_called_once_with(app, 'example.com', site_htdocs)


class PurgeSiteCacheTests(unittest.TestCase):
    """MTFunctions.purge_site_cache: tenant-scoped, best-effort cache purge."""

    def test_fastcgi_purge_greps_and_removes_matching_cache_files(self):
        found = mock.Mock(
            returncode=0,
            stdout='/var/run/nginx-cache/a\n/var/run/nginx-cache/b\n',
            stderr='',
        )
        with mock.patch('wo.cli.plugins.multitenancy_functions.os.path.isdir', return_value=True), \
                mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run', return_value=found) as run, \
                mock.patch('wo.cli.plugins.multitenancy_functions.os.remove') as rm, \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            MTFunctions.purge_site_cache(mock.Mock(), 'example.com')

        argv = run.call_args_list[0].args[0]
        self.assertEqual(argv[0], 'grep')
        self.assertEqual(argv[-1], '/var/run/nginx-cache')
        self.assertIn('GET', argv[2])
        self.assertIn('example\\.com', argv[2])  # re.escape'd domain in the KEY pattern
        self.assertEqual(
            sorted(c.args[0] for c in rm.call_args_list),
            ['/var/run/nginx-cache/a', '/var/run/nginx-cache/b'],
        )

    def test_fastcgi_skipped_when_cache_dir_absent(self):
        with mock.patch('wo.cli.plugins.multitenancy_functions.os.path.isdir', return_value=False), \
                mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run') as run, \
                mock.patch('wo.cli.plugins.multitenancy_functions.os.remove') as rm, \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            MTFunctions.purge_site_cache(mock.Mock(), 'example.com')  # no redis_prefix
        run.assert_not_called()
        rm.assert_not_called()

    def test_redis_purge_scans_by_prefix_and_unlinks_only_those_keys(self):
        scan = mock.Mock(returncode=0, stdout='example_com_k1\nexample_com_k2\n', stderr='')
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ['redis-cli', '--scan']:
                return scan
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('wo.cli.plugins.multitenancy_functions.os.path.isdir', return_value=False), \
                mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run', side_effect=fake_run), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            MTFunctions.purge_site_cache(mock.Mock(), 'example.com', redis_prefix='example_com_')

        self.assertIn(['redis-cli', '--scan', '--pattern', 'example_com_*'], calls)
        unlink = [c for c in calls if c[:2] == ['redis-cli', 'unlink']]
        self.assertEqual(len(unlink), 1)
        self.assertEqual(unlink[0], ['redis-cli', 'unlink', 'example_com_k1', 'example_com_k2'])

    def test_redis_purge_flushes_dedicated_database(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('wo.cli.plugins.multitenancy_functions.os.path.isdir', return_value=False), \
                mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run', side_effect=fake_run), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            MTFunctions.purge_site_cache(
                mock.Mock(), 'example.com', redis_prefix='example_com_', redis_db=5)

        self.assertIn(['redis-cli', '-n', '5', 'flushdb', 'async'], calls)
        # The dedicated database is exclusive; no prefix scan must run.
        self.assertFalse(any(c[:2] == ['redis-cli', '--scan'] for c in calls))

    def test_purge_is_best_effort_on_subprocess_error(self):
        with mock.patch('wo.cli.plugins.multitenancy_functions.os.path.isdir', return_value=True), \
                mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run', side_effect=OSError('boom')), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            # Best-effort: a failing purge must never abort provisioning.
            MTFunctions.purge_site_cache(mock.Mock(), 'example.com', redis_prefix='example_com_')


class SetWpConfigRedisDbTests(unittest.TestCase):
    """MTFunctions.set_wp_config_redis_db rewrites only WP_REDIS_CONFIG's database."""

    def test_rewrites_database_line_and_leaves_rest_untouched(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, 'htdocs'))
        wp_config = os.path.join(tmp, 'htdocs', 'wp-config.php')
        with open(wp_config, 'w') as fh:
            fh.write(
                "<?php\n"
                "define('WP_REDIS_CONFIG', [\n"
                "    'database' => 0,  // All sites use database 0 with unique prefixes\n"
                "    'prefix' => 'example_com_',\n"
                "]);\n"
                "define('DB_NAME', 'db0');\n"
            )

        with mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'):
            result = MTFunctions.set_wp_config_redis_db(mock.Mock(), tmp, 7)

        self.assertTrue(result)
        with open(wp_config) as fh:
            content = fh.read()
        self.assertIn("'database' => 7,", content)
        self.assertNotIn("'database' => 0,", content)
        self.assertIn("define('DB_NAME', 'db0');", content)

    def test_returns_false_when_database_key_missing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, 'htdocs'))
        with open(os.path.join(tmp, 'htdocs', 'wp-config.php'), 'w') as fh:
            fh.write("<?php define('DB_NAME', 'db0');\n")

        with mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'):
            self.assertFalse(MTFunctions.set_wp_config_redis_db(mock.Mock(), tmp, 7))


class MultitenancyDeleteCacheTests(unittest.TestCase):
    """`wo multitenancy delete` purges the domain's caches only on success."""

    def _run_delete(self, returncode):
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")
        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        pargs = mock.Mock()
        pargs.site_name = 'example.com'
        pargs.force = True
        ctrl.app = mock.Mock()
        ctrl.app.pargs = pargs
        site = mock.Mock()
        site.redis_prefix = 'example_com_'
        site.redis_db = 3
        site.php_version = '8.4'
        session = mock.Mock()
        session.query.return_value.filter_by.return_value.first.return_value = site
        with contextlib.ExitStack() as stack:
            for method in ('info', 'warn', 'error', 'debug'):
                stack.enter_context(mock.patch(f'wo.core.logging.Log.{method}'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch('wo.core.database.db_session', session))
            stack.enter_context(mock.patch.object(mt.os.path, 'isdir', return_value=False))
            stack.enter_context(mock.patch.object(
                mt.subprocess, 'run',
                return_value=mock.Mock(returncode=returncode, stdout='', stderr='err'),
            ))
            purge = stack.enter_context(mock.patch.object(mt.MTFunctions, 'purge_site_cache'))
            reset = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'reset_opcache', return_value=True))
            ctrl._delete_impl()
        return purge, reset, ctrl

    def test_delete_purges_cache_on_success(self):
        purge, reset, ctrl = self._run_delete(returncode=0)
        purge.assert_called_once_with(ctrl, 'example.com', 'example_com_', 3)
        reset.assert_called_once_with(ctrl, php_key='php84')

    def test_delete_skips_purge_when_site_delete_fails(self):
        purge, reset, ctrl = self._run_delete(returncode=1)
        purge.assert_not_called()
        reset.assert_not_called()


class MultitenancyRenameTests(unittest.TestCase):
    """`wo multitenancy rename` preserves tenant isolation while renaming domains."""

    def _path_state(self, initial_existing):
        existing = set(initial_existing)

        def exists(path):
            return path in existing

        def rename(src, dst):
            existing.discard(src)
            existing.add(dst)

        def remove(path):
            existing.discard(path)

        def symlink(src, dst):
            existing.add(dst)

        def copy2(src, dst):
            existing.add(dst)

        def makedirs(path, *args, **kwargs):
            existing.add(path)

        def rmtree(path, *args, **kwargs):
            existing.discard(path)

        return existing, exists, rename, remove, symlink, copy2, makedirs, rmtree

    def _subprocess_guard(self, argv, *args, **kwargs):
        self.assertFalse(
            any('--alias' in str(part) for part in argv),
            f"rename must not call wp-cli with --alias: {argv}",
        )
        self.assertFalse(
            argv[:3] == ['wo', 'site', 'update'],
            f"rename must not shell out to wo site update: {argv}",
        )
        self.assertFalse(
            argv[:3] == ['wp', 'cache', 'flush'],
            f"rename must not flush a shared WordPress cache: {argv}",
        )
        self.assertFalse(
            argv[:2] in (['redis-cli', 'flushdb'], ['redis-cli', 'flushall']),
            f"rename must not flush the shared Redis database: {argv}",
        )
        return mock.Mock(returncode=0, stdout='', stderr='')

    def _first_call_index(self, manager, name):
        for index, call in enumerate(manager.mock_calls):
            if call[0] == name:
                return index
        self.fail(f"{name} was not called; calls were {manager.mock_calls!r}")

    def _assert_no_rename_mutations(self, calls):
        calls['makedirs'].assert_not_called()
        calls['copy2'].assert_not_called()
        calls['os_rename'].assert_not_called()
        calls['rewrite_wp_config_for_rename'].assert_not_called()
        calls['update_wordpress_domain'].assert_not_called()
        calls['relink_core_files_for_rename'].assert_not_called()
        calls['rename_shared_site_domain'].assert_not_called()

    def _run_rename(
            self,
            *,
            old_ssl=False,
            pargs_overrides=None,
            check_domain_exists=False,
            mt_site_present=True,
            extra_existing=None,
            old_enabled=False,
            old_available=False,
            old_force_ssl=False,
            rewrite_return='old_example_com_',
            prepare_ssl_return=True,
            install_ssl_return=True,
            reload_return=True,
            validate_return=True,
            write_nginx_return='/etc/nginx/sites-available/new.example.com',
            enable_nginx_return=True,
            update_wp_return=True,
            rename_site_info_return=True,
            rename_shared_return=True,
            manager=None,
            timestamp=None):
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")

        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        pargs = mock.Mock()
        pargs.site_name = 'old.example.com'
        pargs.newsite_name = 'new.example.com'
        pargs.force = True
        pargs.letsencrypt = False
        pargs.dns = None
        pargs.hsts = False
        if pargs_overrides:
            for name, value in pargs_overrides.items():
                setattr(pargs, name, value)
        ctrl.app = mock.Mock()
        ctrl.app.pargs = pargs

        site_info = mock.Mock()
        site_info.site_path = '/var/www/old.example.com'
        site_info.cache_type = 'wpfc'
        site_info.php_version = '8.4'
        site_info.is_ssl = old_ssl

        mt_site = None
        if mt_site_present:
            mt_site = mock.Mock()
            mt_site.domain = 'old.example.com'
            mt_site.site_path = '/var/www/old.example.com'
            mt_site.cache_type = 'wpfc'
            mt_site.php_version = '8.4'
            mt_site.is_ssl = old_ssl
            mt_site.redis_prefix = 'old_example_com_'
            mt_site.redis_db = 3

        session = mock.Mock()
        session.query.return_value.filter_by.return_value.first.return_value = mt_site

        initial_existing = {
            '/var/www/old.example.com',
            '/var/www/old.example.com/htdocs/wp-config.php',
        }
        if old_enabled:
            initial_existing.add('/etc/nginx/sites-enabled/old.example.com')
        if old_available:
            initial_existing.add('/etc/nginx/sites-available/old.example.com')
        if old_force_ssl:
            initial_existing.add('/etc/nginx/conf.d/force-ssl-old.example.com.conf')
        if extra_existing:
            initial_existing.update(extra_existing)

        existing, exists, rename_side_effect, remove_side_effect, symlink_side_effect, \
            copy2_side_effect, makedirs_side_effect, rmtree_side_effect = self._path_state(initial_existing)

        with contextlib.ExitStack() as stack:
            log_mocks = {}
            for method in ('info', 'warn', 'error', 'debug'):
                log_mocks[method] = stack.enter_context(mock.patch(f'wo.core.logging.Log.{method}'))

            if timestamp is not None:
                datetime_mock = mock.Mock()
                datetime_mock.now.return_value.strftime.return_value = timestamp
                stack.enter_context(mock.patch.object(mt, 'datetime', datetime_mock))

            stack.enter_context(mock.patch.object(mt.WODomain, 'validate', side_effect=[
                'old.example.com', 'new.example.com',
            ]))
            get_site_info = stack.enter_context(mock.patch.object(mt, 'getSiteInfo', return_value=site_info))
            check_exists = stack.enter_context(mock.patch.object(
                mt, 'check_domain_exists', return_value=check_domain_exists,
            ))
            stack.enter_context(mock.patch('wo.core.database.db_session', session))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_shared_site', return_value=False))
            generate_redis_prefix = stack.enter_context(mock.patch.object(
                mt.MTDatabase, 'generate_redis_prefix', return_value='new_example_com_',
            ))
            rename_shared_site_domain = stack.enter_context(mock.patch.object(
                mt.MTDatabase, 'rename_shared_site_domain', return_value=rename_shared_return,
            ))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'load_config', return_value={
                'shared_root': '/var/www/shared',
            }))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'preflight_shared_config', return_value=True))
            prepare_ssl = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'prepare_ssl_certificate_for_rename', return_value=prepare_ssl_return,
            ))
            relink_core_files = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'relink_core_files_for_rename', return_value=True,
            ))
            rewrite_wp_config = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'rewrite_wp_config_for_rename', return_value=rewrite_return,
            ))
            write_nginx = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'write_nginx_config_for_rename', return_value=write_nginx_return,
            ))
            enable_nginx = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'enable_nginx_site_for_rename', return_value=enable_nginx_return,
            ))
            validate_nginx = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'validate_nginx_config_recoverable', return_value=validate_return,
            ))
            update_wp = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'update_wordpress_domain', return_value=update_wp_return,
            ))
            install_ssl = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'install_ssl_config_for_rename', return_value=install_ssl_return,
            ))
            purge_cache = stack.enter_context(mock.patch.object(mt.MTFunctions, 'purge_site_cache'))
            reload_nginx = stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'reload_nginx_recoverable', return_value=reload_return,
            ))
            rename_site_info = stack.enter_context(mock.patch.object(
                mt, 'renameSiteInfo', return_value=rename_site_info_return,
            ))
            wogit_add = stack.enter_context(mock.patch.object(mt.WOGit, 'add'))
            os_rename = stack.enter_context(mock.patch.object(mt.os, 'rename', side_effect=rename_side_effect))
            os_remove = stack.enter_context(mock.patch.object(mt.os, 'remove', side_effect=remove_side_effect))
            os_symlink = stack.enter_context(mock.patch.object(mt.os, 'symlink', side_effect=symlink_side_effect))
            os_makedirs = stack.enter_context(mock.patch.object(mt.os, 'makedirs', side_effect=makedirs_side_effect))
            os_chmod = stack.enter_context(mock.patch.object(mt.os, 'chmod'))
            path_exists = stack.enter_context(mock.patch.object(mt.os.path, 'exists', side_effect=exists))
            path_lexists = stack.enter_context(mock.patch.object(mt.os.path, 'lexists', side_effect=exists))
            copy2 = stack.enter_context(mock.patch.object(mt.shutil, 'copy2', side_effect=copy2_side_effect))
            rmtree = stack.enter_context(mock.patch.object(mt.shutil, 'rmtree', side_effect=rmtree_side_effect))
            subprocess_run = stack.enter_context(mock.patch.object(
                mt.subprocess, 'run', side_effect=self._subprocess_guard,
            ))

            if manager is not None:
                manager.attach_mock(prepare_ssl, 'prepare_ssl')
                manager.attach_mock(os_rename, 'rename')
                manager.attach_mock(relink_core_files, 'relink_core_files')
                manager.attach_mock(rewrite_wp_config, 'rewrite_wp_config')
                manager.attach_mock(write_nginx, 'write_nginx')
                manager.attach_mock(enable_nginx, 'enable_nginx')
                manager.attach_mock(validate_nginx, 'validate_nginx')
                manager.attach_mock(update_wp, 'update_wp')
                manager.attach_mock(install_ssl, 'install_ssl')
                manager.attach_mock(rename_site_info, 'rename_site_info')
                manager.attach_mock(rename_shared_site_domain, 'rename_shared_site_domain')
                manager.attach_mock(purge_cache, 'purge_cache')
                manager.attach_mock(reload_nginx, 'reload_nginx')

            result = ctrl._rename_impl()

        return {
            'result': result,
            'ctrl': ctrl,
            'pargs': pargs,
            'session': session,
            'mt_site': mt_site,
            'site_info': site_info,
            'existing': existing,
            'log_info': log_mocks['info'],
            'log_warn': log_mocks['warn'],
            'log_error': log_mocks['error'],
            'log_debug': log_mocks['debug'],
            'getSiteInfo': get_site_info,
            'check_domain_exists': check_exists,
            'generate_redis_prefix': generate_redis_prefix,
            'prepare_ssl_certificate_for_rename': prepare_ssl,
            'rewrite_wp_config_for_rename': rewrite_wp_config,
            'relink_core_files_for_rename': relink_core_files,
            'write_nginx_config_for_rename': write_nginx,
            'enable_nginx_site_for_rename': enable_nginx,
            'validate_nginx_config_recoverable': validate_nginx,
            'update_wordpress_domain': update_wp,
            'install_ssl_config_for_rename': install_ssl,
            'purge_site_cache': purge_cache,
            'reload_nginx_recoverable': reload_nginx,
            'renameSiteInfo': rename_site_info,
            'rename_shared_site_domain': rename_shared_site_domain,
            'WOGit.add': wogit_add,
            'os_rename': os_rename,
            'os_remove': os_remove,
            'os_symlink': os_symlink,
            'makedirs': os_makedirs,
            'chmod': os_chmod,
            'path_exists': path_exists,
            'path_lexists': path_lexists,
            'copy2': copy2,
            'rmtree': rmtree,
            'subprocess_run': subprocess_run,
        }

    def test_rename_closes_app_with_failure_status_when_impl_fails(self):
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")

        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        ctrl.app = mock.Mock()
        ctrl._rename_impl = mock.Mock(return_value=False)

        self.assertFalse(ctrl.rename())
        ctrl._rename_impl.assert_called_once_with()
        ctrl.app.close.assert_called_once_with(1)

    def test_rename_returns_success_without_closing_app_when_impl_succeeds(self):
        if mt is None:
            self.skipTest(f"multitenancy controller import unavailable: {_mt_import_error}")

        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        ctrl.app = mock.Mock()
        ctrl._rename_impl = mock.Mock(return_value=True)

        self.assertTrue(ctrl.rename())
        ctrl._rename_impl.assert_called_once_with()
        ctrl.app.close.assert_not_called()

    def test_rename_requires_two_domains(self):
        calls = self._run_rename(pargs_overrides={'newsite_name': None})

        self.assertFalse(calls['result'])
        calls['log_error'].assert_any_call(
            calls['ctrl'],
            "Usage: wo multitenancy rename <old-domain> <new-domain>",
            exit=False,
        )
        self._assert_no_rename_mutations(calls)

    def test_rename_rejects_existing_target(self):
        calls = self._run_rename(check_domain_exists=True)

        self.assertFalse(calls['result'])
        calls['log_error'].assert_any_call(
            calls['ctrl'], "Site new.example.com already exists", exit=False,
        )
        self._assert_no_rename_mutations(calls)

    def test_rename_rejects_untracked_old_site(self):
        calls = self._run_rename(mt_site_present=False)

        self.assertFalse(calls['result'])
        calls['log_error'].assert_any_call(
            calls['ctrl'],
            "Site old.example.com not found in multitenancy tracking",
            exit=False,
        )
        self._assert_no_rename_mutations(calls)

    def test_rename_rejects_filesystem_or_nginx_target_conflict_before_mutation(self):
        conflict_path = '/etc/nginx/sites-enabled/new.example.com'
        calls = self._run_rename(extra_existing={conflict_path})

        self.assertFalse(calls['result'])
        calls['log_error'].assert_any_call(
            calls['ctrl'], f"Target path already exists: {conflict_path}", exit=False,
        )
        self._assert_no_rename_mutations(calls)

    def test_rename_moves_root_rewrites_wp_updates_databases_and_purges_caches(self):
        manager = mock.Mock()
        calls = self._run_rename(old_available=True, manager=manager)
        ctrl = calls['ctrl']

        self.assertTrue(calls['result'])
        calls['os_rename'].assert_any_call('/var/www/old.example.com', '/var/www/new.example.com')
        calls['os_remove'].assert_any_call('/etc/nginx/sites-available/old.example.com')
        calls['relink_core_files_for_rename'].assert_called_once_with(
            ctrl, '/var/www/new.example.com/htdocs',
        )
        calls['rewrite_wp_config_for_rename'].assert_called_once_with(
            ctrl,
            '/var/www/new.example.com',
            'old.example.com',
            'new.example.com',
            'new_example_com_',
            'old_example_com_',
        )
        calls['update_wordpress_domain'].assert_called_once_with(
            ctrl,
            '/var/www/new.example.com/htdocs',
            'old.example.com',
            'new.example.com',
            'http',
        )
        calls['renameSiteInfo'].assert_called_once_with(
            ctrl,
            'old.example.com',
            'new.example.com',
            site_path='/var/www/new.example.com',
            ssl=False,
        )
        calls['rename_shared_site_domain'].assert_called_once_with(
            ctrl,
            'old.example.com',
            'new.example.com',
            site_path='/var/www/new.example.com',
            redis_prefix='new_example_com_',
            is_ssl=False,
        )
        self.assertEqual(calls['purge_site_cache'].call_args_list, [
            mock.call(ctrl, 'old.example.com', 'old_example_com_', 3),
            mock.call(ctrl, 'new.example.com', 'new_example_com_', 3),
        ])

        self.assertLess(self._first_call_index(manager, 'rename'), self._first_call_index(manager, 'relink_core_files'))
        self.assertLess(self._first_call_index(manager, 'relink_core_files'), self._first_call_index(manager, 'rewrite_wp_config'))
        self.assertLess(self._first_call_index(manager, 'rewrite_wp_config'), self._first_call_index(manager, 'write_nginx'))
        self.assertLess(self._first_call_index(manager, 'write_nginx'), self._first_call_index(manager, 'enable_nginx'))
        self.assertLess(self._first_call_index(manager, 'enable_nginx'), self._first_call_index(manager, 'validate_nginx'))
        self.assertLess(self._first_call_index(manager, 'validate_nginx'), self._first_call_index(manager, 'update_wp'))
        self.assertLess(self._first_call_index(manager, 'update_wp'), self._first_call_index(manager, 'rename_site_info'))
        self.assertLess(self._first_call_index(manager, 'rename_site_info'), self._first_call_index(manager, 'rename_shared_site_domain'))
        self.assertLess(self._first_call_index(manager, 'rename_shared_site_domain'), self._first_call_index(manager, 'purge_cache'))
        self.assertLess(self._first_call_index(manager, 'purge_cache'), self._first_call_index(manager, 'reload_nginx'))

    def test_rename_ssl_site_prepares_and_installs_ssl_and_records_ssl(self):
        manager = mock.Mock()
        calls = self._run_rename(old_ssl=True, manager=manager)
        ctrl = calls['ctrl']

        self.assertTrue(calls['result'])
        calls['prepare_ssl_certificate_for_rename'].assert_called_once_with(
            ctrl, 'new.example.com', calls['pargs'],
        )
        calls['install_ssl_config_for_rename'].assert_called_once_with(
            ctrl, 'new.example.com', '/var/www/new.example.com', calls['pargs'],
        )
        calls['update_wordpress_domain'].assert_called_once_with(
            ctrl,
            '/var/www/new.example.com/htdocs',
            'old.example.com',
            'new.example.com',
            'https',
        )
        calls['renameSiteInfo'].assert_called_once_with(
            ctrl,
            'old.example.com',
            'new.example.com',
            site_path='/var/www/new.example.com',
            ssl=True,
        )
        calls['rename_shared_site_domain'].assert_called_once_with(
            ctrl,
            'old.example.com',
            'new.example.com',
            site_path='/var/www/new.example.com',
            redis_prefix='new_example_com_',
            is_ssl=True,
        )
        self.assertLess(self._first_call_index(manager, 'prepare_ssl'), self._first_call_index(manager, 'rename'))
        self.assertLess(self._first_call_index(manager, 'update_wp'), self._first_call_index(manager, 'install_ssl'))
        self.assertLess(self._first_call_index(manager, 'install_ssl'), self._first_call_index(manager, 'rename_site_info'))
        self.assertLess(self._first_call_index(manager, 'install_ssl'), self._first_call_index(manager, 'rename_shared_site_domain'))

    def test_rename_ssl_setup_failure_rolls_back_before_db_updates(self):
        calls = self._run_rename(
            old_ssl=True,
            old_enabled=True,
            old_available=True,
            install_ssl_return=False,
        )
        ctrl = calls['ctrl']

        self.assertFalse(calls['result'])
        calls['update_wordpress_domain'].assert_any_call(
            ctrl,
            '/var/www/new.example.com/htdocs',
            'old.example.com',
            'new.example.com',
            'https',
        )
        calls['renameSiteInfo'].assert_not_called()
        calls['rename_shared_site_domain'].assert_not_called()
        calls['os_rename'].assert_any_call('/var/www/old.example.com', '/var/www/new.example.com')
        calls['os_rename'].assert_any_call('/var/www/new.example.com', '/var/www/old.example.com')
        calls['os_symlink'].assert_any_call(
            '/etc/nginx/sites-available/old.example.com',
            '/etc/nginx/sites-enabled/old.example.com',
        )
        calls['log_info'].assert_any_call(
            ctrl,
            "site_rename_failed source=old.example.com target=new.example.com result=failure",
        )

    def test_rename_rolls_back_db_and_wp_when_reload_fails(self):
        calls = self._run_rename(
            old_available=True,
            reload_return=False,
            timestamp='20260707-010203',
        )
        ctrl = calls['ctrl']

        self.assertFalse(calls['result'])
        calls['rename_shared_site_domain'].assert_any_call(
            ctrl,
            'new.example.com',
            'old.example.com',
            site_path='/var/www/old.example.com',
            redis_prefix='old_example_com_',
            is_ssl=False,
        )
        calls['renameSiteInfo'].assert_any_call(
            ctrl,
            'new.example.com',
            'old.example.com',
            site_path='/var/www/old.example.com',
            ssl=False,
        )
        calls['update_wordpress_domain'].assert_any_call(
            ctrl,
            '/var/www/new.example.com/htdocs',
            'new.example.com',
            'old.example.com',
            'http',
        )
        calls['os_rename'].assert_any_call('/var/www/new.example.com', '/var/www/old.example.com')
        calls['copy2'].assert_any_call(
            '/var/www/.wo-rename-old.example.com-to-new.example.com-20260707-010203/nginx-site-available-old.example.com',
            '/etc/nginx/sites-available/old.example.com',
        )

    def test_rename_stale_ssl_includes_are_moved_and_restored_on_failure(self):
        timestamp = '20260707-123456'
        ssl_include = '/var/www/new.example.com/conf/nginx/ssl.conf'
        hsts_include = '/var/www/new.example.com/conf/nginx/hsts.conf'
        ssl_backup = f'{ssl_include}.rename.{timestamp}.bak'
        hsts_backup = f'{hsts_include}.rename.{timestamp}.bak'
        manager = mock.Mock()
        calls = self._run_rename(
            extra_existing={ssl_include, hsts_include},
            reload_return=False,
            manager=manager,
            timestamp=timestamp,
        )

        self.assertFalse(calls['result'])
        calls['os_rename'].assert_any_call(ssl_include, ssl_backup)
        calls['os_rename'].assert_any_call(hsts_include, hsts_backup)
        calls['os_rename'].assert_any_call(ssl_backup, ssl_include)
        calls['os_rename'].assert_any_call(hsts_backup, hsts_include)
        calls['os_rename'].assert_any_call('/var/www/new.example.com', '/var/www/old.example.com')

        ssl_move_index = manager.mock_calls.index(mock.call.rename(ssl_include, ssl_backup))
        hsts_move_index = manager.mock_calls.index(mock.call.rename(hsts_include, hsts_backup))
        validate_index = self._first_call_index(manager, 'validate_nginx')
        ssl_restore_index = manager.mock_calls.index(mock.call.rename(ssl_backup, ssl_include))
        hsts_restore_index = manager.mock_calls.index(mock.call.rename(hsts_backup, hsts_include))
        root_restore_index = manager.mock_calls.index(mock.call.rename('/var/www/new.example.com', '/var/www/old.example.com'))

        self.assertLess(ssl_move_index, validate_index)
        self.assertLess(hsts_move_index, validate_index)
        self.assertLess(ssl_restore_index, root_restore_index)
        self.assertLess(hsts_restore_index, root_restore_index)

    def test_rename_does_not_use_alias_or_shared_cache_flush(self):
        calls = self._run_rename()
        ctrl = calls['ctrl']

        self.assertTrue(calls['result'])
        calls['os_rename'].assert_any_call('/var/www/old.example.com', '/var/www/new.example.com')
        calls['update_wordpress_domain'].assert_called_once_with(
            ctrl,
            '/var/www/new.example.com/htdocs',
            'old.example.com',
            'new.example.com',
            'http',
        )
        calls['rename_shared_site_domain'].assert_called_once_with(
            ctrl,
            'old.example.com',
            'new.example.com',
            site_path='/var/www/new.example.com',
            redis_prefix='new_example_com_',
            is_ssl=False,
        )


class MultitenancyRenameHelperTests(unittest.TestCase):
    """Public helper contracts used by `wo multitenancy rename`."""

    def _write_wp_config(self, site_root, content):
        htdocs = os.path.join(site_root, 'htdocs')
        os.makedirs(htdocs)
        wp_config = os.path.join(htdocs, 'wp-config.php')
        with open(wp_config, 'w') as fh:
            fh.write(content)
        return wp_config

    def _db_session_for_domains(self, rows_by_domain):
        session = mock.Mock()
        query = mock.Mock()

        def filter_by(**kwargs):
            domain = kwargs['domain']
            return mock.Mock(first=mock.Mock(return_value=rows_by_domain.get(domain)))

        query.filter_by.side_effect = filter_by
        session.query.return_value = query
        return session

    def test_relink_core_files_for_rename_replaces_absolute_core_links_with_relative_links(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        htdocs = os.path.join(tmp, 'htdocs')
        shared = os.path.join(tmp, 'shared')
        os.makedirs(htdocs)
        os.makedirs(shared)
        os.symlink(shared, os.path.join(htdocs, 'wp'))
        core_files = (
            'wp-login.php', 'wp-admin', 'wp-includes', 'wp-cron.php',
            'xmlrpc.php', 'wp-comments-post.php', 'wp-settings.php',
        )
        for name in core_files:
            target = os.path.join(shared, name)
            if name in ('wp-admin', 'wp-includes'):
                os.makedirs(target)
            else:
                with open(target, 'w') as fh:
                    fh.write(f'{name}\n')
            os.symlink(os.path.join(htdocs, 'wp', name), os.path.join(htdocs, name))

        self.assertTrue(MTFunctions.relink_core_files_for_rename(mock.Mock(), htdocs))

        for name in core_files:
            link = os.path.join(htdocs, name)
            self.assertTrue(os.path.islink(link), name)
            self.assertEqual(os.readlink(link), f'wp/{name}')
            self.assertTrue(os.path.exists(link), name)

    def test_relink_core_files_for_rename_refuses_to_replace_real_core_path(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        htdocs = os.path.join(tmp, 'htdocs')
        shared = os.path.join(tmp, 'shared')
        os.makedirs(htdocs)
        os.makedirs(shared)
        os.symlink(shared, os.path.join(htdocs, 'wp'))
        protected_path = os.path.join(htdocs, 'wp-login.php')
        with open(protected_path, 'wb') as fh:
            fh.write(b'custom login entrypoint')

        with mock.patch('wo.cli.plugins.multitenancy_functions.Log.error') as log_error:
            self.assertFalse(MTFunctions.relink_core_files_for_rename(mock.Mock(), htdocs))

        self.assertTrue(os.path.exists(protected_path))
        self.assertFalse(os.path.islink(protected_path))
        with open(protected_path, 'rb') as fh:
            self.assertEqual(fh.read(), b'custom login entrypoint')
        log_error.assert_called_once()

    def test_rewrite_wp_config_for_rename_preserves_db_credentials_and_salts(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        original = "\n".join([
            "<?php",
            "define('DB_NAME', 'wordpress_old');",
            "define('DB_USER', 'wordpress_user');",
            "define('DB_PASSWORD', 'secret');",
            "define('DB_HOST', 'localhost');",
            "define('AUTH_KEY', 'auth salt value');",
            "define('SECURE_AUTH_KEY', 'secure auth salt value');",
            "define('LOGGED_IN_KEY', 'logged in salt value');",
            "define('NONCE_KEY', 'nonce salt value');",
            "define('WP_CONTENT_URL', 'https://old.example.com/wp-content');",
            "$redis_server = array(",
            "    'host' => '127.0.0.1',",
            "    'prefix' => 'old_example_com_',",
            "    'prefix' => 'secondary_prefix_must_not_change_',",
            ");",
            "",
        ])
        wp_config = self._write_wp_config(tmp, original)
        protected_prefixes = (
            "define('DB_",
            "define('AUTH_KEY'",
            "define('SECURE_AUTH_KEY'",
            "define('LOGGED_IN_KEY'",
            "define('NONCE_KEY'",
        )
        original_protected = [
            line for line in original.splitlines()
            if line.startswith(protected_prefixes)
        ]

        with mock.patch('wo.cli.plugins.multitenancy_functions.os.chmod') as chmod, \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.error'):
            old_prefix = MTFunctions.rewrite_wp_config_for_rename(
                mock.Mock(),
                tmp,
                'old.example.com',
                'new.example.com',
                'new_example_com_',
                'old_example_com_',
            )

        with open(wp_config) as fh:
            rewritten = fh.read()
        rewritten_protected = [
            line for line in rewritten.splitlines()
            if line.startswith(protected_prefixes)
        ]
        self.assertEqual(old_prefix, 'old_example_com_')
        self.assertEqual(rewritten_protected, original_protected)
        self.assertEqual(rewritten.count("'prefix' => 'new_example_com_'"), 1)
        self.assertIn("'prefix' => 'secondary_prefix_must_not_change_'", rewritten)
        self.assertNotIn("https://old.example.com/wp-content", rewritten)
        self.assertIn("https://new.example.com/wp-content", rewritten)
        chmod.assert_called_once_with(wp_config, 0o640)

    def test_rewrite_wp_config_for_rename_missing_redis_prefix_returns_none_without_rewrite(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        original = "\n".join([
            "<?php",
            "define('DB_NAME', 'wordpress_old');",
            "define('WP_CONTENT_URL', 'https://old.example.com/wp-content');",
            "",
        ])
        wp_config = self._write_wp_config(tmp, original)

        with mock.patch('wo.cli.plugins.multitenancy_functions.os.chmod') as chmod, \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.error'):
            old_prefix = MTFunctions.rewrite_wp_config_for_rename(
                mock.Mock(),
                tmp,
                'old.example.com',
                'new.example.com',
                'new_example_com_',
                'old_example_com_',
            )

        self.assertIsNone(old_prefix)
        with open(wp_config) as fh:
            self.assertEqual(fh.read(), original)
        chmod.assert_not_called()

    def test_update_wordpress_domain_uses_allow_root_and_safe_search_replace_flags(self):
        with mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run') as run:
            result = MTFunctions.update_wordpress_domain(
                mock.Mock(),
                '/var/www/new.example.com/htdocs',
                'old.example.com',
                'new.example.com',
                'https',
            )

        self.assertTrue(result)
        expected_argvs = [
            [
                'wp', 'option', 'update', 'home', 'https://new.example.com',
                '--path=/var/www/new.example.com/htdocs', '--allow-root',
            ],
            [
                'wp', 'option', 'update', 'siteurl', 'https://new.example.com',
                '--path=/var/www/new.example.com/htdocs', '--allow-root',
            ],
            [
                'wp', 'search-replace', 'old.example.com', 'new.example.com',
                '--all-tables-with-prefix',
                '--skip-columns=guid',
                '--precise',
                '--recurse-objects',
                '--path=/var/www/new.example.com/htdocs',
                '--allow-root',
            ],
        ]
        self.assertEqual([call.args[0] for call in run.call_args_list], expected_argvs)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs, {
                'capture_output': True,
                'text': True,
                'check': True,
            })
            self.assertIn('--allow-root', call.args[0])
            self.assertNotEqual(call.args[0][:3], ['wp', 'cache', 'flush'])
        search_replace = run.call_args_list[2].args[0]
        for flag in ('--all-tables-with-prefix', '--skip-columns=guid', '--precise', '--recurse-objects'):
            self.assertIn(flag, search_replace)

    def test_rename_shared_site_domain_recoverable_errors_and_preserved_fields(self):
        from wo.cli.plugins.multitenancy_db import MTDatabase

        app = mock.Mock()
        missing_session = self._db_session_for_domains({})
        conflict_old = mock.Mock()
        conflict_new = mock.Mock()
        conflict_session = self._db_session_for_domains({
            'old.example.com': conflict_old,
            'new.example.com': conflict_new,
        })

        with mock.patch('wo.cli.plugins.multitenancy_db.Log.error') as log_error, \
                mock.patch('wo.core.database.db_session', missing_session), \
                mock.patch('wo.cli.plugins.multitenancy_db.db_session', missing_session):
            self.assertFalse(MTDatabase.rename_shared_site_domain(app, 'old.example.com', 'new.example.com'))
        with mock.patch('wo.cli.plugins.multitenancy_db.Log.error') as conflict_log_error, \
                mock.patch('wo.core.database.db_session', conflict_session), \
                mock.patch('wo.cli.plugins.multitenancy_db.db_session', conflict_session):
            self.assertFalse(MTDatabase.rename_shared_site_domain(app, 'old.example.com', 'new.example.com'))

        for call in log_error.call_args_list + conflict_log_error.call_args_list:
            self.assertIs(call.kwargs.get('exit'), False)

        created_at = object()
        site = mock.Mock()
        site.id = 7
        site.domain = 'old.example.com'
        site.site_path = '/var/www/old.example.com'
        site.redis_prefix = 'old_example_com_'
        site.is_ssl = False
        site.is_enabled = True
        site.shared_release = 'release-2026'
        site.site_type = 'wp'
        site.cache_type = 'wpfc'
        site.php_version = '8.4'
        site.created_at = created_at
        site.updated_at = 'previous-updated-at'
        rows = {'old.example.com': site, 'new.example.com': None}
        success_session = self._db_session_for_domains(rows)

        with mock.patch('wo.cli.plugins.multitenancy_db.Log.debug'), \
                mock.patch('wo.core.database.db_session', success_session), \
                mock.patch('wo.cli.plugins.multitenancy_db.db_session', success_session):
            self.assertTrue(MTDatabase.rename_shared_site_domain(
                app,
                'old.example.com',
                'new.example.com',
                site_path='/var/www/new.example.com',
                redis_prefix='new_example_com_',
                is_ssl=True,
            ))

        self.assertIs(rows['old.example.com'], site)
        self.assertEqual(site.id, 7)
        self.assertTrue(site.is_enabled)
        self.assertEqual(site.shared_release, 'release-2026')
        self.assertEqual(site.site_type, 'wp')
        self.assertEqual(site.cache_type, 'wpfc')
        self.assertEqual(site.php_version, '8.4')
        self.assertIs(site.created_at, created_at)
        self.assertEqual(site.domain, 'new.example.com')
        self.assertEqual(site.site_path, '/var/www/new.example.com')
        self.assertEqual(site.redis_prefix, 'new_example_com_')
        self.assertTrue(site.is_ssl)
        self.assertNotEqual(site.updated_at, 'previous-updated-at')
        success_session.commit.assert_called_once_with()

    def test_rename_site_info_updates_sitename_and_path_without_changing_db_credentials(self):
        from wo.cli.plugins.sitedb import renameSiteInfo

        app = mock.Mock()
        site = mock.Mock()
        site.sitename = 'old.example.com'
        site.site_path = '/var/www/old.example.com'
        site.is_ssl = False
        site.created_on = 'created-on'
        site.site_type = 'wp'
        site.cache_type = 'wpfc'
        site.db_name = 'wordpress_old'
        site.db_user = 'wordpress_user'
        site.db_password = 'secret'
        site.db_host = 'localhost'
        site.php_version = '8.4'
        old_lookup = mock.Mock(first=mock.Mock(return_value=site))
        new_lookup = mock.Mock(first=mock.Mock(return_value=None))

        with mock.patch('wo.cli.plugins.sitedb.SiteDB') as SiteDB, \
                mock.patch('wo.cli.plugins.sitedb.db_session') as db_session:
            SiteDB.query.filter.side_effect = [old_lookup, new_lookup]
            self.assertTrue(renameSiteInfo(
                app,
                'old.example.com',
                'new.example.com',
                site_path='/var/www/new.example.com',
                ssl=True,
            ))

        self.assertEqual(site.sitename, 'new.example.com')
        self.assertEqual(site.site_path, '/var/www/new.example.com')
        self.assertTrue(site.is_ssl)
        self.assertEqual(site.created_on, 'created-on')
        self.assertEqual(site.site_type, 'wp')
        self.assertEqual(site.cache_type, 'wpfc')
        self.assertEqual(site.db_name, 'wordpress_old')
        self.assertEqual(site.db_user, 'wordpress_user')
        self.assertEqual(site.db_password, 'secret')
        self.assertEqual(site.db_host, 'localhost')
        self.assertEqual(site.php_version, '8.4')
        db_session.commit.assert_called_once_with()

    def test_recoverable_nginx_helpers_do_not_call_exiting_log_error(self):
        app = mock.Mock()

        with mock.patch('wo.cli.plugins.multitenancy_functions.Log.error') as log_error:
            with mock.patch(
                    'wo.cli.plugins.multitenancy_functions.subprocess.run',
                    side_effect=RuntimeError('nginx unavailable')):
                self.assertFalse(MTFunctions.validate_nginx_config_recoverable(app))

            with mock.patch.object(
                    MTFunctions,
                    'generate_modular_nginx_config',
                    side_effect=RuntimeError('render failed')):
                self.assertIsNone(MTFunctions.write_nginx_config_for_rename(
                    app,
                    'new.example.com',
                    '8.4',
                    'wpfc',
                    '/var/www/new.example.com',
                ))

            with mock.patch('wo.cli.plugins.multitenancy_functions.os.path.lexists', return_value=False), \
                    mock.patch('wo.cli.plugins.multitenancy_functions.os.path.exists', return_value=False), \
                    mock.patch(
                        'wo.cli.plugins.multitenancy_functions.os.symlink',
                        side_effect=RuntimeError('symlink failed'),
                    ):
                self.assertFalse(MTFunctions.enable_nginx_site_for_rename(app, 'new.example.com'))

            reload_failures = [
                mtf.subprocess.CalledProcessError(1, ['systemctl', 'reload', 'nginx'], stderr='systemctl failed'),
                mtf.subprocess.CalledProcessError(1, ['nginx', '-s', 'reload'], stderr='nginx reload failed'),
            ]
            with mock.patch.object(MTFunctions, 'validate_nginx_config_recoverable', return_value=True), \
                    mock.patch(
                        'wo.cli.plugins.multitenancy_functions.subprocess.run',
                        side_effect=reload_failures,
                    ), \
                    mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'):
                self.assertFalse(MTFunctions.reload_nginx_recoverable(app, 'new.example.com'))

        self.assertTrue(log_error.call_args_list)
        for call in log_error.call_args_list:
            self.assertIs(call.kwargs.get('exit'), False)


class SharedContentLayoutTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.infra = SharedInfrastructure(mock.Mock(), self.tmp)

    def test_fresh_init_creates_content_storage_and_fixed_links(self):
        self.infra.create_directory_structure()

        for name in ('releases', 'content', 'config', 'backups'):
            self.assertTrue(os.path.isdir(os.path.join(self.tmp, name)))
        self.assertEqual(
            os.readlink(os.path.join(self.tmp, 'wp-content')),
            'content/current/wp-content',
        )
        self.assertEqual(
            os.readlink(os.path.join(self.tmp, 'config', 'baseline.json')),
            '../content/current/baseline.json',
        )
        self.assertFalse(os.path.lexists(os.path.join(self.tmp, 'content', 'current')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'config', 'baseline.json')))

        # The fixed links are deliberately dangling until the first deploy.
        self.infra.create_directory_structure()
        self.assertTrue(os.path.islink(os.path.join(self.tmp, 'wp-content')))

    def test_init_leaves_real_content_and_operator_baseline_untouched(self):
        content = os.path.join(self.tmp, 'wp-content')
        os.makedirs(os.path.join(content, 'plugins'))
        plugin = os.path.join(content, 'plugins', 'operator.php')
        with open(plugin, 'wb') as fh:
            fh.write(b'<?php // operator code\n')
        os.makedirs(os.path.join(self.tmp, 'config'))
        baseline = os.path.join(self.tmp, 'config', 'baseline.json')
        original = b'{"plugins":["operator"],"theme":""}\n'
        with open(baseline, 'wb') as fh:
            fh.write(original)

        with mock.patch('wo.core.logging.Log.warn') as warn:
            self.infra.create_directory_structure()

        self.assertFalse(os.path.islink(content))
        self.assertFalse(os.path.islink(baseline))
        with open(plugin, 'rb') as fh:
            self.assertEqual(fh.read(), b'<?php // operator code\n')
        with open(baseline, 'rb') as fh:
            self.assertEqual(fh.read(), original)
        self.assertTrue(warn.called)

    def test_init_permissions_never_touch_content_releases_or_symlink_targets(self):
        self.infra.create_directory_structure()
        release = os.path.join(self.tmp, 'content', 'release-one')
        content = os.path.join(release, 'wp-content')
        os.makedirs(content)
        payload = os.path.join(content, 'plugin.php')
        with open(payload, 'w') as fh:
            fh.write('<?php\n')
        baseline = os.path.join(release, 'baseline.json')
        with open(baseline, 'w') as fh:
            fh.write('{}')
        os.symlink('release-one', os.path.join(self.tmp, 'content', 'current'))

        with mock.patch.object(mtf.shutil, 'chown') as chown, \
                mock.patch.object(mtf.os, 'chmod') as chmod:
            self.infra.set_permissions()

        for operation in (chown, chmod):
            self.assertTrue(operation.called)
            paths = [call.args[0] for call in operation.call_args_list]
            self.assertNotIn(os.path.join(self.tmp, 'wp-content'), paths)
            self.assertNotIn(os.path.join(self.tmp, 'config', 'baseline.json'), paths)
            self.assertFalse(any(
                path == os.path.join(self.tmp, 'content')
                or path.startswith(os.path.join(self.tmp, 'content') + os.sep)
                for path in paths
            ))


class SimplifiedControllerTests(unittest.TestCase):

    def setUp(self):
        if mt is None:
            self.skipTest(f'multitenancy controller import unavailable: {_mt_import_error}')
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        self.ctrl.app = mock.Mock()
        self.ctrl.app.pargs = mock.Mock(
            force=False, site_name='example.com', newsite_name=None,
            dry_run=False, verbose=False, prune=False,
        )
        self.config = {'shared_root': self.tmp}

    @contextlib.contextmanager
    def _environment(self):
        with contextlib.ExitStack() as stack:
            logs = {}
            for method in ('info', 'warn', 'error', 'debug'):
                logs[method] = stack.enter_context(mock.patch(f'wo.core.logging.Log.{method}'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'load_config', return_value=self.config))
            stack.enter_context(mock.patch.object(
                mt.MTFunctions, 'preflight_shared_config', return_value=True,
            ))
            yield stack, logs

    def test_create_refuses_absent_or_dangling_content_before_provisioning(self):
        for layout in ('missing', 'fresh-init', 'missing-release'):
            with self.subTest(layout=layout):
                if layout == 'fresh-init':
                    SharedInfrastructure(self.ctrl, self.tmp).create_directory_structure()
                elif layout == 'missing-release':
                    os.symlink('absent-release', os.path.join(self.tmp, 'content', 'current'))
                with self._environment() as (stack, logs):
                    stack.enter_context(mock.patch.object(mt.WODomain, 'validate', return_value='example.com'))
                    stack.enter_context(mock.patch.object(mt, 'check_domain_exists', return_value=False))
                    provision = stack.enter_context(mock.patch.object(mt, 'site_package_check'))
                    directories = stack.enter_context(mock.patch.object(mt.MTFunctions, 'create_site_directories'))
                    database = stack.enter_context(mock.patch.object(mt, 'setupdatabase'))
                    self.ctrl._create_impl()
                provision.assert_not_called()
                directories.assert_not_called()
                database.assert_not_called()
                self.assertTrue(any(
                    'No content release is live' in call.args[1]
                    and 'tools/deploy' in call.args[1]
                    for call in logs['error'].call_args_list
                ))

    def test_create_accepts_old_folder_or_deployed_content(self):
        for layout in ('old-folder', 'deployed'):
            with self.subTest(layout=layout):
                shared_root = os.path.join(self.tmp, layout)
                self.config['shared_root'] = shared_root
                if layout == 'old-folder':
                    os.makedirs(os.path.join(shared_root, 'wp-content'))
                else:
                    SharedInfrastructure(self.ctrl, shared_root).create_directory_structure()
                    os.makedirs(os.path.join(shared_root, 'content', 'live', 'wp-content'))
                    os.symlink('live', os.path.join(shared_root, 'content', 'current'))
                with self._environment() as (stack, logs):
                    stack.enter_context(mock.patch.object(mt.WODomain, 'validate', return_value='example.com'))
                    stack.enter_context(mock.patch.object(mt, 'check_domain_exists', return_value=False))
                    stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_php_version', return_value='8.4'))
                    stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_cache_type', return_value='wpfc'))
                    provision = stack.enter_context(mock.patch.object(
                        mt, 'site_package_check', side_effect=RuntimeError('provisioning reached'),
                    ))
                    with self.assertRaisesRegex(RuntimeError, 'provisioning reached'):
                        self.ctrl._create_impl()
                provision.assert_called_once_with(self.ctrl, 'wp')
                logs['error'].assert_not_called()

    def test_init_changes_only_core_infrastructure_without_login_renderer(self):
        with self._environment() as (stack, logs):
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=False))
            infra = stack.enter_context(mock.patch.object(mt, 'SharedInfrastructure')).return_value
            infra.download_wordpress_core.return_value = 'wp-new'
            stack.enter_context(mock.patch.object(mt, 'create_shared_config_file', return_value=True))
            stack.enter_context(mock.patch.object(mt, 'ReleaseManager'))
            record = stack.enter_context(mock.patch.object(mt.MTDatabase, 'update_release', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'save_config', return_value=True))
            login = stack.enter_context(mock.patch.object(mt.MTFunctions, 'install_login_failure_logger'))
            self.ctrl.init()
        self.assertEqual(infra.method_calls, [
            mock.call.create_directory_structure(),
            mock.call.download_wordpress_core(None),
            mock.call.switch_release('wp-new'),
            mock.call.set_permissions(),
        ])
        record.assert_called_once_with(self.ctrl, 'wp-new')
        login.assert_not_called()

    def test_apply_passes_flags_without_version_or_login_renderer(self):
        self.ctrl.app.pargs.verbose = True
        self.ctrl.app.pargs.prune = True
        with self._environment() as (stack, logs):
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'get_shared_sites', return_value=[]))
            apply = stack.enter_context(mock.patch.object(
                mt.BaselineApplicator, 'apply_baseline_to_sites',
                return_value={'attempted': 0, 'succeeded': 0, 'failed': 0},
            ))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'sync_wp_cron_entries', return_value=True))
            stack.enter_context(mock.patch.object(mt, 'repair_backup_cron'))
            login = stack.enter_context(mock.patch.object(mt.MTFunctions, 'install_login_failure_logger'))
            self.ctrl._apply_impl()
        apply.assert_called_once_with(
            self.ctrl, self.config, dry_run=False, verbose=True, prune=True,
        )
        login.assert_not_called()

    def _status_messages(self):
        with self._environment() as (stack, logs):
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'get_current_release', return_value='wp-core'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'get_shared_sites', return_value=[]))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'perform_health_check', return_value={}))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'calculate_disk_usage', return_value={}))
            manager = stack.enter_context(mock.patch.object(mt, 'ReleaseManager')).return_value
            manager.list_releases.return_value = ['wp-core']
            self.ctrl.status()
        return [call.args[1] for call in logs['info'].call_args_list]

    def test_status_reports_deployed_commit_from_live_release(self):
        SharedInfrastructure(self.ctrl, self.tmp).create_directory_structure()
        release = os.path.join(self.tmp, 'content', '20261025-120000-a1b2c3d')
        os.makedirs(os.path.join(release, 'wp-content'))
        os.symlink(os.path.basename(release), os.path.join(self.tmp, 'content', 'current'))
        commit = 'a1b2c3d' + '0' * 33
        with open(os.path.join(release, 'RELEASE'), 'w') as fh:
            json.dump({'commit': commit}, fh)
        with open(os.path.join(release, 'baseline.json'), 'w') as fh:
            json.dump({'version': 12, 'plugins': ['shop'], 'theme': 'store'}, fh)
        messages = self._status_messages()
        self.assertIn('  Deployed commit: ' + commit, messages)
        self.assertIn('  Plugins: shop', messages)
        self.assertIn('  Theme: store', messages)
        self.assertFalse(any('baseline version' in message.lower() for message in messages))

    def test_status_identifies_old_layout_without_baseline_version(self):
        os.makedirs(os.path.join(self.tmp, 'wp-content'))
        os.makedirs(os.path.join(self.tmp, 'config'))
        with open(os.path.join(self.tmp, 'config', 'baseline.json'), 'w') as fh:
            json.dump({'version': 12, 'plugins': [], 'theme': ''}, fh)
        messages = self._status_messages()
        self.assertIn('  Deployed commit: old layout, not cut over', messages)
        self.assertFalse(any('baseline version' in message.lower() for message in messages))

    def test_parser_help_omits_removed_commands_and_options(self):
        from cement.core.foundation import CementApp

        class BaseController(mt.CementBaseController):
            class Meta:
                label = 'base'

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with CementApp(
                'mt-help-test', argv=['multitenancy', '--help'],
                handlers=[BaseController, mt.WOMultitenancyController],
                config_files=[], exit_on_close=False,
            ) as app:
                with self.assertRaises(SystemExit) as exited:
                    app.run()
        self.assertEqual(exited.exception.code, 0)
        help_text = output.getvalue()
        for command in (
            'add-plugin', 'add-theme', 'remove-plugin', 'update-plugin',
            'update-theme', 'set-theme', 'history', 'baseline-rollback',
        ):
            self.assertNotIn(command, help_text)
            self.assertFalse(hasattr(mt.WOMultitenancyController, command.replace('-', '_')))
        for option in (
            '--github', '--branch', '--tag', '--url', '--apply-now',
            '--set-default', '--to-version',
        ):
            self.assertNotIn(option, help_text)
        for retained in ('update', 'apply', 'baseline', '--prune', '--dry-run'):
            self.assertIn(retained, help_text)


class BaselineApplicatorTests(unittest.TestCase):

    def setUp(self):
        self.app = mock.Mock()
        self.site_path = '/var/www/example.com/htdocs'

    def _wp_result(self, returncode=0, stdout='', stderr=''):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def _assert_nginx_helper_update(self, run, expected_cache_method):
        run.assert_called_once()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], [
            'wp', 'option', 'update', 'rt_wp_nginx_helper_options',
        ])
        self.assertEqual(cmd[5:], [
            '--path=' + self.site_path, '--allow-root', '--format=json',
        ])
        payload = json.loads(cmd[4])
        self.assertEqual(payload['enable_purge'], 1)
        self.assertEqual(payload['cache_method'], expected_cache_method)
        self.assertEqual(payload['purge_method'], 'get_request')

    def _nginx_helper_update_commands(self, run):
        return [
            call.args[0] for call in run.call_args_list
            if call.args[0][:4] == [
                'wp', 'option', 'update', 'rt_wp_nginx_helper_options',
            ]
        ]

    def _nginx_helper_cap_commands(self, run):
        return [
            call.args[0] for call in run.call_args_list
            if call.args[0][:3] == ['wp', 'cap', 'add']
        ]

    def _make_plugin_site(self):
        site_path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, site_path, ignore_errors=True)
        plugins_root = os.path.join(site_path, 'wp-content', 'plugins')
        os.makedirs(plugins_root)
        return site_path, plugins_root

    def test_find_plugin_main_file_falls_back_to_header_scan(self):
        """A non-conventional PHP file with a plugin header is selected."""
        site_path, plugins_root = self._make_plugin_site()
        plugin_dir = os.path.join(plugins_root, 'moyasar')
        os.makedirs(plugin_dir)
        with open(os.path.join(plugin_dir, 'moyasar-payments.php'), 'w') as fh:
            fh.write("<?php\n/*\nPlugin Name: Moyasar\n*/\n")

        self.assertEqual(
            BaselineApplicator.find_plugin_main_file(site_path, 'moyasar'),
            'moyasar/moyasar-payments.php',
        )

    def test_find_plugin_main_file_prefers_conventional_slug_file(self):
        """The conventional <slug>/<slug>.php path wins over fallback scan."""
        site_path, plugins_root = self._make_plugin_site()
        plugin_dir = os.path.join(plugins_root, 'sample-plugin')
        os.makedirs(plugin_dir)
        with open(os.path.join(plugin_dir, 'a-header.php'), 'w') as fh:
            fh.write("<?php\n/*\nPlugin Name: Header Candidate\n*/\n")
        with open(os.path.join(plugin_dir, 'sample-plugin.php'), 'w') as fh:
            fh.write("<?php\n/*\nPlugin Name: Sample Plugin\n*/\n")

        self.assertEqual(
            BaselineApplicator.find_plugin_main_file(site_path, 'sample-plugin'),
            'sample-plugin/sample-plugin.php',
        )

    def test_find_plugin_main_file_returns_root_single_file_plugin(self):
        """A single-file plugin directly under plugins/ resolves to <slug>.php."""
        site_path, plugins_root = self._make_plugin_site()
        with open(os.path.join(plugins_root, 'root-plugin.php'), 'w') as fh:
            fh.write("<?php\n/*\nPlugin Name: Root Plugin\n*/\n")

        self.assertEqual(
            BaselineApplicator.find_plugin_main_file(site_path, 'root-plugin'),
            'root-plugin.php',
        )

    def test_find_plugin_main_file_ignores_headerless_index_stub(self):
        """Headerless index.php stubs are ignored during fallback scan."""
        site_path, plugins_root = self._make_plugin_site()
        plugin_dir = os.path.join(plugins_root, 'madfu-payment-gateway')
        os.makedirs(plugin_dir)
        with open(os.path.join(plugin_dir, 'index.php'), 'w'):
            pass
        with open(os.path.join(plugin_dir, 'madfu-pay.php'), 'w') as fh:
            fh.write("<?php\n/*\nPlugin Name: Madfu Payment Gateway\n*/\n")

        self.assertEqual(
            BaselineApplicator.find_plugin_main_file(
                site_path, 'madfu-payment-gateway'
            ),
            'madfu-payment-gateway/madfu-pay.php',
        )

        index_only_dir = os.path.join(plugins_root, 'index-only-plugin')
        os.makedirs(index_only_dir)
        with open(os.path.join(index_only_dir, 'index.php'), 'w'):
            pass

        self.assertIsNone(
            BaselineApplicator.find_plugin_main_file(site_path, 'index-only-plugin')
        )

    def test_find_plugin_main_file_returns_none_for_absent_plugin_dir(self):
        """Missing plugin directories are reported as absent."""
        site_path, _plugins_root = self._make_plugin_site()

        self.assertIsNone(
            BaselineApplicator.find_plugin_main_file(site_path, 'missing-plugin')
        )

    def test_find_plugin_main_file_returns_none_without_plugin_header(self):
        """Fallback scan ignores PHP files that do not declare Plugin Name."""
        site_path, plugins_root = self._make_plugin_site()
        plugin_dir = os.path.join(plugins_root, 'headerless-plugin')
        os.makedirs(plugin_dir)
        with open(os.path.join(plugin_dir, 'custom.php'), 'w') as fh:
            fh.write("<?php\n// not a WordPress plugin header\n")

        self.assertIsNone(
            BaselineApplicator.find_plugin_main_file(site_path, 'headerless-plugin')
        )


    def test_ensure_nginx_helper_caps_grants_required_administrator_caps(self):
        """Nginx Helper caps are granted through exact WP-CLI cap-add calls."""
        expected_commands = [
            [
                'wp', 'cap', 'add', 'administrator',
                'Nginx Helper | Purge cache',
                '--path=' + self.site_path, '--allow-root',
            ],
            [
                'wp', 'cap', 'add', 'administrator',
                'Nginx Helper | Config',
                '--path=' + self.site_path, '--allow-root',
            ],
        ]

        with mock.patch.object(
                mtf.subprocess,
                'run',
                return_value=self._wp_result(),
        ) as run, \
                mock.patch('wo.core.logging.Log.debug'):
            BaselineApplicator.ensure_nginx_helper_caps(
                self.app, 'example.com', self.site_path
            )

        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    expected_commands[0],
                    capture_output=True,
                    text=True,
                    timeout=BaselineApplicator.WP_CLI_TIMEOUT,
                ),
                mock.call(
                    expected_commands[1],
                    capture_output=True,
                    text=True,
                    timeout=BaselineApplicator.WP_CLI_TIMEOUT,
                ),
            ],
        )

    def test_ensure_nginx_helper_caps_warns_and_continues_on_failed_cap_add(self):
        """Rejected cap grants warn but do not abort remaining Nginx Helper caps."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                side_effect=[
                    self._wp_result(returncode=1, stderr='denied'),
                    self._wp_result(),
                ],
        ) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            BaselineApplicator.ensure_nginx_helper_caps(
                self.app, 'example.com', self.site_path
            )

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                [
                    'wp', 'cap', 'add', 'administrator',
                    'Nginx Helper | Purge cache',
                    '--path=' + self.site_path, '--allow-root',
                ],
                [
                    'wp', 'cap', 'add', 'administrator',
                    'Nginx Helper | Config',
                    '--path=' + self.site_path, '--allow-root',
                ],
            ],
        )
        log_warn.assert_called_once()

    def test_ensure_nginx_helper_caps_warns_and_continues_on_timeout(self):
        """WP-CLI cap-add exceptions are warn-only and the second cap is still tried."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                side_effect=mtf.subprocess.TimeoutExpired('wp', 30),
        ) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            BaselineApplicator.ensure_nginx_helper_caps(
                self.app, 'example.com', self.site_path
            )

        self.assertEqual(run.call_count, 2)
        self.assertEqual(log_warn.call_count, 2)

    def test_ensure_nginx_helper_caps_propagates_unexpected_subprocess_errors(self):
        """Unexpected subprocess errors must not be hidden by cap grant warnings."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                side_effect=ValueError('boom'),
        ):
            with self.assertRaises(ValueError):
                BaselineApplicator.ensure_nginx_helper_caps(
                    self.app, 'example.com', self.site_path
                )


    def test_configure_nginx_helper_wpfc_updates_purge_options(self):
        """FastCGI tenants get Nginx Helper purge enabled via WP-CLI JSON."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                return_value=self._wp_result(),
        ) as run, \
                mock.patch('wo.core.logging.Log.debug'):
            BaselineApplicator.configure_nginx_helper(
                self.app, 'example.com', self.site_path, 'wpfc'
            )

        self._assert_nginx_helper_update(run, 'enable_fastcgi')

    def test_configure_nginx_helper_wpredis_uses_redis_cache_method(self):
        """Redis tenants select the Redis Nginx Helper cache backend."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                return_value=self._wp_result(),
        ) as run, \
                mock.patch('wo.core.logging.Log.debug'):
            BaselineApplicator.configure_nginx_helper(
                self.app, 'example.com', self.site_path, 'wpredis'
            )

        self._assert_nginx_helper_update(run, 'enable_redis')

    def test_configure_nginx_helper_skips_non_nginx_helper_cache_types(self):
        """Cache types that do not purge through Nginx Helper do no WP-CLI work."""
        for cache_type in ('basic', 'wprocket', None):
            with self.subTest(cache_type=cache_type), \
                    mock.patch.object(mtf.subprocess, 'run') as run:
                BaselineApplicator.configure_nginx_helper(
                    self.app, 'example.com', self.site_path, cache_type
                )

            run.assert_not_called()

    def test_configure_nginx_helper_warns_and_returns_on_failed_wp_cli_update(self):
        """A rejected helper option update warns but does not raise."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                return_value=self._wp_result(returncode=1, stderr='rejected'),
        ), \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            BaselineApplicator.configure_nginx_helper(
                self.app, 'example.com', self.site_path, 'wpfc'
            )

        log_warn.assert_called_once()

    def test_configure_nginx_helper_catches_timeout_and_warns(self):
        """WP-CLI timeout while seeding helper options is warn-only."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                side_effect=mtf.subprocess.TimeoutExpired('wp', 30),
        ), \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            BaselineApplicator.configure_nginx_helper(
                self.app, 'example.com', self.site_path, 'wpfc'
            )

        log_warn.assert_called_once()

    def test_configure_nginx_helper_propagates_unexpected_subprocess_errors(self):
        """Unexpected subprocess errors must not be hidden while configuring purge."""
        with mock.patch.object(
                mtf.subprocess,
                'run',
                side_effect=ValueError('boom'),
        ):
            with self.assertRaises(ValueError):
                BaselineApplicator.configure_nginx_helper(
                    self.app, 'example.com', self.site_path, 'wpfc'
                )

    def test_apply_baseline_to_site_configures_nginx_helper_for_wpfc_baseline(self):
        """Applying a wpfc baseline with nginx-helper enables purge and stays successful."""
        baseline = {
            'plugins': ['nginx-helper'],
            'theme': '',
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:4] == [
                    'wp', 'option', 'update', 'rt_wp_nginx_helper_options',
            ]:
                return self._wp_result()
            if cmd[:4] == ['wp', 'cap', 'add', 'administrator']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value='nginx-helper/nginx-helper.php'), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.debug'), \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_site(
                self.app,
                'example.com',
                self.site_path,
                baseline,
                cache_type='wpfc',
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )
        nginx_helper_commands = self._nginx_helper_update_commands(run)
        self.assertEqual(len(nginx_helper_commands), 1)
        payload = json.loads(nginx_helper_commands[0][4])
        self.assertEqual(payload['enable_purge'], 1)
        self.assertEqual(payload['cache_method'], 'enable_fastcgi')
        self.assertEqual(payload['purge_method'], 'get_request')
        self.assertEqual(self._nginx_helper_cap_commands(run), [
            [
                'wp', 'cap', 'add', 'administrator',
                'Nginx Helper | Purge cache',
                '--path=' + self.site_path, '--allow-root',
            ],
            [
                'wp', 'cap', 'add', 'administrator',
                'Nginx Helper | Config',
                '--path=' + self.site_path, '--allow-root',
            ],
        ])

    def test_apply_baseline_to_site_nginx_helper_gates_options_and_caps_separately(self):
        """Option updates require plugin plus cache type; caps require only the plugin."""
        cases = [
            ('plugin_absent', {'plugins': ['kept-plugin'], 'theme': '', 'options': {}}, 'wpfc'),
            ('cache_type_missing', {'plugins': ['nginx-helper'], 'theme': '', 'options': {}}, None),
        ]

        for name, baseline, cache_type in cases:
            with self.subTest(name=name):
                def run_wp(cmd, **kwargs):
                    if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                        return self._wp_result(stdout='[]')
                    if cmd[:3] == ['wp', 'plugin', 'activate']:
                        return self._wp_result()
                    if cmd[:4] == [
                            'wp', 'option', 'update', 'rt_wp_nginx_helper_options',
                    ]:
                        self.fail('nginx-helper purge must not be configured')
                    if cmd[:4] == ['wp', 'cap', 'add', 'administrator']:
                        return self._wp_result()
                    self.fail(f'unexpected wp command: {cmd!r}')

                with mock.patch.object(
                        BaselineApplicator,
                        'find_plugin_main_file',
                        side_effect=lambda site_path, slug: f'{slug}/{slug}.php',
                ), \
                        mock.patch.object(mtf.subprocess, 'run',
                                          side_effect=run_wp) as run:
                    result = BaselineApplicator.apply_baseline_to_site(
                        self.app,
                        'example.com',
                        self.site_path,
                        baseline,
                        cache_type=cache_type,
                    )

                self.assertEqual(
                    result,
                    {'success': True, 'error': None, 'skipped_plugins': []},
                )
                self.assertEqual(self._nginx_helper_update_commands(run), [])
                if name == 'plugin_absent':
                    self.assertEqual(self._nginx_helper_cap_commands(run), [])
                else:
                    self.assertEqual(self._nginx_helper_cap_commands(run), [
                        [
                            'wp', 'cap', 'add', 'administrator',
                            'Nginx Helper | Purge cache',
                            '--path=' + self.site_path, '--allow-root',
                        ],
                        [
                            'wp', 'cap', 'add', 'administrator',
                            'Nginx Helper | Config',
                            '--path=' + self.site_path, '--allow-root',
                        ],
                    ])

    def test_apply_baseline_to_site_success_when_nginx_helper_update_fails(self):
        """A failed helper option update warns but does not fail the baseline apply."""
        baseline = {
            'plugins': ['nginx-helper'],
            'theme': '',
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:4] == [
                    'wp', 'option', 'update', 'rt_wp_nginx_helper_options',
            ]:
                return self._wp_result(returncode=1, stderr='rejected')
            if cmd[:4] == ['wp', 'cap', 'add', 'administrator']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value='nginx-helper/nginx-helper.php'), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp), \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            result = BaselineApplicator.apply_baseline_to_site(
                self.app,
                'example.com',
                self.site_path,
                baseline,
                cache_type='wpfc',
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )
        self.assertTrue(log_warn.called)

    def test_apply_baseline_to_site_updates_options_and_continues_after_option_failure(self):
        """options are serialized for WP-CLI; one failed option only logs and does not roll back."""
        baseline = {
            'plugins': ['kept-plugin'],
            'theme': 'baseline-theme',
            'options': {
                'blog_public': 1,
                'comments_allowed': False,
                'default_comment_status': 'closed',
                'broken_option': 'still-try-later-options',
                'structured_option': {'nested': ['value']},
            },
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='["old-plugin/old-plugin.php"]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'theme', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'option', 'update']:
                if cmd[3] == 'broken_option':
                    return self._wp_result(returncode=1, stderr='option rejected')
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value='kept-plugin/kept-plugin.php'), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )
        self.assertTrue(log_warn.called)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn([
            'wp', 'plugin', 'activate', 'kept-plugin/kept-plugin.php',
            '--path=' + self.site_path, '--allow-root',
        ], commands)
        self.assertEqual([
            cmd for cmd in commands
            if cmd[:3] == ['wp', 'option', 'update']
        ], [
            [
                'wp', 'option', 'update', 'blog_public', '1',
                '--path=' + self.site_path, '--allow-root',
            ],
            [
                'wp', 'option', 'update', 'comments_allowed', '0',
                '--path=' + self.site_path, '--allow-root',
            ],
            [
                'wp', 'option', 'update', 'default_comment_status', 'closed',
                '--path=' + self.site_path, '--allow-root',
            ],
            [
                'wp', 'option', 'update', 'broken_option',
                'still-try-later-options',
                '--path=' + self.site_path, '--allow-root',
            ],
            [
                'wp', 'option', 'update', 'structured_option',
                json.dumps({'nested': ['value']}),
                '--path=' + self.site_path, '--allow-root', '--format=json',
            ],
        ])

    def test_apply_baseline_to_site_skips_missing_plugin_and_continues(self):
        """A missing plugin is reported but does not abort later baseline work."""
        baseline = {
            'plugins': ['good-plugin', 'missing-plugin', 'after-plugin'],
            'theme': 'baseline-theme',
            'options': {'blogname': 'Tenant Site'},
        }
        plugin_files = {
            'good-plugin': 'good-plugin/good-plugin.php',
            'missing-plugin': None,
            'after-plugin': 'after-plugin/after-plugin.php',
        }

        def find_plugin(site_path, slug):
            return plugin_files[slug]

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'theme', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'option', 'update']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               side_effect=find_plugin), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline
            )

        self.assertTrue(result['success'])
        self.assertIsNone(result['error'])
        self.assertEqual(result['skipped_plugins'], ['missing-plugin'])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn([
            'wp', 'plugin', 'activate', 'good-plugin/good-plugin.php',
            '--path=' + self.site_path, '--allow-root',
        ], commands)
        self.assertIn([
            'wp', 'plugin', 'activate', 'after-plugin/after-plugin.php',
            '--path=' + self.site_path, '--allow-root',
        ], commands)
        self.assertIn([
            'wp', 'theme', 'activate', 'baseline-theme',
            '--path=' + self.site_path, '--allow-root',
        ], commands)
        self.assertTrue(log_warn.called)

    def test_apply_baseline_to_site_skips_failed_plugin_activation_and_continues(self):
        """A plugin activation failure skips that slug and activates later plugins."""
        baseline = {
            'plugins': ['bad-plugin', 'after-plugin'],
            'theme': '',
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                if cmd[3] == 'bad-plugin/bad-plugin.php':
                    return self._wp_result(returncode=1, stderr='activation failed')
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(
                BaselineApplicator,
                'find_plugin_main_file',
                side_effect=lambda site_path, slug: f'{slug}/{slug}.php',
        ), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline
            )

        self.assertTrue(result['success'])
        self.assertIsNone(result['error'])
        self.assertEqual(result['skipped_plugins'], ['bad-plugin'])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn([
            'wp', 'plugin', 'activate', 'after-plugin/after-plugin.php',
            '--path=' + self.site_path, '--allow-root',
        ], commands)
        self.assertTrue(log_warn.called)

    def test_apply_baseline_to_site_skips_plugin_on_activation_timeout(self):
        """A timed-out plugin activation skips that plugin and continues."""
        baseline = {
            'plugins': ['slow-plugin', 'after-plugin'],
            'theme': 'baseline-theme',
            'options': {'blogname': 'Tenant Site'},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                if cmd[3] == 'slow-plugin/slow-plugin.php':
                    raise mtf.subprocess.TimeoutExpired(
                        cmd,
                        BaselineApplicator.WP_CLI_TIMEOUT,
                    )
                return self._wp_result()
            if cmd[:3] == ['wp', 'option', 'update']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'theme', 'activate']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(
                BaselineApplicator,
                'find_plugin_main_file',
                side_effect=lambda site_path, slug: f'{slug}/{slug}.php',
        ), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline
            )

        self.assertTrue(result['success'])
        self.assertIsNone(result['error'])
        self.assertEqual(result['skipped_plugins'], ['slow-plugin'])
        commands = [call.args[0] for call in run.call_args_list]
        slow_plugin_cmd = [
            'wp', 'plugin', 'activate', 'slow-plugin/slow-plugin.php',
            '--path=' + self.site_path, '--allow-root',
        ]
        after_plugin_cmd = [
            'wp', 'plugin', 'activate', 'after-plugin/after-plugin.php',
            '--path=' + self.site_path, '--allow-root',
        ]
        self.assertIn(slow_plugin_cmd, commands)
        self.assertIn(after_plugin_cmd, commands)
        self.assertLess(
            commands.index(slow_plugin_cmd),
            commands.index(after_plugin_cmd),
        )
        self.assertTrue(log_warn.called)

    def test_apply_baseline_to_site_theme_failure_is_fatal_after_options(self):
        """Theme activation fails last, after plugins and options were applied."""
        baseline = {
            'plugins': ['good-plugin'],
            'theme': 'missing-theme',
            'options': {'blog_public': 0},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'option', 'update']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'theme', 'activate']:
                return self._wp_result(returncode=1, stderr='theme missing')
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value='good-plugin/good-plugin.php'), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline
            )

        self.assertFalse(result['success'])
        self.assertIn('Failed to activate baseline theme', result['error'])
        self.assertIn('missing-theme', result['error'])
        self.assertEqual(result['skipped_plugins'], [])
        commands = [call.args[0] for call in run.call_args_list]
        plugin_cmd = [
            'wp', 'plugin', 'activate', 'good-plugin/good-plugin.php',
            '--path=' + self.site_path, '--allow-root',
        ]
        option_cmd = [
            'wp', 'option', 'update', 'blog_public', '0',
            '--path=' + self.site_path, '--allow-root',
        ]
        theme_cmd = [
            'wp', 'theme', 'activate', 'missing-theme',
            '--path=' + self.site_path, '--allow-root',
        ]
        self.assertIn(plugin_cmd, commands)
        self.assertIn(option_cmd, commands)
        self.assertLess(commands.index(option_cmd), commands.index(theme_cmd))
        self.assertTrue(log_warn.called)

    def test_apply_baseline_to_site_theme_activated_last(self):
        """Baseline theme activation is ordered after options and cache config."""
        baseline = {
            'plugins': ['good-plugin', 'nginx-helper'],
            'theme': 'baseline-theme',
            'options': {'blog_public': 1},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'option', 'update']:
                return self._wp_result()
            if cmd[:4] == ['wp', 'cap', 'add', 'administrator']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'theme', 'activate']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(
                BaselineApplicator,
                'find_plugin_main_file',
                side_effect=lambda site_path, slug: f'{slug}/{slug}.php',
        ), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.debug'), \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_site(
                self.app,
                'example.com',
                self.site_path,
                baseline,
                cache_type='wpfc',
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )
        commands = [call.args[0] for call in run.call_args_list]
        theme_index = next(
            index for index, cmd in enumerate(commands)
            if cmd[:3] == ['wp', 'theme', 'activate']
        )
        option_indices = [
            index for index, cmd in enumerate(commands)
            if cmd[:4] == ['wp', 'option', 'update', 'blog_public']
        ]
        nginx_helper_index = next(
            index for index, cmd in enumerate(commands)
            if cmd[:4] == [
                'wp', 'option', 'update', 'rt_wp_nginx_helper_options',
            ]
        )

        self.assertEqual(len(option_indices), 1)
        self.assertLess(option_indices[0], theme_index)
        self.assertLess(nginx_helper_index, theme_index)

    def test_apply_baseline_to_site_theme_success_marks_fully_applied(self):
        """All plugins found plus theme success returns the fully-applied result."""
        baseline = {
            'plugins': ['good-plugin'],
            'theme': 'baseline-theme',
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'theme', 'activate']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value='good-plugin/good-plugin.php'), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp):
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )

    def test_apply_baseline_to_site_result_flags_preserve_full_apply_rule(self):
        """Callers can reject fatal themes and skipped plugins as incomplete."""
        theme_failure_baseline = {
            'plugins': [],
            'theme': 'missing-theme',
            'options': {},
        }

        def run_theme_failure_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'theme', 'activate']:
                return self._wp_result(returncode=1, stderr='theme missing')
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(mtf.subprocess, 'run',
                               side_effect=run_theme_failure_wp), \
                mock.patch('wo.core.logging.Log.warn'):
            theme_result = BaselineApplicator.apply_baseline_to_site(
                self.app,
                'example.com',
                self.site_path,
                theme_failure_baseline,
            )

        skipped_plugin_baseline = {
            'plugins': ['missing-plugin'],
            'theme': '',
            'options': {},
        }

        def run_skipped_plugin_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value=None), \
                mock.patch.object(mtf.subprocess, 'run',
                                  side_effect=run_skipped_plugin_wp), \
                mock.patch('wo.core.logging.Log.warn'):
            skipped_plugin_result = BaselineApplicator.apply_baseline_to_site(
                self.app,
                'example.com',
                self.site_path,
                skipped_plugin_baseline,
            )

        self.assertFalse(theme_result['success'])
        self.assertFalse(
            theme_result['success'] and not theme_result['skipped_plugins']
        )
        self.assertTrue(skipped_plugin_result['success'])
        self.assertEqual(
            skipped_plugin_result['skipped_plugins'],
            ['missing-plugin'],
        )
        self.assertFalse(
            skipped_plugin_result['success']
            and not skipped_plugin_result['skipped_plugins']
        )

    def test_apply_baseline_to_site_prune_deactivates_exact_active_unlisted_set(self):
        """prune removes only plugins active on the site and absent from baseline plugins."""
        baseline = {
            'plugins': ['kept-plugin'],
            'theme': '',
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:4] == ['wp', 'plugin', 'list', '--status=active']:
                return self._wp_result(stdout='kept-plugin\nextra-b\nextra-a\n')
            if cmd[:3] == ['wp', 'plugin', 'deactivate']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value='kept-plugin/kept-plugin.php'), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.info'):
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline, prune=True
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )
        deactivate_commands = [
            call.args[0] for call in run.call_args_list
            if call.args[0][:3] == ['wp', 'plugin', 'deactivate']
        ]
        self.assertEqual(deactivate_commands, [[
            'wp', 'plugin', 'deactivate',
            'extra-a', 'extra-b',
            '--path=' + self.site_path,
            '--allow-root',
        ]])

    def test_apply_baseline_to_site_without_prune_never_lists_or_deactivates_extras(self):
        """prune=False leaves non-baseline active plugins untouched."""
        baseline = {
            'plugins': ['kept-plugin'],
            'theme': '',
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'plugin', 'list']:
                self.fail('prune=False must not inspect active plugin extras')
            if cmd[:3] == ['wp', 'plugin', 'deactivate']:
                self.fail('prune=False must not deactivate plugin extras')
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                               return_value='kept-plugin/kept-plugin.php'), \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp):
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline, prune=False
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )



class ObjectCacheDropinTests(unittest.TestCase):

    def setUp(self):
        self.app = mock.Mock()
        self.domain = 'example.com'
        self.site_path = '/var/www/example.com/htdocs'
        self.dropin = os.path.join(
            self.site_path, 'wp-content', 'object-cache.php'
        )

    def _wp_result(self, returncode=0, stdout='', stderr=''):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def _enable_cmd(self):
        return [
            'wp', 'redis', 'enable', '--force',
            '--skip-flush', '--skip-flush-notice',
            '--path=' + self.site_path,
            '--allow-root',
        ]

    def test_gate_skips_when_ocp_not_in_baseline(self):
        baseline = {'plugins': ['redis-cache'], 'theme': None, 'options': {}}

        with mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run') as run, \
                mock.patch('wo.cli.plugins.multitenancy_functions.shutil.chown') as chown, \
                mock.patch('wo.cli.plugins.multitenancy_functions.os.path.exists') as exists, \
                mock.patch.object(BaselineApplicator, 'find_plugin_main_file'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            BaselineApplicator.enable_object_cache_dropin(
                self.app, self.domain, self.site_path, baseline
            )

        run.assert_not_called()
        chown.assert_not_called()
        exists.assert_not_called()

    def test_enable_happy_path_runs_force_command_and_chowns(self):
        baseline = {'plugins': ['object-cache-pro'], 'theme': None, 'options': {}}

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                return_value=self._wp_result(),
        ) as run, \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.shutil.chown'
                ) as chown, \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.os.path.exists',
                    return_value=True,
                ), \
                mock.patch.object(BaselineApplicator, 'find_plugin_main_file'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            BaselineApplicator.enable_object_cache_dropin(
                self.app, self.domain, self.site_path, baseline
            )

        run.assert_called_once_with(
            self._enable_cmd(), capture_output=True, text=True,
            timeout=BaselineApplicator.WP_CLI_TIMEOUT,
        )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ['wp', 'redis', 'enable'])
        self.assertIn('--force', argv)
        chown.assert_called_once_with(
            self.dropin, user='www-data', group='www-data'
        )

    def test_enable_nonzero_returncode_is_nonfatal(self):
        baseline = {'plugins': ['object-cache-pro'], 'theme': None, 'options': {}}

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                return_value=self._wp_result(returncode=1, stderr='failed'),
        ), \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.shutil.chown'
                ) as chown, \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.os.path.exists',
                    return_value=True,
                ), \
                mock.patch.object(BaselineApplicator, 'find_plugin_main_file'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            BaselineApplicator.enable_object_cache_dropin(
                self.app, self.domain, self.site_path, baseline
            )

        chown.assert_not_called()

    def test_enable_missing_dropin_is_nonfatal(self):
        baseline = {'plugins': ['object-cache-pro'], 'theme': None, 'options': {}}

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                return_value=self._wp_result(),
        ), \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.shutil.chown'
                ) as chown, \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.os.path.exists',
                    return_value=False,
                ), \
                mock.patch.object(BaselineApplicator, 'find_plugin_main_file'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            BaselineApplicator.enable_object_cache_dropin(
                self.app, self.domain, self.site_path, baseline
            )

        chown.assert_not_called()

    def test_chown_failure_is_nonfatal(self):
        baseline = {'plugins': ['object-cache-pro'], 'theme': None, 'options': {}}

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                return_value=self._wp_result(),
        ), \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.shutil.chown',
                    side_effect=OSError('permission denied'),
                ), \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.os.path.exists',
                    return_value=True,
                ), \
                mock.patch.object(BaselineApplicator, 'find_plugin_main_file'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            BaselineApplicator.enable_object_cache_dropin(
                self.app, self.domain, self.site_path, baseline
            )

    def test_subprocess_exception_is_nonfatal(self):
        baseline = {'plugins': ['object-cache-pro'], 'theme': None, 'options': {}}

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                side_effect=OSError('wp failed'),
        ), \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.shutil.chown'
                ) as chown, \
                mock.patch('wo.cli.plugins.multitenancy_functions.os.path.exists'), \
                mock.patch.object(BaselineApplicator, 'find_plugin_main_file'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            BaselineApplicator.enable_object_cache_dropin(
                self.app, self.domain, self.site_path, baseline
            )

        chown.assert_not_called()

    def test_apply_baseline_invokes_dropin_enable_with_force(self):
        baseline = {
            'plugins': ['object-cache-pro'],
            'theme': None,
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'redis', 'enable']:
                return self._wp_result()
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                side_effect=run_wp,
        ) as run, \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.shutil.chown'
                ), \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.os.path.exists',
                    return_value=True,
                ), \
                mock.patch.object(
                    BaselineApplicator,
                    'find_plugin_main_file',
                    return_value='object-cache-pro/object-cache-pro.php',
                ), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'x.com', self.site_path, baseline, prune=False
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(self._enable_cmd(), commands)

    def test_apply_baseline_success_even_if_dropin_enable_fails(self):
        baseline = {
            'plugins': ['object-cache-pro'],
            'theme': None,
            'options': {},
        }

        def run_wp(cmd, **kwargs):
            if cmd[:4] == ['wp', 'option', 'get', 'active_plugins']:
                return self._wp_result(stdout='[]')
            if cmd[:3] == ['wp', 'plugin', 'activate']:
                return self._wp_result()
            if cmd[:3] == ['wp', 'redis', 'enable']:
                return self._wp_result(returncode=1, stderr='redis failed')
            self.fail(f'unexpected wp command: {cmd!r}')

        with mock.patch(
                'wo.cli.plugins.multitenancy_functions.subprocess.run',
                side_effect=run_wp,
        ), \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.shutil.chown'
                ) as chown, \
                mock.patch(
                    'wo.cli.plugins.multitenancy_functions.os.path.exists',
                    return_value=True,
                ), \
                mock.patch.object(
                    BaselineApplicator,
                    'find_plugin_main_file',
                    return_value='object-cache-pro/object-cache-pro.php',
                ), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.warn'), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.info'):
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'x.com', self.site_path, baseline, prune=False
            )

        self.assertEqual(
            result,
            {'success': True, 'error': None, 'skipped_plugins': []},
        )
        chown.assert_not_called()
class BaselineApplicatorSitesTests(unittest.TestCase):

    def setUp(self):
        self.app = mock.Mock()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = {'shared_root': self.tmp}
        config_dir = os.path.join(self.tmp, 'config')
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, 'baseline.json'), 'w') as fh:
            json.dump({
                'plugins': ['kept-plugin'],
                'theme': '',
                'options': {},
            }, fh)

    def _session_with_enabled_site(self):
        site = mock.Mock()
        site.domain = 'example.com'
        site.site_path = '/var/www/example.com'
        site.is_enabled = True
        session = mock.Mock()
        query = session.query.return_value
        query.filter_by.return_value = query
        query.all.return_value = [site]
        query.first.return_value = site
        return session

    def _write_baseline(self, baseline):
        with open(os.path.join(self.tmp, 'config', 'baseline.json'), 'w') as fh:
            json.dump(baseline, fh)

    def _enabled_site_from_session(self, session):
        return session.query.return_value.filter_by.return_value.all.return_value[0]

    def test_apply_baseline_to_sites_passes_db_cache_type_to_site_apply(self):
        """Each DB row's cache_type is threaded into per-site baseline apply."""
        session = self._session_with_enabled_site()
        self._enabled_site_from_session(session).cache_type = 'wpfc'

        with mock.patch('wo.core.database.db_session', session), \
                mock.patch.object(
                    BaselineApplicator,
                    'apply_baseline_to_site',
                    return_value={'success': True, 'error': None},
                ) as apply_site, \
                mock.patch('wo.core.shellexec.WOShellExec.cmd_exec'), \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.debug'), \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_sites(self.app, self.config)

        self.assertEqual(result['succeeded'], 1)
        apply_site.assert_called_once()
        self.assertEqual(apply_site.call_args.kwargs['cache_type'], 'wpfc')

    def test_apply_baseline_to_sites_dry_run_reports_nginx_helper_purge(self):
        """Dry-run previews caps and purge for wpfc/nginx-helper sites."""
        self._write_baseline({
            'plugins': ['nginx-helper'],
            'theme': '',
            'options': {},
        })
        session = self._session_with_enabled_site()
        self._enabled_site_from_session(session).cache_type = 'wpfc'

        with mock.patch('wo.core.database.db_session', session), \
                mock.patch.object(BaselineApplicator, 'apply_baseline_to_site') as apply_site, \
                mock.patch('wo.core.shellexec.WOShellExec.cmd_exec') as cmd_exec, \
                mock.patch('wo.core.logging.Log.info') as log_info, \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_sites(
                self.app, self.config, dry_run=True,
            )

        messages = [call.args[1] for call in log_info.call_args_list]
        self.assertEqual(result['status'], 'dry_run')
        apply_site.assert_not_called()
        cmd_exec.assert_not_called()
        self.assertTrue(
            any(
                'would grant Nginx Helper admin capabilities' in message
                for message in messages
            ),
            'dry-run must announce Nginx Helper capability grants',
        )
        self.assertTrue(
            any(
                'would enable Nginx Helper purge' in message
                for message in messages
            ),
            'dry-run must announce Nginx Helper purge enablement',
        )

    def test_apply_baseline_to_sites_dry_run_reports_nginx_helper_caps_for_basic_cache(self):
        """Dry-run previews caps for nginx-helper even when purge is not enabled."""
        self._write_baseline({
            'plugins': ['nginx-helper'],
            'theme': '',
            'options': {},
        })
        session = self._session_with_enabled_site()
        self._enabled_site_from_session(session).cache_type = 'basic'

        with mock.patch('wo.core.database.db_session', session), \
                mock.patch.object(BaselineApplicator, 'apply_baseline_to_site') as apply_site, \
                mock.patch('wo.core.shellexec.WOShellExec.cmd_exec') as cmd_exec, \
                mock.patch('wo.core.logging.Log.info') as log_info, \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_sites(
                self.app, self.config, dry_run=True,
            )

        messages = [call.args[1] for call in log_info.call_args_list]
        self.assertEqual(result['status'], 'dry_run')
        apply_site.assert_not_called()
        cmd_exec.assert_not_called()
        self.assertTrue(
            any(
                'would grant Nginx Helper admin capabilities' in message
                for message in messages
            ),
            'dry-run must announce Nginx Helper capability grants',
        )
        self.assertFalse(
            any(
                'would enable Nginx Helper purge' in message
                for message in messages
            ),
            'dry-run must not announce purge for non-purge cache types',
        )

    def test_apply_baseline_to_sites_dry_run_omits_nginx_helper_preview_without_plugin(self):
        """Dry-run does not preview nginx-helper work when the plugin is absent."""
        self._write_baseline({
            'plugins': ['kept-plugin'],
            'theme': '',
            'options': {},
        })
        session = self._session_with_enabled_site()
        self._enabled_site_from_session(session).cache_type = 'wpfc'

        with mock.patch('wo.core.database.db_session', session), \
                mock.patch.object(BaselineApplicator, 'apply_baseline_to_site') as apply_site, \
                mock.patch('wo.core.shellexec.WOShellExec.cmd_exec') as cmd_exec, \
                mock.patch('wo.core.logging.Log.info') as log_info, \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_sites(
                self.app, self.config, dry_run=True,
            )

        messages = [call.args[1] for call in log_info.call_args_list]
        self.assertEqual(result['status'], 'dry_run')
        apply_site.assert_not_called()
        cmd_exec.assert_not_called()
        self.assertFalse(
            any(
                'would grant Nginx Helper admin capabilities' in message
                for message in messages
            ),
            'dry-run must not announce Nginx Helper caps without the plugin',
        )
        self.assertFalse(
            any(
                'would enable Nginx Helper purge' in message
                for message in messages
            ),
            'dry-run must not announce Nginx Helper purge without the plugin',
        )

    def test_apply_baseline_to_sites_passes_htdocs_path_to_site_apply(self):
        """DB site_path is the site root; apply uses the WordPress htdocs dir."""
        session = self._session_with_enabled_site()

        with mock.patch('wo.core.database.db_session', session), \
                mock.patch.object(
                    BaselineApplicator,
                    'apply_baseline_to_site',
                    return_value={'success': True, 'error': None},
                ) as apply_site, \
                mock.patch('wo.core.shellexec.WOShellExec.cmd_exec'), \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.debug'), \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_sites(self.app, self.config)

        self.assertEqual(result['succeeded'], 1)
        apply_site.assert_called_once()
        self.assertEqual(
            apply_site.call_args.args[2],
            '/var/www/example.com/htdocs',
        )

    def test_apply_baseline_to_sites_dry_run_prune_reads_active_plugins_from_htdocs(self):
        """Dry-run prune probes active plugins in the WordPress htdocs dir."""
        session = self._session_with_enabled_site()

        with mock.patch('wo.core.database.db_session', session), \
                mock.patch.object(
                    BaselineApplicator,
                    '_get_active_plugin_slugs',
                    return_value=[],
                ) as get_active_plugin_slugs, \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_sites(
                self.app, self.config, dry_run=True, prune=True,
            )

        self.assertEqual(result['status'], 'dry_run')
        get_active_plugin_slugs.assert_called_once()
        self.assertEqual(
            get_active_plugin_slugs.call_args.args[1],
            '/var/www/example.com/htdocs',
        )

    def test_apply_ignores_obsolete_baseline_metadata(self):
        self._write_baseline({
            'plugins': ['kept-plugin'], 'theme': 'store', 'options': {'blogname': 'Store'},
            'version': {'not': 'a version'},
            'generated': ['not', 'a timestamp'],
            'sources': {'plugins': {'kept-plugin': {'type': 'unsupported'}}},
        })
        session = self._session_with_enabled_site()
        site = self._enabled_site_from_session(session)
        with mock.patch('wo.core.database.db_session', session), \
                mock.patch.object(BaselineApplicator, 'find_plugin_main_file',
                                  return_value='kept-plugin/main.php'), \
                mock.patch.object(mtf.subprocess, 'run',
                                  return_value=mock.Mock(returncode=0, stdout='[]', stderr='')) as run, \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.warn'):
            result = BaselineApplicator.apply_baseline_to_sites(self.app, self.config)

        self.assertEqual(result['succeeded'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertNotIn('baseline_version', result)
        self.assertNotIn('baseline_version', vars(site))
        self.assertIn('updated_at', vars(site))
        commands = [call.args[0] for call in run.call_args_list]
        wp_path = '--path=/var/www/example.com/htdocs'
        self.assertEqual(commands, [
            ['wp', 'option', 'get', 'active_plugins', '--format=json', wp_path, '--allow-root'],
            ['wp', 'plugin', 'activate', 'kept-plugin/main.php', wp_path, '--allow-root'],
            ['wp', 'option', 'update', 'blogname', 'Store', wp_path, '--allow-root'],
            ['wp', 'theme', 'activate', 'store', wp_path, '--allow-root'],
        ])
        session.commit.assert_called_once()


class ReloadServicesAfterConfigChangeTests(unittest.TestCase):

    def setUp(self):
        self.app = mock.Mock()
        self.shared_root = '/tmp/shared'

    def test_nginx_reload_failure_returns_false_and_logs_non_exiting_error(self):
        """A failed nginx reload is fatal to the config-change operation."""
        with mock.patch.object(mtf.os.path, 'exists', return_value=False), \
                mock.patch('wo.core.services.WOService.reload_service', return_value=False) as reload_service, \
                mock.patch('wo.core.logging.Log.error') as log_error, \
                mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok = mtf.reload_services_after_config_change(self.app, self.shared_root)

        self.assertFalse(ok)
        reload_service.assert_called_once_with(self.app, 'nginx')
        self.assertTrue(
            any(call.kwargs.get('exit') is False for call in log_error.call_args_list),
            'nginx reload failure must log a non-exiting error',
        )

    def test_successful_reloads_return_true(self):
        """When every detected service reload succeeds, the operation succeeds."""
        php_config = '/etc/php/8.1/fpm/php-fpm.conf'

        def exists(path):
            return path == php_config

        with mock.patch.object(mtf.os.path, 'exists', side_effect=exists), \
                mock.patch('wo.core.services.WOService.reload_service', return_value=True) as reload_service, \
                mock.patch('wo.core.logging.Log.error') as log_error, \
                mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok = mtf.reload_services_after_config_change(self.app, self.shared_root)

        self.assertTrue(ok)
        self.assertEqual(
            reload_service.call_args_list,
            [mock.call(self.app, 'php8.1-fpm'), mock.call(self.app, 'nginx')],
        )
        log_error.assert_not_called()

    def test_php_fpm_reload_failure_warns_but_nginx_success_returns_true(self):
        """PHP-FPM reload failures are warnings; nginx success remains decisive."""
        php_config = '/etc/php/8.1/fpm/php-fpm.conf'

        def exists(path):
            return path == php_config

        def reload_service(app, service_name):
            return service_name == 'nginx'

        with mock.patch.object(mtf.os.path, 'exists', side_effect=exists), \
                mock.patch('wo.core.services.WOService.reload_service', side_effect=reload_service) as reload_service_mock, \
                mock.patch('wo.core.logging.Log.warn') as log_warn, \
                mock.patch('wo.core.logging.Log.error') as log_error, \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok = mtf.reload_services_after_config_change(self.app, self.shared_root)

        self.assertTrue(ok)
        self.assertEqual(
            reload_service_mock.call_args_list,
            [mock.call(self.app, 'php8.1-fpm'), mock.call(self.app, 'nginx')],
        )
        self.assertTrue(log_warn.called)
        log_error.assert_not_called()


class CoreDatabaseSafetyTests(unittest.TestCase):
    """Focused tests for schema-gated tenant database safety."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write_db_version(self, relative_root, value):
        includes = os.path.join(self.tmp, relative_root, 'wp-includes')
        os.makedirs(includes, exist_ok=True)
        with open(os.path.join(includes, 'version.php'), 'w') as fh:
            fh.write(f"<?php\n$wp_db_version = {value};\n")

    def test_parse_wordpress_db_version(self):
        contents = """<?php
$wp_version = '6.9.1';
  $wp_db_version = 60717;
"""
        self.assertEqual(
            MTFunctions.parse_wordpress_db_version(contents), 60717
        )
        self.assertEqual(
            MTFunctions.parse_wordpress_db_version(
                "<?php\n$wp_db_version = '60718';\n"
            ),
            60718,
        )
        self.assertEqual(
            MTFunctions.parse_wordpress_db_version(
                '<?php\n$wp_db_version = "60719";\n'
            ),
            60719,
        )
        self.assertIsNone(
            MTFunctions.parse_wordpress_db_version(
                "<?php\n$wp_db_version = 'not-an-integer';\n"
            )
        )

    def test_core_schema_transition_is_directional(self):
        self._write_db_version('current', "'60717'")
        self._write_db_version(
            os.path.join('releases', 'wp-staged'), '"60717"'
        )
        self.assertEqual(
            MTFunctions.core_schema_transition(
                mock.Mock(), self.tmp, 'wp-staged'
            ),
            'equal',
        )

        self._write_db_version(
            os.path.join('releases', 'wp-staged'), "'60718'"
        )
        self.assertEqual(
            MTFunctions.core_schema_transition(
                mock.Mock(), self.tmp, 'wp-staged'
            ),
            'upgrade',
        )

        self._write_db_version(
            os.path.join('releases', 'wp-staged'), 60716
        )
        self.assertEqual(
            MTFunctions.core_schema_transition(
                mock.Mock(), self.tmp, 'wp-staged'
            ),
            'downgrade',
        )

    def test_core_schema_transition_reports_unknown_for_unreadable_version(self):
        self._write_db_version('current', 60717)
        self.assertEqual(
            MTFunctions.core_schema_transition(
                mock.Mock(), self.tmp, 'missing-release'
            ),
            'unknown',
        )

    def test_backup_aggregates_failures_and_secures_directory(self):
        sites = [
            {'domain': 'ok.example', 'site_path': '/srv/ok'},
            {'domain': 'bad.example', 'site_path': '/srv/bad'},
        ]
        results = [
            mock.Mock(returncode=0, stdout='', stderr=''),
            mock.Mock(returncode=1, stdout='', stderr='export failed'),
        ]
        with mock.patch.object(
                mtf.subprocess, 'run', side_effect=results) as run:
            ok, failures, backup_dir = MTFunctions.backup_tenant_databases(
                mock.Mock(), sites, self.tmp
            )

        self.assertFalse(ok)
        self.assertEqual([item['domain'] for item in failures], ['bad.example'])
        self.assertEqual(os.stat(backup_dir).st_mode & 0o777, 0o700)
        self.assertIn(
            '--path=/srv/ok/htdocs',
            run.call_args_list[0].args[0],
        )

    def _old_dumps(self):
        names = ['2000-01-0{}-000000'.format(day) for day in range(1, 6)] + ['manual-copy']
        for name in names:
            os.makedirs(os.path.join(self.tmp, 'backups', 'db', name))
        return names

    def _remaining_dumps(self):
        return sorted(os.listdir(os.path.join(self.tmp, 'backups', 'db')))

    def test_backup_keeps_newest_dumps_and_the_back_restore_point(self):
        old = self._old_dumps()
        content = os.path.join(self.tmp, 'content')
        os.makedirs(os.path.join(content, '20000105-000000-abcdef0'))
        os.symlink('20000105-000000-abcdef0', os.path.join(content, 'current'))
        dumps = os.path.join(self.tmp, 'backups', 'db')
        records = [
            {'release': '20000104-000000-1234567', 'previous': 'x', 'result': 'pass',
             'restore_point': {'dumps': os.path.join(dumps, old[1])}},
            {'release': '20000105-000000-abcdef0', 'previous': '20000104-000000-1234567',
             'result': 'pass', 'restore_point': {'dumps': os.path.join(dumps, old[0])}},
            {'release': '20000105-000000-abcdef0', 'previous': None, 'result': 'aborted',
             'restore_point': {'dumps': os.path.join(dumps, old[2])}},
        ]
        with open(os.path.join(content, 'history.jsonl'), 'w') as handle:
            handle.write(''.join(json.dumps(record) + '\n' for record in records))
        sites = [{'domain': 'ok.example', 'site_path': '/srv/ok'}]
        with mock.patch.object(mtf.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            ok, _, backup_dir = MTFunctions.backup_tenant_databases(mock.Mock(), sites, self.tmp)

        self.assertTrue(ok)
        self.assertEqual(
            self._remaining_dumps(),
            sorted([old[0], old[3], old[4], 'manual-copy', os.path.basename(backup_dir)]),
        )

    def test_failed_backup_keeps_old_dumps(self):
        old = self._old_dumps()
        sites = [{'domain': 'bad.example', 'site_path': '/srv/bad'}]
        failed = mock.Mock(returncode=1, stdout='', stderr='export failed')
        with mock.patch.object(mtf.subprocess, 'run', return_value=failed):
            ok, _, backup_dir = MTFunctions.backup_tenant_databases(mock.Mock(), sites, self.tmp)

        self.assertFalse(ok)
        self.assertEqual(self._remaining_dumps(), sorted(old + [os.path.basename(backup_dir)]))

    def test_core_db_upgrade_aggregates_command_and_exception_failures(self):
        sites = [
            {'domain': 'ok.example', 'site_path': '/srv/ok'},
            {'domain': 'exit.example', 'site_path': '/srv/exit'},
            {'domain': 'raise.example', 'site_path': '/srv/raise'},
        ]
        results = [
            mock.Mock(returncode=0, stdout='', stderr=''),
            mock.Mock(returncode=1, stdout='', stderr='upgrade failed'),
            OSError('wp unavailable'),
        ]
        with mock.patch.object(mtf.subprocess, 'run', side_effect=results):
            failures = MTFunctions.run_core_db_upgrades(mock.Mock(), sites)

        self.assertEqual(
            [item['domain'] for item in failures],
            ['exit.example', 'raise.example'],
        )
        self.assertEqual(failures[1]['error'], 'wp unavailable')

    def test_core_db_upgrade_timeout_is_failed_and_next_site_continues(self):
        sites = [
            {'domain': 'hung.example', 'site_path': '/srv/hung'},
            {'domain': 'ok.example', 'site_path': '/srv/ok'},
        ]
        results = [
            mtf.subprocess.TimeoutExpired(cmd='wp', timeout=300),
            mock.Mock(returncode=0, stdout='', stderr=''),
        ]
        with mock.patch.object(
                mtf.subprocess, 'run', side_effect=results) as run:
            failures = MTFunctions.run_core_db_upgrades(mock.Mock(), sites)

        self.assertEqual(len(run.call_args_list), 2)
        self.assertEqual(run.call_args_list[0].kwargs['timeout'], 300)
        self.assertEqual([item['domain'] for item in failures], ['hung.example'])
        self.assertEqual(
            failures[0]['error'],
            'wp core update-db timed out after 300 seconds',
        )

    def _run_update(self, force, backups, upgrades=None,
                    transition='upgrade', switch_error=None, sites=None,
                    reload_results=None,
                    preexisting_domains=None, ungate_results=None,
                    record_error=None, cron_failures=None,
                    local_canary_ok=True,
                    drain_result=None, drain_results=None,
                    gate_results=None, capture_results=None, ledger_error=None):
        if mt is None:
            self.skipTest(
                f'multitenancy controller import unavailable: {_mt_import_error}'
            )
        ctrl = mt.WOMultitenancyController.__new__(
            mt.WOMultitenancyController
        )
        ctrl.app = mock.Mock()
        ctrl.app.pargs = mock.Mock(force=force)
        if sites is None:
            sites = [{
                'domain': 'site.example',
                'site_path': '/srv/site',
            }]
        infra = mock.Mock(spec_set=SharedInfrastructure)
        infra.download_wordpress_core.return_value = 'wp-staged'
        order = []
        reload_results = iter(reload_results or [])
        ungate_results = iter(ungate_results or [])
        preexisting_domains = set(preexisting_domains or [])
        drain_results = iter(drain_results or [])
        gate_results = iter(gate_results or [])
        capture_results = iter(capture_results or [])


        def switch_release(release):
            order.append('flip')
            if transition == 'upgrade':
                with open(mt._pending_upgrades_path(self.tmp)) as fh:
                    ledger = json.load(fh)
                self.assertEqual(ledger['release'], release)
                self.assertEqual(ledger['domains'], [site['domain'] for site in sites])
                self.assertIn('dumps', order)
            if switch_error:
                raise switch_error
        def update_record(app, release):
            order.append('record')
            if record_error:
                raise record_error
            return True


        infra.switch_release.side_effect = switch_release


        os.makedirs(os.path.join(self.tmp, 'config'), exist_ok=True)
        with contextlib.ExitStack() as stack:
            if ledger_error:
                stack.enter_context(mock.patch.object(
                    mt, '_write_pending_upgrades', side_effect=ledger_error,
                ))
            info = stack.enter_context(
                mock.patch('wo.core.logging.Log.info')
            )
            warn = stack.enter_context(
                mock.patch('wo.core.logging.Log.warn')
            )
            stack.enter_context(mock.patch('wo.core.logging.Log.debug'))
            error = stack.enter_context(mock.patch(
                'wo.core.logging.Log.error',
                side_effect=lambda controller, message, exit=True: (
                    controller.app.close(1) if exit else None
                ),
            ))
            stack.enter_context(
                mock.patch.object(
                    mt.MTDatabase, 'is_initialized', return_value=True
                )
            )
            stack.enter_context(
                mock.patch.object(
                    mt.MTDatabase,
                    'get_shared_sites',
                    return_value=sites,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    mt.MTFunctions,
                    'load_config',
                    return_value={'shared_root': self.tmp},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    mt.MTFunctions,
                    'preflight_shared_config',
                    return_value=True,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    mt.MTFunctions,
                    'core_schema_transition',
                    return_value=transition,
                )
            )
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'test_site_locally',
                side_effect=lambda app, site: (
                    order.append(f"canary-local:{site['domain']}")
                    or local_canary_ok
                ),
            ))
            backup = stack.enter_context(
                mock.patch.object(
                    mt.MTFunctions,
                    'backup_tenant_databases',
                    side_effect=lambda app, sites, root: (
                        order.append('dumps') or backups
                    ),
                )
            )
            upgrade = stack.enter_context(
                mock.patch.object(
                    mt.MTFunctions,
                    'run_core_db_upgrades',
                    side_effect=lambda app, target_sites: (
                        order.append('upgrade')
                        or (
                            upgrades(target_sites[0])
                            if callable(upgrades) else (upgrades or [])
                        )
                    ),
                )
            )
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'capture_nginx_worker_pids',
                side_effect=lambda: (
                    order.append('nginx-capture')
                    or next(capture_results, ({101}, None))
                ),
            ))
            stack.enter_context(mock.patch.object(
                mt,
                '_maintenance_gate_exists',
                side_effect=lambda domain: domain in preexisting_domains,
            ))
            gate = stack.enter_context(mock.patch.object(
                mt,
                '_maintenance_enable',
                side_effect=lambda app, domain, message, config, **kwargs: (
                    order.append(f'gate:{domain}')
                    or next(gate_results, True)
                ),
            ))
            ungate = stack.enter_context(mock.patch.object(
                mt,
                '_maintenance_disable',
                side_effect=lambda app, domain: (
                    order.append(f'ungate:{domain}')
                    or next(ungate_results, True)
                ),
            ))
            def acquire_locks(app, target_sites, timeout):
                if cron_failures:
                    return {}, list(cron_failures)
                locks = {}
                for site in target_sites:
                    domain = site['domain']
                    order.append(f'lock:{domain}')
                    locks[domain] = object()
                return locks, []

            def release_locks(locks):
                for domain in list(locks):
                    order.append(f'unlock:{domain}')
                locks.clear()

            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'acquire_tenant_cron_locks',
                side_effect=acquire_locks,
            ))
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'release_tenant_cron_locks',
                side_effect=release_locks,
            ))
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'sync_wp_cron_entries',
                side_effect=lambda app: (
                    order.append('cron-sync') or True
                ),
            ))
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'drain_tenant_active_work',
                side_effect=lambda app, sites, locks, old_pids, **kwargs: (
                    order.append(
                        f"drain:{kwargs['timeout']}:"
                        f"{kwargs['sleeper_horizon']}"
                    )
                    or next(
                        drain_results,
                        drain_result if drain_result is not None else (True, [])
                    )
                ),
            ))
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'safe_nginx_reload',
                side_effect=lambda app, domain: (
                    order.append('reload') or next(reload_results, True)
                ),
            ))
            stack.enter_context(
                mock.patch.object(mt.MTFunctions, 'clear_all_caches')
            )
            stack.enter_context(
                mock.patch.object(
                    mt.MTFunctions, 'reset_opcache', return_value=True
                )
            )
            stack.enter_context(
                mock.patch.object(
                    mt.MTDatabase,
                    'update_release',
                    side_effect=update_record,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    mt, 'SharedInfrastructure', return_value=infra
                )
            )
            stack.enter_context(
                mock.patch.object(mt, 'ReleaseManager')
            )
            ctrl.update()

        return (
            infra, order, backup, upgrade, info, warn, error, ctrl, gate, ungate
        )

    def test_update_aborts_on_backup_failure_even_with_force(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (False, [{'domain': 'site.example', 'error': 'export failed'}],
             '/backups/db/stamp'),
        )
        infra.switch_release.assert_not_called()
        upgrade.assert_not_called()
        ctrl.app.close.assert_called_once_with(1)
        self.assertFalse(os.path.exists(mt._pending_upgrades_path(self.tmp)))
        self.assertEqual(order, [
            'nginx-capture',
            'gate:site.example', 'reload', 'lock:site.example',
            'cron-sync', 'drain:330:60', 'dumps',
            'unlock:site.example', 'ungate:site.example', 'reload',
        ])

    def test_update_runs_db_upgrade_only_after_release_record(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(True, (True, [], '/backups/db/stamp'))
        infra.switch_release.assert_called_once_with('wp-staged')
        self.assertEqual(order, [
            'nginx-capture',
            'gate:site.example', 'reload', 'lock:site.example',
            'cron-sync', 'drain:330:60', 'dumps',
            'flip', 'record', 'upgrade', 'ungate:site.example',
            'unlock:site.example', 'reload',
        ])
        self.assertFalse(os.path.exists(mt._pending_upgrades_path(self.tmp)))

    def test_same_schema_canary_runs_after_switch_without_db_commands(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], None), transition='equal',
        )
        infra.switch_release.assert_called_once_with('wp-staged')
        backup.assert_not_called()
        upgrade.assert_not_called()
        gate.assert_not_called()
        ungate.assert_not_called()
        self.assertEqual(order, ['flip', 'record', 'canary-local:site.example'])
        self.assertFalse(os.path.exists(mt._pending_upgrades_path(self.tmp)))

    def test_downgrade_and_unknown_abort_before_mutation_with_nonzero_status(self):
        for transition in ('downgrade', 'unknown'):
            with self.subTest(transition=transition):
                (infra, order, backup, upgrade, info, warn, error,
                 ctrl, gate, ungate) = self._run_update(
                    True, (True, [], None), transition=transition
                )
                infra.switch_release.assert_not_called()
                ctrl.app.close.assert_called_once_with(1)
                self.assertEqual(order, [])

    def test_partial_db_upgrade_reports_nonzero_without_success_status(self):
        failure = {
            'domain': 'site.example',
            'path': '/srv/site/htdocs',
            'error': 'upgrade failed',
        }
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (True, [], '/backups/db/stamp'),
            upgrades=[failure],
        )

        messages = [call.args[1] for call in info.call_args_list]
        self.assertTrue(any('result=partial' in msg for msg in messages))
        self.assertFalse(any('result=success' in msg for msg in messages))
        self.assertFalse(any('completed successfully' in msg for msg in messages))
        ctrl.app.close.assert_called_once_with(1)
        self.assertIn(
            'backup_dir=/backups/db/stamp',
            error.call_args_list[-1].args[1],
        )
        self.assertIn('gate:site.example', order)
        self.assertNotIn('ungate:site.example', order)
        warning_messages = [call.args[1] for call in warn.call_args_list]
        self.assertTrue(any(
            'wo multitenancy maintenance --disable --site=site.example' in msg
            for msg in warning_messages
        ))

    def test_pre_flip_exception_ungates_every_activated_site(self):
        sites = [
            {'domain': 'one.example', 'site_path': '/srv/one'},
            {'domain': 'two.example', 'site_path': '/srv/two'},
        ]
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (True, [], '/backups/db/stamp'),
            switch_error=RuntimeError('flip failed'),
            sites=sites,
        )

        self.assertEqual(order, [
            'nginx-capture',
            'gate:one.example', 'gate:two.example', 'reload',
            'lock:one.example', 'lock:two.example',
            'cron-sync', 'drain:330:60', 'dumps', 'flip',
            'unlock:one.example', 'unlock:two.example',
            'ungate:one.example', 'ungate:two.example', 'reload',
        ])
        ctrl.app.close.assert_called_once_with(1)

    def test_gated_local_canary_runs_after_all_migrations_before_batch_ungate(self):
        sites = [
            {'domain': 'one.example', 'site_path': '/srv/one'},
            {'domain': 'two.example', 'site_path': '/srv/two'},
        ]
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], '/backups/db/stamp'), sites=sites,
        )
        self.assertEqual(order, [
            'nginx-capture', 'gate:one.example', 'gate:two.example', 'reload',
            'lock:one.example', 'lock:two.example', 'cron-sync', 'drain:330:60',
            'dumps', 'flip', 'record', 'upgrade', 'upgrade',
            'gate:one.example', 'gate:two.example', 'reload',
            'canary-local:one.example', 'nginx-capture',
            'gate:one.example', 'gate:two.example', 'reload', 'drain:330:0',
            'ungate:one.example', 'unlock:one.example',
            'ungate:two.example', 'unlock:two.example', 'reload',
        ])
        self.assertEqual(
            [call.kwargs.get('loopback_bypass') for call in gate.call_args_list],
            [False, False, True, True, False, False],
        )
        infra.switch_release.assert_called_once_with('wp-staged')

    def test_gate_reload_abort_cleans_gates_without_core_switch(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True, (True, [], '/backups/db/stamp'),
            # Activation reload fails; pre-flip cleanup reload succeeds.
            reload_results=[False, True],
        )

        infra.switch_release.assert_not_called()
        self.assertEqual(order, [
            'nginx-capture',
            'gate:site.example', 'reload',
            'ungate:site.example', 'reload',
        ])

    def test_canary_failure_closes_bypass_and_holds_new_core_in_maintenance(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], '/backups/db/stamp'), local_canary_ok=False,
        )
        self.assertFalse(gate.call_args_list[-1].kwargs['loopback_bypass'])
        self.assertLess(order.index('dumps'), order.index('flip'))
        self.assertLess(order.index('flip'), order.index('upgrade'))
        self.assertLess(order.index('upgrade'), order.index('canary-local:site.example'))
        infra.switch_release.assert_called_once_with('wp-staged')
        ungate.assert_not_called()
        self.assertIn('unlock:site.example', order)
        ctrl.app.close.assert_called_once_with(1)
        self.assertTrue(any('new core remains active' in call.args[1] for call in warn.call_args_list))

    def test_canary_drain_timeout_holds_new_core_in_maintenance(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], '/backups/db/stamp'),
            drain_results=[(True, []), (False, ['old_nginx_workers=202'])],
        )
        self.assertFalse(gate.call_args_list[-1].kwargs['loopback_bypass'])
        self.assertIn('dumps', order)
        infra.switch_release.assert_called_once_with('wp-staged')
        ungate.assert_not_called()
        self.assertIn('unlock:site.example', order)
        self.assertIn('old_nginx_workers=202', error.call_args_list[-1].args[1])
        ctrl.app.close.assert_called_once_with(1)

    def test_abort_close_render_failure_retains_gates_after_core_switch(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], '/backups/db/stamp'),
            capture_results=[({101}, None), (set(), 'nginx_workers=unavailable')],
            gate_results=[True, True, False],
        )
        infra.switch_release.assert_called_once_with('wp-staged')
        ungate.assert_not_called()
        self.assertIn('unlock:site.example', order)
        final_error = error.call_args_list[-1].args[1]
        self.assertIn('could not close loopback bypass', final_error)
        self.assertIn('maintenance cleanup incomplete', final_error)
        ctrl.app.close.assert_called_once_with(1)

    def test_abort_close_reload_failure_retains_gates_after_core_switch(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], '/backups/db/stamp'),
            capture_results=[({101}, None), (set(), 'nginx_workers=unavailable')],
            reload_results=[True, True, False],
        )
        infra.switch_release.assert_called_once_with('wp-staged')
        ungate.assert_not_called()
        self.assertIn('unlock:site.example', order)
        self.assertIn('could not reload nginx after closing loopback bypass',
                      error.call_args_list[-1].args[1])
        ctrl.app.close.assert_called_once_with(1)




    def test_active_work_drain_timeout_aborts_before_dumps_and_switch(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (True, [], '/backups/db/stamp'),
            drain_result=(False, [
                'php_fpm_active=2', 'innodb_transactions=1',
            ]),
        )

        infra.switch_release.assert_not_called()
        backup.assert_not_called()
        upgrade.assert_not_called()
        self.assertIn('ungate:site.example', order)
        self.assertIn(
            'php_fpm_active=2, innodb_transactions=1',
            error.call_args_list[-1].args[1],
        )

    def test_active_work_drain_waits_for_old_generation_then_backstops(self):
        locks = {'site.example': object()}
        with mock.patch.object(
                MTFunctions,
                'live_nginx_worker_pids',
                side_effect=[{101}, set(), set()]) as nginx_probe, \
                mock.patch.object(
                    MTFunctions,
                    'probe_active_php_fpm_workers',
                    side_effect=[(0, 1, None), (0, 0, None)]
                ) as php_probe, \
                mock.patch.object(
                    MTFunctions,
                    'probe_tenant_innodb_transactions',
                    side_effect=[(1, None), (0, None)]) as db_probe, \
                mock.patch.object(
                    mtf._time, 'monotonic',
                    side_effect=[0, 0, 1, 2]), \
                mock.patch.object(mtf._time, 'sleep') as sleep:
            drained, blockers = MTFunctions.drain_tenant_active_work(
                mock.Mock(),
                [{'domain': 'site.example'}],
                locks,
                {101},
                timeout=10,
                sleeper_horizon=0,
                poll_interval=1,
            )

        self.assertTrue(drained)
        self.assertEqual(blockers, [])
        self.assertEqual(nginx_probe.call_count, 3)
        self.assertEqual(php_probe.call_count, 2)
        self.assertEqual(db_probe.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    def test_preexisting_gate_is_never_modified_or_removed(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (True, [], '/backups/db/stamp'),
            preexisting_domains={'site.example'},
        )

        gate.assert_not_called()
        ungate.assert_not_called()
        self.assertIn('lock:site.example', order)
        self.assertIn('unlock:site.example', order)
        warning_messages = [call.args[1] for call in warn.call_args_list]
        self.assertTrue(any(
            'Pre-existing maintenance gate remains active for site.example'
            in message for message in warning_messages
        ))

    def test_unforced_update_refuses_when_every_gate_is_operator_owned(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], '/backups/db/stamp'),
            preexisting_domains={'site.example'},
        )
        gate.assert_not_called()
        ungate.assert_not_called()
        backup.assert_not_called()
        upgrade.assert_not_called()
        infra.switch_release.assert_not_called()
        self.assertEqual(order, ['nginx-capture'])
        self.assertIn('No update-owned gate', error.call_args_list[-1].args[1])
        ctrl.app.close.assert_called_once_with(1)

    def test_unsafe_domain_aborts_before_paths_or_backups(self):
        site = {'domain': '../escape;rm', 'site_path': '/srv/unsafe'}
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (True, [], '/backups/db/stamp'),
            sites=[site],
        )

        backup.assert_not_called()
        gate.assert_not_called()
        ungate.assert_not_called()
        ctrl.app.close.assert_called_once_with(1)

    def test_ledger_write_failure_aborts_before_switch_even_with_force(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True, (True, [], '/backups/db/stamp'),
            ledger_error=OSError('disk full'),
        )
        backup.assert_called_once()
        infra.switch_release.assert_not_called()
        upgrade.assert_not_called()
        self.assertIn('ungate:site.example', order)
        self.assertIn('pending-db-upgrades ledger', error.call_args_list[-1].args[1])
        ctrl.app.close.assert_called_once_with(1)

    def test_tracking_failure_after_switch_preserves_pending_migration_ledger(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True, (True, [], '/backups/db/stamp'),
            record_error=RuntimeError('tracking database unavailable'),
        )
        infra.switch_release.assert_called_once_with('wp-staged')
        upgrade.assert_not_called()
        ungate.assert_not_called()
        self.assertIn('unlock:site.example', order)
        with open(mt._pending_upgrades_path(self.tmp)) as fh:
            ledger = json.load(fh)
        self.assertEqual(ledger['domains'], ['site.example'])
        self.assertEqual(ledger['release'], 'wp-staged')
        ctrl.app.close.assert_called_once_with(1)

    def test_equal_schema_canary_failure_reports_failure_after_switch(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            False, (True, [], None), transition='equal', local_canary_ok=False,
        )
        infra.switch_release.assert_called_once_with('wp-staged')
        self.assertEqual(order, ['flip', 'record', 'canary-local:site.example'])
        messages = [call.args[1] for call in info.call_args_list]
        self.assertIn('update_failed target=core result=failure', messages)
        self.assertFalse(any('result=partial' in message for message in messages))
        self.assertIn('wo multitenancy rollback', error.call_args_list[-1].args[1])
        ctrl.app.close.assert_called_once_with(1)
        backup.assert_not_called()
        upgrade.assert_not_called()

    def test_abort_surfaces_gate_cleanup_and_reload_failures(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True, (True, [], '/backups/db/stamp'),
            reload_results=[False, False], ungate_results=[False],
        )

        final_error = error.call_args_list[-1].args[1]
        self.assertIn('maintenance cleanup incomplete', final_error)
        self.assertIn('could not remove maintenance gate', final_error)
        self.assertIn('could not reload nginx after gate cleanup', final_error)

    def test_post_loop_reload_failure_restores_gate_and_reports_partial(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (True, [], '/backups/db/stamp'),
            reload_results=[True, False],
        )

        self.assertEqual(gate.call_count, 2)
        self.assertEqual(ungate.call_count, 1)
        messages = [call.args[1] for call in info.call_args_list]
        self.assertTrue(any('result=partial' in message for message in messages))
        self.assertIn(
            'batch maintenance ungate reload failed',
            error.call_args_list[-2].args[1],
        )

    def test_mixed_db_result_only_ungates_successful_site(self):
        sites = [
            {'domain': 'ok.example', 'site_path': '/srv/ok'},
            {'domain': 'bad.example', 'site_path': '/srv/bad'},
        ]

        def upgrades(site):
            if site['domain'] == 'bad.example':
                return [{
                    'domain': 'bad.example',
                    'path': '/srv/bad/htdocs',
                    'error': 'upgrade failed',
                }]
            return []

        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True,
            (True, [], '/backups/db/stamp'),
            upgrades=upgrades,
            sites=sites,
        )

        self.assertIn('ungate:ok.example', order)
        self.assertNotIn('ungate:bad.example', order)
        self.assertLess(
            order.index('unlock:ok.example'),
            len(order) - 1,
        )
        self.assertIn('unlock:bad.example', order)
        with open(mt._pending_upgrades_path(self.tmp)) as fh:
            ledger = json.load(fh)
        self.assertEqual(ledger['domains'], ['bad.example'])
        self.assertEqual(ledger['release'], 'wp-staged')

    def test_cron_lock_drain_failure_aborts_and_cleans_preflip_state(self):
        (infra, order, backup, upgrade, info, warn, error, ctrl,
         gate, ungate) = self._run_update(
            True, (True, [], '/backups/db/stamp'), cron_failures=['site.example'],
        )

        infra.switch_release.assert_not_called()
        self.assertIn('ungate:site.example', order)

    def test_cron_lock_helper_drains_and_holds_until_release(self):
        domain = 'wordops-lock-test.example'
        lock_path = f'/tmp/wo-cron-{domain}.lock'
        locks = {}
        retry = {}
        try:
            locks, failures = MTFunctions.acquire_tenant_cron_locks(
                mock.Mock(), [{'domain': domain}], timeout=0
            )
            self.assertEqual(failures, [])
            self.assertIn(domain, locks)

            blocked, failures = MTFunctions.acquire_tenant_cron_locks(
                mock.Mock(), [{'domain': domain}], timeout=0
            )
            self.assertEqual(blocked, {})
            self.assertEqual(failures, [domain])

            MTFunctions.release_tenant_cron_locks(locks)
            retry, failures = MTFunctions.acquire_tenant_cron_locks(
                mock.Mock(), [{'domain': domain}], timeout=0
            )
            self.assertEqual(failures, [])
            self.assertIn(domain, retry)
        finally:
            MTFunctions.release_tenant_cron_locks(locks)
            MTFunctions.release_tenant_cron_locks(retry)
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass

    def test_cron_lock_helper_opens_protected_existing_file_read_only(self):
        domain = 'protected-cron.example'
        path = f'/tmp/wo-cron-{domain}.lock'
        lock_path = os.path.join(self.tmp, 'cron.lock')
        with open(lock_path, 'w') as fh:
            fh.write('existing cron lock\n')
        os.chmod(lock_path, 0o664)
        before = os.stat(lock_path)
        real_open = os.open

        def open_lock(actual_path, flags, mode=0o777):
            self.assertEqual(actual_path, path)
            # Model Linux protected_regular rejecting O_CREAT on a lock
            # owned by www-data, even when root can open it read-only.
            if flags & os.O_CREAT:
                raise PermissionError(errno.EACCES, 'protected_regular', path)
            return real_open(lock_path, flags, mode)

        with mock.patch.object(mtf.os, 'open', side_effect=open_lock) as opened:
            locks, failures = MTFunctions.acquire_tenant_cron_locks(
                mock.Mock(), [{'domain': domain}], timeout=0
            )
        self.addCleanup(MTFunctions.release_tenant_cron_locks, locks)
        self.assertEqual(failures, [])
        opened.assert_called_once_with(
            path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        )
        self.assertEqual(locks[domain].read(), 'existing cron lock\n')
        after = os.fstat(locks[domain].fileno())
        self.assertEqual(
            (after.st_ino, after.st_uid, after.st_mode),
            (before.st_ino, before.st_uid, before.st_mode),
        )

    def test_cron_lock_helper_creates_missing_file_exclusively(self):
        domain = 'missing-cron.example'
        path = f'/tmp/wo-cron-{domain}.lock'
        lock_path = os.path.join(self.tmp, 'cron.lock')
        real_open = os.open
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)

        def open_lock(actual_path, open_flags, mode=0o777):
            self.assertEqual(actual_path, path)
            return real_open(lock_path, open_flags, mode)

        with mock.patch.object(mtf.os, 'open', side_effect=open_lock) as opened:
            locks, failures = MTFunctions.acquire_tenant_cron_locks(
                mock.Mock(), [{'domain': domain}], timeout=0
            )
        self.addCleanup(MTFunctions.release_tenant_cron_locks, locks)
        self.assertEqual(failures, [])
        self.assertIn(domain, locks)
        self.assertEqual(opened.call_args_list, [
            mock.call(path, flags),
            mock.call(path, flags | os.O_CREAT | os.O_EXCL, 0o644),
        ])
        self.assertEqual(
            os.fstat(locks[domain].fileno()).st_ino, os.stat(lock_path).st_ino
        )

    def test_cron_lock_helper_reopens_concurrently_created_file_read_only(self):
        domain = 'racing-cron.example'
        path = f'/tmp/wo-cron-{domain}.lock'
        lock_path = os.path.join(self.tmp, 'cron.lock')
        real_open = os.open
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)

        def open_lock(actual_path, open_flags, mode=0o777):
            self.assertEqual(actual_path, path)
            if open_flags & os.O_CREAT:
                with open(lock_path, 'x') as fh:
                    fh.write('created by cron\n')
                os.chmod(lock_path, 0o664)
            return real_open(lock_path, open_flags, mode)

        with mock.patch.object(mtf.os, 'open', side_effect=open_lock) as opened:
            locks, failures = MTFunctions.acquire_tenant_cron_locks(
                mock.Mock(), [{'domain': domain}], timeout=0
            )
        self.addCleanup(MTFunctions.release_tenant_cron_locks, locks)
        self.assertEqual(failures, [])
        self.assertEqual(locks[domain].read(), 'created by cron\n')
        self.assertEqual(opened.call_args_list, [
            mock.call(path, flags),
            mock.call(path, flags | os.O_CREAT | os.O_EXCL, 0o644),
            mock.call(path, flags),
        ])
        self.assertEqual(
            os.fstat(locks[domain].fileno()).st_ino, os.stat(lock_path).st_ino
        )

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'requires O_NOFOLLOW')
    def test_cron_lock_helper_rejects_existing_and_racing_symlinks(self):
        domain = 'symlink-cron.example'
        path = f'/tmp/wo-cron-{domain}.lock'
        real_open = os.open
        flags = os.O_RDONLY | os.O_NOFOLLOW
        for racing in (False, True):
            for dangling in (False, True):
                with self.subTest(racing=racing, dangling=dangling):
                    suffix = f'{racing}-{dangling}'
                    lock_path = os.path.join(self.tmp, f'cron-{suffix}.lock')
                    target = os.path.join(self.tmp, f'target-{suffix}')
                    if not dangling:
                        with open(target, 'w') as fh:
                            fh.write('do not touch\n')
                    if not racing:
                        os.symlink(target, lock_path)

                    def open_lock(actual_path, open_flags, mode=0o777):
                        self.assertEqual(actual_path, path)
                        if racing and open_flags & os.O_CREAT:
                            os.symlink(target, lock_path)
                        return real_open(lock_path, open_flags, mode)

                    with mock.patch.object(
                        mtf.os, 'open', side_effect=open_lock
                    ) as opened:
                        locks, failures = MTFunctions.acquire_tenant_cron_locks(
                            mock.Mock(), [{'domain': domain}], timeout=0
                        )
                    self.addCleanup(MTFunctions.release_tenant_cron_locks, locks)
                    self.assertEqual(locks, {})
                    self.assertEqual(failures, [domain])
                    expected = [mock.call(path, flags)]
                    if racing:
                        expected.extend([
                            mock.call(
                                path, flags | os.O_CREAT | os.O_EXCL, 0o644
                            ),
                            mock.call(path, flags),
                        ])
                    self.assertEqual(opened.call_args_list, expected)
                    self.assertTrue(os.path.islink(lock_path))
                    if dangling:
                        self.assertFalse(os.path.lexists(target))
                    else:
                        with open(target) as fh:
                            self.assertEqual(fh.read(), 'do not touch\n')

    def _guarded_cron_test_command(self, domain, splay, ran):
        site_root = os.path.join(self.tmp, domain)
        gate_dir = os.path.join(site_root, 'conf', 'nginx')
        os.makedirs(gate_dir, exist_ok=True)
        os.makedirs(os.path.join(site_root, 'htdocs'), exist_ok=True)
        line = MTFunctions.build_tenant_cron_line(domain, splay=splay)
        self.assertLess(line.index('sleep '), line.index('test -x'))
        self.assertLess(line.index('test -x'), line.index('test ! -e'))
        self.assertLess(line.index('test ! -e'), line.index('flock -n'))
        command = line.split('www-data ', 1)[1]
        command = command.replace(f'/var/www/{domain}', site_root)
        command = command.replace(
            f'/tmp/wo-cron-{domain}.lock',
            os.path.join(self.tmp, 'cron.lock'),
        )
        wp_command = (
            "/usr/local/bin/wp cron event run --due-now --quiet "
            ">/dev/null 2>&1"
        )
        fake_flock = os.path.join(self.tmp, 'flock')
        with open(fake_flock, 'w') as fh:
            fh.write('#!/bin/sh\nshift 2\nexec "$@"\n')
        os.chmod(fake_flock, 0o755)

        command = command.replace('flock -n', f"'{fake_flock}' -n")
        return command.replace(wp_command, f"touch '{ran}'"), gate_dir
    def test_maintenance_bypass_uses_socket_source_not_rewritten_client(self):
        with open('wo/cli/templates/multitenancy-maintenance.mustache') as fh:
            template = fh.read()
        self.assertIn('$realip_remote_addr = 127.0.0.1', template)
        self.assertIn('$realip_remote_addr = "::1"', template)
        self.assertNotIn('$remote_addr =', template)
        import pystache
        context = {
            'domain': 'site.example',
            'generated_at': 'now',
            'retry_after_seconds': 600,
            'site_htdocs': '/var/www/site.example/htdocs',
        }
        closed = pystache.render(
            template, dict(context, loopback_bypass=False)
        )
        bypass = pystache.render(
            template, dict(context, loopback_bypass=True)
        )
        self.assertNotIn('$realip_remote_addr', closed)
        self.assertIn('$realip_remote_addr = 127.0.0.1', bypass)
        self.assertIn('$realip_remote_addr = 127.0.0.2', bypass)
        self.assertIn('set $wo_mt_maintenance 0;', bypass)

    def test_local_canary_curl_is_cacheproof_and_loopback_pinned(self):
        result = mock.Mock(returncode=0, stdout='200\t', stderr='')
        with mock.patch.object(
                mtf.subprocess, 'run', return_value=result) as run:
            first = MTFunctions.test_site_locally(mock.Mock(), {
                'domain': 'shop.example',
                'is_ssl': True,
            })
            second = MTFunctions.test_site_locally(mock.Mock(), {
                'domain': 'shop.example',
                'is_ssl': True,
            })

        self.assertTrue(first)
        self.assertTrue(second)
        commands = [call.args[0] for call in run.call_args_list]
        urls = [command[-1] for command in commands]
        prefix = 'https://shop.example/?wo_mt_canary='
        tokens = [url[len(prefix):] for url in urls]
        self.assertTrue(all(url.startswith(prefix) for url in urls))
        self.assertTrue(all(
            len(token) == 32
            and all(char in '0123456789abcdef' for char in token)
            for token in tokens
        ))
        self.assertNotEqual(tokens[0], tokens[1])
        command = commands[0]
        self.assertNotIn('--location', command)
        self.assertEqual(command[command.index('--proto') + 1], '=http,https')
        self.assertEqual(
            command[command.index('--header') + 1],
            'X-Requested-With: XMLHttpRequest',
        )
        for resolve in (
                'shop.example:80:127.0.0.2',
                'shop.example:443:127.0.0.2',
                'www.shop.example:80:127.0.0.2',
                'www.shop.example:443:127.0.0.2'):
            self.assertIn(resolve, command)

    def test_local_canary_manually_follows_allowed_redirect(self):
        results = [
            mock.Mock(
                returncode=0,
                stdout='301\thttps://www.shop.example/landing',
                stderr='',
            ),
            mock.Mock(returncode=0, stdout='200\t', stderr=''),
        ]
        with mock.patch.object(
                mtf.subprocess, 'run', side_effect=results) as run:
            ok = MTFunctions.test_site_locally(
                mock.Mock(),
                {'domain': 'shop.example', 'is_ssl': True},
            )

        self.assertTrue(ok)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0][-1],
            'https://www.shop.example/landing',
        )

    def test_canary_redirect_limit_is_five(self):
        curl_redirect = mock.Mock(
            returncode=0,
            stdout='302\thttps://shop.example/again',
            stderr='',
        )
        with mock.patch.object(
                mtf.subprocess, 'run',
                return_value=curl_redirect) as run:
            self.assertFalse(MTFunctions.test_site_locally(
                mock.Mock(),
                {'domain': 'shop.example', 'is_ssl': True},
            ))
        self.assertEqual(run.call_count, 6)

    def test_local_canary_rejects_terminal_or_unsafe_redirect(self):
        for output in ('301\t', '404\t'):
            result = mock.Mock(returncode=0, stdout=output, stderr='')
            with mock.patch.object(
                    mtf.subprocess, 'run', return_value=result):
                self.assertFalse(MTFunctions.test_site_locally(
                    mock.Mock(),
                    {'domain': 'shop.example', 'is_ssl': True},
                ))

        for location in (
                'https://unlisted.example/',
                'http://shop.example:8080/',
                'https://www.shop.example:8443/'):
            result = mock.Mock(
                returncode=0,
                stdout=f'302\t{location}',
                stderr='',
            )
            with mock.patch.object(
                    mtf.subprocess, 'run',
                    return_value=result) as run, \
                    mock.patch.object(mtf.Log, 'warn') as warn:
                self.assertFalse(MTFunctions.test_site_locally(
                    mock.Mock(),
                    {'domain': 'shop.example', 'is_ssl': True},
                ))
            self.assertEqual(run.call_count, 1)
            self.assertIn(location, warn.call_args.args[1])

    @staticmethod
    def _fpm_status_result(pool='www', version='84', queue=0, processes=None):
        if processes is None:
            processes = [
                {'state': 'Running', 'request uri': '/status?json&full',
                 'script': '-'},
                {'state': 'Idle', 'request uri': '/old-request/'},
            ]
        payload = {
            'pool': f'{pool}-php{version}',
            'listen queue': queue,
            'total processes': len(processes),
            'processes': processes,
        }
        return mock.Mock(
            returncode=0,
            stdout='Content-type: application/json\r\n\r\n' + json.dumps(payload),
            stderr='',
        )

    def test_fpm_probe_reads_both_pools_directly_and_reports_active_work(self):
        responses = [
            self._fpm_status_result(queue=2),
            self._fpm_status_result('www-two', queue=3, processes=[
                {'state': 'Running', 'request uri': '/status?json&full',
                 'script': '-'},
                {'state': 'Running', 'request uri': '/checkout/'},
                {'state': 'Finishing', 'request uri': '/status-of-order/'},
                {'state': 'Running', 'request uri': '/fpm/status/tenant-page'},
                {'state': 'Running', 'request uri': '/status?json&full',
                 'script': '/var/www/store/htdocs/index.php'},
                {'state': 'Running', 'request uri': '/status'},
                {'state': 'Idle', 'request uri': '/old-request/'},
            ]),
        ]
        with mock.patch.object(
                mtf.subprocess, 'run', side_effect=responses) as run:
            result = MTFunctions.probe_active_php_fpm_workers([
                {'php_version': '8.4'}, {'php_version': '8.4'},
            ])

        self.assertEqual(result, (5, 5, None))
        self.assertEqual(run.call_count, 2)
        for call, suffix in zip(run.call_args_list, ('', '-two')):
            self.assertEqual(call.args[0], [
                '/usr/bin/cgi-fcgi', '-bind', '-connect',
                f'/var/run/php/php84{suffix}-fpm.sock',
            ])
            self.assertEqual(call.kwargs['env'], {
                'REQUEST_METHOD': 'GET',
                'SCRIPT_NAME': '/status',
                'SCRIPT_FILENAME': '/status',
                'REQUEST_URI': '/status?json&full',
                'QUERY_STRING': 'json&full',
                'SERVER_PROTOCOL': 'HTTP/1.1',
                'GATEWAY_INTERFACE': 'CGI/1.1',
            })
            self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(call.kwargs['timeout'], 5)

    def test_php_stack_includes_fastcgi_status_client(self):
        from wo.core.variables import WOVar

        self.assertIn('libfcgi-bin', WOVar.wo_php_extra)

    def test_fpm_probe_requires_all_pools_for_all_tracked_versions(self):
        responses = [
            self._fpm_status_result(pool, version)
            for version in ('82', '84')
            for pool in ('www', 'www-two')
        ]
        with mock.patch.object(
                mtf.subprocess, 'run', side_effect=responses) as run:
            self.assertEqual(MTFunctions.probe_active_php_fpm_workers([
                {'php_version': '8.4'}, {'php_version': '8.2'},
                {'php_version': '8.4'},
            ]), (0, 0, None))
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            [
                '/var/run/php/php82-fpm.sock',
                '/var/run/php/php82-two-fpm.sock',
                '/var/run/php/php84-fpm.sock',
                '/var/run/php/php84-two-fpm.sock',
            ],
        )

    def test_fpm_probe_rejects_unknown_versions_without_requesting_status(self):
        for sites in ([], [{}], [{'php_version': '../../8.4'}]):
            with self.subTest(sites=sites), mock.patch.object(
                    mtf.subprocess, 'run') as run:
                self.assertEqual(
                    MTFunctions.probe_active_php_fpm_workers(sites),
                    (None, None, 'php_fpm_status=unavailable'),
                )
                run.assert_not_called()

    def test_fpm_probe_fails_closed_when_either_pool_is_unavailable(self):
        for pool_index, pool in enumerate(('www', 'www-two')):
            for failure in (
                    FileNotFoundError(),
                    subprocess.TimeoutExpired('cgi-fcgi', 5),
                    mock.Mock(returncode=1, stdout='', stderr='unavailable')):
                with self.subTest(pool=pool, failure=type(failure).__name__):
                    responses = [self._fpm_status_result()] * pool_index
                    responses.append(failure)
                    with mock.patch.object(
                            mtf.subprocess, 'run', side_effect=responses):
                        self.assertEqual(
                            MTFunctions.probe_active_php_fpm_workers([
                                {'php_version': '8.4'},
                            ]),
                            (None, None,
                             f'php_fpm_status_8.4_{pool}=unavailable'),
                        )

    def test_fpm_probe_rejects_incomplete_or_wrong_pool_status(self):
        valid = json.loads(self._fpm_status_result().stdout.split('\r\n\r\n')[1])
        invalid = [
            {**valid, 'pool': 'www-two-php84'},
            {**valid, 'listen queue': -1},
            {**valid, 'listen queue': '0'},
            {**valid, 'listen queue': False},
            {key: value for key, value in valid.items() if key != 'listen queue'},
            {key: value for key, value in valid.items() if key != 'processes'},
            {key: value for key, value in valid.items() if key != 'total processes'},
            {**valid, 'total processes': 3},
            {**valid, 'total processes': 0, 'processes': []},
            {**valid, 'total processes': 1, 'processes': [{}]},
            {**valid, 'total processes': 1, 'processes': [None]},
            {**valid, 'total processes': 1, 'processes': [
                {'state': '', 'request uri': '/checkout/'},
            ]},
            {**valid, 'total processes': 1, 'processes': [
                {'state': 'Running'},
            ]},
            [],
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = mock.Mock(
                    returncode=0,
                    stdout='Content-type: application/json\n\n' + json.dumps(payload),
                )
                with mock.patch.object(
                        mtf.subprocess, 'run', return_value=response):
                    self.assertEqual(
                        MTFunctions.probe_active_php_fpm_workers([
                            {'php_version': '8.4'},
                        ]),
                        (None, None, 'php_fpm_status_8.4_www=unparseable'),
                    )

    def test_fpm_probe_rejects_cgi_errors_and_non_json_bodies(self):
        valid_body = self._fpm_status_result().stdout.split('\r\n\r\n')[1]
        for output in (
                valid_body,
                'Status: 404 Not Found\n\n' + valid_body,
                'Status:\n\n' + valid_body,
                'Content-type: text/html\n\n<html>Not found</html>',
                'Content-type: application/json\n\n'):
            with self.subTest(output=output):
                with mock.patch.object(
                        mtf.subprocess, 'run',
                        return_value=mock.Mock(returncode=0, stdout=output)):
                    self.assertEqual(
                        MTFunctions.probe_active_php_fpm_workers([
                            {'php_version': '8.4'},
                        ]),
                        (None, None, 'php_fpm_status_8.4_www=unparseable'),
                    )


    def test_guarded_cron_healthy_path_reaches_flock_command(self):
        domain = 'healthy.example'
        ran = os.path.join(self.tmp, 'healthy-ran')
        command, gate_dir = self._guarded_cron_test_command(
            domain, 0, ran
        )

        result = mtf.subprocess.run(
            ['/bin/sh', '-c', command], check=False
        )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.exists(ran))

    def test_sleeping_cron_rechecks_marker_and_skips_after_gate_appears(self):
        domain = 'race.example'
        ran = os.path.join(self.tmp, 'race-ran')
        command, gate_dir = self._guarded_cron_test_command(
            domain, 0.2, ran
        )
        marker = os.path.join(
            gate_dir, 'multitenancy-maintenance.conf'
        )

        process = mtf.subprocess.Popen(['/bin/sh', '-c', command])
        mtf._time.sleep(0.05)
        with open(marker, 'w') as fh:
            fh.write('update gate')
        process.wait(timeout=2)

        self.assertFalse(os.path.exists(ran))


class ReleaseRetentionTests(unittest.TestCase):
    """cleanup_old_releases counts only promoted releases (E.3)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.releases = os.path.join(self.tmp, 'releases')
        os.makedirs(self.releases)
        for name in ('wp-1', 'wp-2', 'wp-3', 'wp-4', 'wp-5'):
            os.makedirs(os.path.join(self.releases, name))
        # wp-3 is promoted; wp-4/wp-5 are leaked/staged (lexically newer).
        os.symlink(os.path.join(self.releases, 'wp-3'),
                   os.path.join(self.tmp, 'current'))

    def test_prune_ignores_staged_newer_names_and_keeps_current(self):
        manager = mtf.ReleaseManager(mock.Mock(), self.tmp)
        with mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            manager.cleanup_old_releases(keep_count=2)
        remaining = sorted(os.listdir(self.releases))
        # current + one older promoted release survive; staged names are
        # not deleted here and never displace a promoted one.
        self.assertEqual(remaining, ['wp-2', 'wp-3', 'wp-4', 'wp-5'])

    def test_prune_never_removes_current_even_with_keep_zero(self):
        manager = mtf.ReleaseManager(mock.Mock(), self.tmp)
        with mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            manager.cleanup_old_releases(keep_count=0)
        self.assertIn('wp-3', os.listdir(self.releases))


class RejectExtraPositionalsTests(unittest.TestCase):
    """Stray positionals (e.g. a pasted em dash) must error out (G.2)."""

    def test_em_dash_token_errors_with_ascii_hint(self):
        if mt is None:
            self.skipTest(
                f'multitenancy controller import unavailable: {_mt_import_error}')
        controller = mock.Mock()
        pargs = mock.Mock()
        pargs.newsite_name = '\u2014le'
        with mock.patch('wo.cli.plugins.multitenancy.Log.error') as log_error:
            mt._reject_extra_positionals(controller, pargs)
        self.assertTrue(log_error.called)
        message = log_error.call_args.args[1]
        self.assertIn('unrecognized arguments: \u2014le', message)
        self.assertIn("use ASCII '--'", message)

    def test_clean_pargs_pass_silently(self):
        if mt is None:
            self.skipTest(
                f'multitenancy controller import unavailable: {_mt_import_error}')
        pargs = mock.Mock()
        pargs.newsite_name = None
        with mock.patch('wo.cli.plugins.multitenancy.Log.error') as log_error:
            mt._reject_extra_positionals(mock.Mock(), pargs)
        log_error.assert_not_called()


if __name__ == '__main__':
    unittest.main()
