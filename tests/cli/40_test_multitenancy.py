"""Multitenancy helper unit tests (stdlib unittest + mock, no live stack).

Covers the two safety-relevant behaviors introduced by the simplification
patch: the shared-config `php -l` preflight, and the admin-IP-free maintenance
render context. Run with:

    python3 -m unittest tests.cli.40_test_multitenancy -v
"""

import contextlib
import io
import json
import os
import shutil
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



class MultitenancyTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write_shared_config(self, content):
        cfg_dir = os.path.join(self.tmp, 'config')
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, 'wp-config-shared.php'), 'w') as fh:
            fh.write(content)


    def _run_create_impl_with_mocks(self, cache_type='wpfc', baseline=None, manager=None):
        domain = 'example.com'
        shared_root = '/var/www/shared'
        if baseline is None:
            baseline = {'plugins': ['nginx-helper'], 'theme': 't', 'options': {}}
        pargs = mock.Mock()
        pargs.site_name = domain
        pargs.letsencrypt = False
        pargs.admin_user = 'admin'
        pargs.admin_email = 'admin@example.com'
        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        ctrl.app = mock.Mock()
        ctrl.app.pargs = pargs

        with contextlib.ExitStack() as stack:
            for method in ('info', 'warn', 'error', 'debug'):
                stack.enter_context(mock.patch(f'wo.core.logging.Log.{method}'))
            stack.enter_context(mock.patch.object(mt.WODomain, 'validate', return_value=domain))
            stack.enter_context(mock.patch.object(mt, 'check_domain_exists', return_value=False))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch.object(
                mt.MTFunctions,
                'load_config',
                return_value={'shared_root': shared_root, 'admin_email': 'fallback@example.com'},
            ))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'preflight_shared_config', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_php_version', return_value='8.4'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_cache_type', return_value=cache_type))
            stack.enter_context(mock.patch.object(mt, 'site_package_check'))
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
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'generate_wp_config'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'generate_nginx_config', return_value='nginx.conf'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'install_wordpress'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'purge_site_cache'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'get_baseline_version', return_value=7))
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
            stack.enter_context(mock.patch.object(mt, 'setwebrootpermissions'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'validate_nginx_config', return_value=True))
            stack.enter_context(mock.patch.object(mt.WOFileUtils, 'create_symlink'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'safe_nginx_reload', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'get_current_release', return_value='current'))
            stack.enter_context(mock.patch.object(mt, 'addNewSite'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'add_shared_site'))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'update_site_baseline'))
            stack.enter_context(mock.patch.object(mt.WOGit, 'add'))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'get_admin_password', return_value='secret'))

            ctrl._create_impl()

        return {
            'apply_baseline': apply_baseline,
            'set_permalink_structure': set_permalink,
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


    def test_load_config_parses_wordpress_sources_without_legacy_defaults(self):
        """Source sections parse independently; removed legacy baseline keys stay absent."""
        conf = """
[multitenancy]
shared_root = /tmp/shared

[wordpress_plugins]
source-only = latest
akismet = 5.3

[wordpress_themes]
block-theme = latest

[github_plugins]
github-only = owner/repo,branch,main

[url_plugins]
url-only = https://example.com/url-only.zip
"""

        def read_config(parser, filenames, encoding=None):
            parser.read_file(io.StringIO(conf))
            return [filenames]

        with mock.patch.object(mtf.os.path, 'exists', return_value=True), \
                mock.patch.object(mtf.configparser.ConfigParser, 'read', read_config):
            config = MTFunctions.load_config(mock.Mock())

        self.assertNotIn('baseline_plugins', config)
        self.assertNotIn('baseline_theme', config)
        self.assertEqual(config['wordpress_plugins'], {
            'source-only': 'latest',
            'akismet': '5.3',
        })
        self.assertEqual(config['wordpress_themes'], {
            'block-theme': 'latest',
        })
        self.assertEqual(config['github_plugins'], {
            'github-only': 'owner/repo,branch,main',
        })
        self.assertEqual(config['url_plugins'], {
            'url-only': 'https://example.com/url-only.zip',
        })

    def test_seed_downloads_wordpress_source_sections_and_skips_external_duplicates(self):
        """WordPress.org downloads come from source sections and skip GitHub/URL duplicates."""
        infra = SharedInfrastructure(mock.Mock(), self.tmp)
        config = {
            'baseline_plugins': ['active-only'],
            'baseline_theme': 'legacy-theme',
            'wordpress_plugins': {
                'wp-source': 'latest',
                'github-duplicate': 'latest',
                'url-duplicate': 'latest',
            },
            'wordpress_themes': {
                'wp-theme': 'latest',
                'github-theme': 'latest',
                'url-theme': 'latest',
            },
            'github_plugins': {
                'github-duplicate': 'owner/repo,branch,main',
            },
            'url_plugins': {
                'url-duplicate': 'https://example.com/url-duplicate.zip',
            },
            'github_themes': {
                'github-theme': 'owner/theme,branch,main',
            },
            'url_themes': {
                'url-theme': 'https://example.com/url-theme.zip',
            },
        }

        with mock.patch.object(infra, 'download_plugin', return_value=True) as download_plugin, \
                mock.patch.object(infra, 'download_plugin_from_github', return_value=True) as download_github, \
                mock.patch.object(infra, 'download_plugin_from_url', return_value=True) as download_url, \
                mock.patch.object(infra, 'download_theme', return_value=True) as download_theme, \
                mock.patch.object(infra, 'download_theme_from_github', return_value=True) as download_theme_github, \
                mock.patch.object(infra, 'download_theme_from_url', return_value=True) as download_theme_url:
            failures = infra.seed_plugins_and_themes(config)

        self.assertEqual(failures, [])
        download_plugin.assert_called_once_with('wp-source', force=False)
        download_github.assert_called_once_with('owner/repo', 'github-duplicate', branch='main', force=False)
        download_url.assert_called_once_with(
            'https://example.com/url-duplicate.zip',
            'url-duplicate',
            force=False,
        )
        download_theme.assert_called_once_with('wp-theme', force=False)
        download_theme_github.assert_called_once_with('owner/theme', 'github-theme', branch='main', force=False)
        download_theme_url.assert_called_once_with(
            'https://example.com/url-theme.zip',
            'url-theme',
            force=False,
        )

    def test_seed_threads_force_true_to_all_download_helpers(self):
        """init --force path: seed passes force=True to every plugin/theme download helper."""
        infra = SharedInfrastructure(mock.Mock(), self.tmp)
        config = {
            'wordpress_plugins': {'wp-source': 'latest'},
            'github_plugins': {'gh-plugin': 'owner/repo,branch,main'},
            'url_plugins': {'url-plugin': 'https://example.com/url-plugin.zip'},
            'wordpress_themes': {'wp-theme': 'latest'},
            'github_themes': {'gh-theme': 'owner/theme,branch,main'},
            'url_themes': {'url-theme': 'https://example.com/url-theme.zip'},
        }

        with mock.patch.object(infra, 'download_plugin', return_value=True) as download_plugin, \
                mock.patch.object(infra, 'download_plugin_from_github', return_value=True) as download_github, \
                mock.patch.object(infra, 'download_plugin_from_url', return_value=True) as download_url, \
                mock.patch.object(infra, 'download_theme', return_value=True) as download_theme, \
                mock.patch.object(infra, 'download_theme_from_github', return_value=True) as download_theme_github, \
                mock.patch.object(infra, 'download_theme_from_url', return_value=True) as download_theme_url:
            failures = infra.seed_plugins_and_themes(config, force=True)

        self.assertEqual(failures, [])
        download_plugin.assert_called_once_with('wp-source', force=True)
        download_github.assert_called_once_with('owner/repo', 'gh-plugin', branch='main', force=True)
        download_url.assert_called_once_with(
            'https://example.com/url-plugin.zip',
            'url-plugin',
            force=True,
        )
        download_theme.assert_called_once_with('wp-theme', force=True)
        download_theme_github.assert_called_once_with('owner/theme', 'gh-theme', branch='main', force=True)
        download_theme_url.assert_called_once_with(
            'https://example.com/url-theme.zip',
            'url-theme',
            force=True,
        )

    def test_seed_legacy_config_without_source_sections_uses_legacy_baseline_keys(self):
        """Legacy configs without source sections seed baseline plugins and theme from WordPress.org."""
        infra = SharedInfrastructure(mock.Mock(), self.tmp)
        config = {
            'baseline_plugins': ['legacy-one', 'legacy-two'],
            'baseline_theme': 'legacy-theme',
        }

        with mock.patch.object(infra, 'download_plugin', return_value=True) as download_plugin, \
                mock.patch.object(infra, 'download_theme', return_value=True) as download_theme:
            failures = infra.seed_plugins_and_themes(config)

        self.assertEqual(failures, [])
        self.assertEqual(download_plugin.call_args_list, [
            mock.call('legacy-one', force=False),
            mock.call('legacy-two', force=False),
        ])
        download_theme.assert_called_once_with('legacy-theme', force=False)

    def test_create_baseline_config_leaves_existing_file_byte_identical(self):
        """bootstrap must never rewrite an operator-owned baseline.json."""
        infra = SharedInfrastructure(mock.Mock(), self.tmp)
        os.makedirs(infra.config_dir, exist_ok=True)
        baseline_file = os.path.join(infra.config_dir, 'baseline.json')
        original = b'{"version":7,"plugins":["operator"],"theme":"custom"}\n'
        with open(baseline_file, 'wb') as fh:
            fh.write(original)

        with mock.patch('wo.core.logging.Log.info'):
            ok = infra.create_baseline_config({
                'wordpress_plugins': {'new-plugin': 'latest'},
                'wordpress_themes': {'new-theme': 'latest'},
            })

        self.assertTrue(ok)
        with open(baseline_file, 'rb') as fh:
            self.assertEqual(fh.read(), original)

    def test_create_baseline_config_bootstraps_from_sources_with_ordered_dedupe(self):
        """new baseline.json seeds from source sections only when legacy keys are absent."""
        infra = SharedInfrastructure(mock.Mock(), self.tmp)
        os.makedirs(infra.config_dir, exist_ok=True)
        config = {
            'wordpress_plugins': {
                'wp-one': 'latest',
                'duplicate': 'latest',
            },
            'github_plugins': {
                'github-one': 'owner/github,branch,main',
                'duplicate': 'owner/duplicate,branch,main',
            },
            'url_plugins': {
                'url-one': 'https://example.com/url-one.zip',
                'wp-one': 'https://example.com/wp-one.zip',
            },
            'wordpress_themes': {
                'wp-theme': 'latest',
            },
            'github_themes': {
                'site-child': 'owner/site-child,branch,main',
            },
            'url_themes': {
                'url-theme': 'https://example.com/url-theme.zip',
            },
        }

        with mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok = infra.create_baseline_config(config)

        self.assertTrue(ok)
        with open(os.path.join(infra.config_dir, 'baseline.json')) as fh:
            baseline = json.load(fh)

        self.assertEqual(baseline['version'], 1)
        self.assertEqual(baseline['plugins'], [
            'wp-one',
            'duplicate',
            'github-one',
            'url-one',
        ])
        self.assertEqual(baseline['theme'], 'wp-theme')
        self.assertEqual(baseline['options'], {
            'blog_public': 1,
            'default_comment_status': 'closed',
            'default_ping_status': 'closed',
        })

    def test_create_baseline_config_theme_fallback_prefers_child_github_then_first_source(self):
        """theme bootstrap falls back from WordPress.org to -child GitHub to first GitHub/URL source."""
        cases = [
            (
                'github-child',
                {
                    'github_themes': {
                        'parent-theme': 'owner/parent,branch,main',
                        'parent-child': 'owner/child,branch,main',
                    },
                    'url_themes': {
                        'url-theme': 'https://example.com/url-theme.zip',
                    },
                },
                'parent-child',
            ),
            (
                'first-github',
                {
                    'github_themes': {
                        'github-theme': 'owner/theme,branch,main',
                    },
                    'url_themes': {
                        'url-theme': 'https://example.com/url-theme.zip',
                    },
                },
                'github-theme',
            ),
            (
                'first-url',
                {
                    'url_themes': {
                        'url-theme': 'https://example.com/url-theme.zip',
                    },
                },
                'url-theme',
            ),
        ]

        for name, config, expected_theme in cases:
            with self.subTest(name=name):
                case_tmp = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, case_tmp, ignore_errors=True)
                infra = SharedInfrastructure(mock.Mock(), case_tmp)
                os.makedirs(infra.config_dir, exist_ok=True)

                with mock.patch('wo.core.logging.Log.info'), \
                        mock.patch('wo.core.logging.Log.debug'):
                    ok = infra.create_baseline_config(config)

                self.assertTrue(ok)
                with open(os.path.join(infra.config_dir, 'baseline.json')) as fh:
                    baseline = json.load(fh)
                self.assertEqual(baseline['plugins'], [])
                self.assertEqual(baseline['theme'], expected_theme)

    def test_create_baseline_config_empty_sources_yields_empty_activation_baseline(self):
        """no legacy keys and no source slugs bootstrap an intentionally empty baseline."""
        infra = SharedInfrastructure(mock.Mock(), self.tmp)
        os.makedirs(infra.config_dir, exist_ok=True)
        config = {
            'wordpress_plugins': {},
            'github_plugins': {},
            'url_plugins': {},
            'wordpress_themes': {},
            'github_themes': {},
            'url_themes': {},
        }

        with mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok = infra.create_baseline_config(config)

        self.assertTrue(ok)
        with open(os.path.join(infra.config_dir, 'baseline.json')) as fh:
            baseline = json.load(fh)
        self.assertEqual(baseline['plugins'], [])
        self.assertEqual(baseline['theme'], '')

    def test_create_baseline_config_legacy_keys_win_as_bootstrap_seeds(self):
        """legacy activation keys seed the first baseline even when source sections exist."""
        infra = SharedInfrastructure(mock.Mock(), self.tmp)
        os.makedirs(infra.config_dir, exist_ok=True)
        config = {
            'baseline_plugins': ['legacy-active', 'legacy-github'],
            'baseline_theme': 'legacy-theme',
            'wordpress_plugins': {
                'wp-source-only': 'latest',
            },
            'github_plugins': {
                'legacy-github': 'owner/active,branch,main',
                'github-source-only': 'owner/source,branch,main',
            },
            'url_plugins': {
                'url-source-only': 'https://example.com/url-source-only.zip',
            },
            'wordpress_themes': {
                'wp-theme': 'latest',
            },
            'github_themes': {
                'site-child': 'owner/site-child,branch,main',
            },
        }

        with mock.patch('wo.core.logging.Log.debug'):
            ok = infra.create_baseline_config(config)

        self.assertTrue(ok)
        with open(os.path.join(infra.config_dir, 'baseline.json')) as fh:
            baseline = json.load(fh)

        self.assertEqual(baseline['plugins'], ['legacy-active', 'legacy-github'])
        self.assertEqual(baseline['theme'], 'legacy-theme')




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

    def test_purge_is_best_effort_on_subprocess_error(self):
        with mock.patch('wo.cli.plugins.multitenancy_functions.os.path.isdir', return_value=True), \
                mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run', side_effect=OSError('boom')), \
                mock.patch('wo.cli.plugins.multitenancy_functions.Log.debug'):
            # Best-effort: a failing purge must never abort provisioning.
            MTFunctions.purge_site_cache(mock.Mock(), 'example.com', redis_prefix='example_com_')


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
            ctrl._delete_impl()
        return purge, ctrl

    def test_delete_purges_cache_on_success(self):
        purge, ctrl = self._run_delete(returncode=0)
        purge.assert_called_once_with(ctrl, 'example.com', 'example_com_')

    def test_delete_skips_purge_when_site_delete_fails(self):
        purge, ctrl = self._run_delete(returncode=1)
        purge.assert_not_called()


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
            result = BaselineApplicator.apply_baseline_to_sites(
                self.app, self.config, baseline_version=7,
            )

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
                self.app,
                self.config,
                baseline_version=7,
                dry_run=True,
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
                self.app,
                self.config,
                baseline_version=7,
                dry_run=True,
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
                self.app,
                self.config,
                baseline_version=7,
                dry_run=True,
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
            result = BaselineApplicator.apply_baseline_to_sites(
                self.app, self.config, baseline_version=7,
            )

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
                self.app,
                self.config,
                baseline_version=7,
                dry_run=True,
                prune=True,
            )

        self.assertEqual(result['status'], 'dry_run')
        get_active_plugin_slugs.assert_called_once()
        self.assertEqual(
            get_active_plugin_slugs.call_args.args[1],
            '/var/www/example.com/htdocs',
        )

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



class PluginThemeUpdateTests(unittest.TestCase):
    """Focused tests for shared plugin/theme source updates."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _infra(self, root=None):
        return SharedInfrastructure(mock.Mock(), root or self.tmp)

    def _write_baseline(self, baseline, root=None):
        root = root or self.tmp
        config_dir = os.path.join(root, 'config')
        os.makedirs(config_dir, exist_ok=True)
        baseline_file = os.path.join(config_dir, 'baseline.json')
        with open(baseline_file, 'w') as fh:
            json.dump(baseline, fh)
        return baseline_file

    def _read_baseline(self, root=None):
        with open(os.path.join(root or self.tmp, 'config', 'baseline.json')) as fh:
            return json.load(fh)

    def _patch_logs(self, stack):
        for method in ('info', 'warn', 'error', 'debug'):
            stack.enter_context(mock.patch(f'wo.core.logging.Log.{method}'))

    def test_load_config_keeps_bare_github_repo_values(self):
        conf = """
[multitenancy]
shared_root = /tmp/shared

[github_plugins]
bare = owner/repo

[github_themes]
bare-theme = owner/theme
"""

        def read_config(parser, filenames, encoding=None):
            parser.read_file(io.StringIO(conf))
            return [filenames]

        with mock.patch.object(mtf.os.path, 'exists', return_value=True), \
                mock.patch.object(mtf.configparser.ConfigParser, 'read', read_config):
            config = MTFunctions.load_config(mock.Mock())

        self.assertEqual(config['github_plugins'], {'bare': 'owner/repo'})
        self.assertEqual(config['github_themes'], {'bare-theme': 'owner/theme'})

    def test_create_baseline_config_writes_sources_from_config_sections(self):
        infra = self._infra()
        os.makedirs(infra.config_dir, exist_ok=True)
        config = {
            'wordpress_plugins': {'wp-plugin': '5.3'},
            'github_plugins': {'github-plugin': 'owner/repo,branch,main'},
            'url_plugins': {'url-plugin': 'https://example.com/plugin.zip'},
            'wordpress_themes': {'wp-theme': 'latest'},
            'github_themes': {'github-theme': 'owner/theme,tag,1.2.3'},
            'url_themes': {'url-theme': 'https://example.com/theme.zip'},
        }

        with mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.debug'), \
                mock.patch('wo.core.logging.Log.warn'):
            self.assertTrue(infra.create_baseline_config(config))

        baseline = self._read_baseline()
        self.assertEqual(baseline['sources']['plugins'], {
            'wp-plugin': {'type': 'wordpress', 'version': '5.3'},
            'github-plugin': {
                'type': 'github',
                'repo': 'owner/repo',
                'ref_type': 'branch',
                'ref': 'main',
            },
            'url-plugin': {'type': 'url', 'url': 'https://example.com/plugin.zip'},
        })
        self.assertEqual(baseline['sources']['themes'], {
            'wp-theme': {'type': 'wordpress', 'version': 'latest'},
            'github-theme': {
                'type': 'github',
                'repo': 'owner/theme',
                'ref_type': 'tag',
                'ref': '1.2.3',
            },
            'url-theme': {'type': 'url', 'url': 'https://example.com/theme.zip'},
        })

    def test_update_plugin_uses_baseline_github_source(self):
        infra = self._infra()
        self._write_baseline({
            'plugins': ['custom'],
            'sources': {
                'plugins': {
                    'custom': {
                        'type': 'github',
                        'repo': 'owner/repo',
                        'ref_type': 'branch',
                        'ref': 'main',
                    },
                },
            },
        })

        with mock.patch.object(SharedInfrastructure, 'download_plugin_from_github', return_value=True) as download, \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.error'), \
                mock.patch('wo.core.logging.Log.debug'):
            self.assertTrue(infra.update_plugin('custom', config={}))

        download.assert_called_once_with(
            'owner/repo',
            'custom',
            branch='main',
            force=True,
            backup_records=mock.ANY,
        )

    def test_update_plugin_falls_back_to_config_url_source(self):
        infra = self._infra()
        self._write_baseline({'plugins': ['custom']})

        with mock.patch.object(SharedInfrastructure, 'download_plugin_from_url', return_value=True) as download, \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.error'), \
                mock.patch('wo.core.logging.Log.debug'):
            self.assertTrue(infra.update_plugin(
                'custom',
                config={'url_plugins': {'custom': 'https://example.com/custom.zip'}},
            ))

        download.assert_called_once_with(
            'https://example.com/custom.zip',
            'custom',
            force=True,
            backup_records=mock.ANY,
        )

    def test_update_plugin_unknown_source_fails_without_guessing_wordpress_org(self):
        infra = self._infra()
        self._write_baseline({'plugins': ['premium']})

        with mock.patch.object(SharedInfrastructure, 'download_plugin') as wp, \
                mock.patch.object(SharedInfrastructure, 'download_plugin_from_github') as github, \
                mock.patch.object(SharedInfrastructure, 'download_plugin_from_url') as url, \
                mock.patch('wo.core.logging.Log.error'), \
                mock.patch('wo.core.logging.Log.debug'):
            self.assertFalse(infra.update_plugin('premium', config={}))

        wp.assert_not_called()
        github.assert_not_called()
        url.assert_not_called()

    def test_update_theme_uses_baseline_theme_when_slug_missing(self):
        infra = self._infra()
        self._write_baseline({
            'theme': 'woodmart-child',
            'sources': {
                'themes': {
                    'woodmart-child': {
                        'type': 'github',
                        'repo': 'owner/theme',
                        'ref_type': 'branch',
                        'ref': 'main',
                    },
                },
            },
        })

        with mock.patch.object(SharedInfrastructure, 'download_theme_from_github', return_value=True) as download, \
                mock.patch('wo.core.logging.Log.info'), \
                mock.patch('wo.core.logging.Log.error'), \
                mock.patch('wo.core.logging.Log.debug'):
            self.assertTrue(infra.update_theme(config={}))

        download.assert_called_once_with(
            'owner/theme',
            'woodmart-child',
            branch='main',
            force=True,
            backup_records=mock.ANY,
        )

    def test_update_plugins_and_themes_dispatches_all_source_types(self):
        infra = self._infra()
        config = {
            'wordpress_plugins': {'wp-source': '5.3'},
            'github_plugins': {'github-source': 'owner/repo,branch,main'},
            'url_plugins': {'url-source': 'https://example.com/url-source.zip'},
            'wordpress_themes': {'wp-theme': '6.0'},
            'github_themes': {'github-theme': 'owner/theme,tag,1.2.3'},
            'url_themes': {'url-theme': 'https://example.com/url-theme.zip'},
        }

        with mock.patch.object(SharedInfrastructure, 'download_plugin', return_value=True) as download_plugin, \
                mock.patch.object(SharedInfrastructure, 'download_plugin_from_github', return_value=True) as download_github, \
                mock.patch.object(SharedInfrastructure, 'download_plugin_from_url', return_value=True) as download_url, \
                mock.patch.object(SharedInfrastructure, 'download_theme', return_value=True) as download_theme, \
                mock.patch.object(SharedInfrastructure, 'download_theme_from_github', return_value=True) as download_theme_github, \
                mock.patch.object(SharedInfrastructure, 'download_theme_from_url', return_value=True) as download_theme_url, \
                mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.error'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok, backup_records = infra.update_plugins_and_themes(config)

        self.assertTrue(ok)
        self.assertIsInstance(backup_records, list)
        download_plugin.assert_called_once_with(
            'wp-source',
            version='5.3',
            force=True,
            backup_records=mock.ANY,
        )
        download_github.assert_called_once_with(
            'owner/repo',
            'github-source',
            branch='main',
            force=True,
            backup_records=mock.ANY,
        )
        download_url.assert_called_once_with(
            'https://example.com/url-source.zip',
            'url-source',
            force=True,
            backup_records=mock.ANY,
        )
        download_theme.assert_called_once_with(
            'wp-theme',
            version='6.0',
            force=True,
            backup_records=mock.ANY,
        )
        download_theme_github.assert_called_once_with(
            'owner/theme',
            'github-theme',
            tag='1.2.3',
            force=True,
            backup_records=mock.ANY,
        )
        download_theme_url.assert_called_once_with(
            'https://example.com/url-theme.zip',
            'url-theme',
            force=True,
            backup_records=mock.ANY,
        )

    def test_update_plugins_and_themes_warns_and_skips_unknown_baseline_slug(self):
        infra = self._infra()
        self._write_baseline({
            'plugins': ['known', 'unknown'],
            'sources': {
                'plugins': {
                    'known': {'type': 'wordpress', 'version': 'latest'},
                },
            },
        })

        with mock.patch.object(infra, '_dispatch_download', return_value=True) as dispatch, \
                mock.patch('wo.core.logging.Log.warn') as log_warn, \
                mock.patch('wo.core.logging.Log.error'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok, backup_records = infra.update_plugins_and_themes({})

        self.assertTrue(ok)
        self.assertIsInstance(backup_records, list)
        dispatch.assert_called_once_with(
            'plugin',
            'known',
            {'type': 'wordpress', 'version': 'latest'},
            force=True,
            backup_records=mock.ANY,
        )
        log_warn.assert_called_once_with(
            infra.app,
            'No download source configured for plugin unknown; skipping',
        )

    def test_update_plugins_and_themes_restores_successes_when_later_item_fails(self):
        infra = self._infra()
        self._write_baseline({
            'plugins': ['first', 'second'],
            'sources': {
                'plugins': {
                    'first': {'type': 'wordpress', 'version': 'latest'},
                    'second': {'type': 'wordpress', 'version': 'latest'},
                },
            },
        })
        record = {
            'kind': 'plugin',
            'slug': 'first',
            'target': os.path.join(self.tmp, 'wp-content', 'plugins', 'first'),
            'backup': os.path.join(self.tmp, 'backups', 'first'),
        }

        def dispatch(kind, slug, source, force=False, backup_records=None):
            if slug == 'first':
                backup_records.append(record)
                return True
            return False

        with mock.patch.object(infra, '_dispatch_download', side_effect=dispatch) as dispatch_mock, \
                mock.patch.object(infra, 'restore_asset_backups', return_value=True) as restore, \
                mock.patch('wo.core.logging.Log.error'), \
                mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.debug'):
            result = infra.update_plugins_and_themes({})

        self.assertEqual(result, (False, []))
        self.assertEqual(dispatch_mock.call_count, 2)
        restore.assert_called_once_with([record])

    def test_promote_asset_force_restores_existing_on_failed_rename(self):
        infra = self._infra()
        target = os.path.join(self.tmp, 'wp-content', 'plugins', 'demo')
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, 'old.txt'), 'w') as fh:
            fh.write('old')
        staged_parent = os.path.join(self.tmp, 'tmp', 'assets')
        staged_dir = os.path.join(staged_parent, 'wo_plugin_demo_test')
        os.makedirs(staged_dir, exist_ok=True)
        with open(os.path.join(staged_dir, 'new.txt'), 'w') as fh:
            fh.write('new')
        real_rename = os.rename

        def fail_staged_to_target(src, dst):
            if src == staged_dir:
                raise OSError('promote failed')
            return real_rename(src, dst)

        backup_records = []
        with mock.patch.object(mtf.os, 'rename', side_effect=fail_staged_to_target), \
                mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.debug'):
            ok = infra._promote_asset('plugin', 'demo', staged_dir, force=True, backup_records=backup_records)

        self.assertFalse(ok)
        self.assertEqual(backup_records, [])
        self.assertTrue(os.path.exists(os.path.join(target, 'old.txt')))

    def test_restore_asset_backups_reverts_successful_promotions(self):
        infra = self._infra()
        plugin_target = os.path.join(self.tmp, 'wp-content', 'plugins', 'demo')
        theme_target = os.path.join(self.tmp, 'wp-content', 'themes', 'child')
        os.makedirs(plugin_target, exist_ok=True)
        os.makedirs(theme_target, exist_ok=True)
        with open(os.path.join(plugin_target, 'old-plugin.txt'), 'w') as fh:
            fh.write('old plugin')
        with open(os.path.join(theme_target, 'old-theme.txt'), 'w') as fh:
            fh.write('old theme')
        staged_parent = os.path.join(self.tmp, 'tmp', 'assets')
        plugin_staged = os.path.join(staged_parent, 'plugin-staged')
        theme_staged = os.path.join(staged_parent, 'theme-staged')
        os.makedirs(plugin_staged, exist_ok=True)
        os.makedirs(theme_staged, exist_ok=True)
        with open(os.path.join(plugin_staged, 'new-plugin.txt'), 'w') as fh:
            fh.write('new plugin')
        with open(os.path.join(theme_staged, 'new-theme.txt'), 'w') as fh:
            fh.write('new theme')
        records = []

        with mock.patch('wo.core.logging.Log.warn'), \
                mock.patch('wo.core.logging.Log.debug'):
            self.assertTrue(infra._promote_asset('plugin', 'demo', plugin_staged, force=True, backup_records=records))
            self.assertTrue(infra._promote_asset('theme', 'child', theme_staged, force=True, backup_records=records))
            self.assertTrue(infra.restore_asset_backups(records))

        self.assertTrue(os.path.exists(os.path.join(plugin_target, 'old-plugin.txt')))
        self.assertFalse(os.path.exists(os.path.join(plugin_target, 'new-plugin.txt')))
        self.assertTrue(os.path.exists(os.path.join(theme_target, 'old-theme.txt')))
        self.assertFalse(os.path.exists(os.path.join(theme_target, 'new-theme.txt')))

    def test_wordpress_download_uses_version_pin_and_latest_stable_urls(self):
        urls = []

        def run_download(root, method_name, slug, version):
            infra = self._infra(root)
            os.makedirs(os.path.join(root, 'wp-content', 'plugins'), exist_ok=True)
            os.makedirs(os.path.join(root, 'wp-content', 'themes'), exist_ok=True)
            def fake_run(argv, **kwargs):
                if argv[0] == 'curl':
                    urls.append(argv[-1])
                    with open(argv[3], 'wb') as fh:
                        fh.write(b'PK\x03\x04')
                    return mock.Mock(returncode=0, stdout='', stderr='')
                if argv[0] == 'unzip':
                    extract_dir = argv[-1]
                    os.makedirs(os.path.join(extract_dir, slug), exist_ok=True)
                    return mock.Mock(returncode=0, stdout='', stderr='')
                raise AssertionError(f'unexpected command: {argv}')

            with mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run', side_effect=fake_run), \
                    mock.patch('wo.core.logging.Log.debug'), \
                    mock.patch('wo.core.logging.Log.warn'):
                return getattr(infra, method_name)(slug, version=version)

        self.assertTrue(run_download(os.path.join(self.tmp, 'pinned-plugin'), 'download_plugin', 'akismet', '5.3'))
        self.assertTrue(run_download(os.path.join(self.tmp, 'latest-plugin'), 'download_plugin', 'akismet', 'latest'))
        self.assertTrue(run_download(os.path.join(self.tmp, 'pinned-theme'), 'download_theme', 'twentytwentyfour', '6.0'))

        self.assertIn('https://downloads.wordpress.org/plugin/akismet.5.3.zip', urls)
        self.assertIn('https://downloads.wordpress.org/plugin/akismet.latest-stable.zip', urls)
        self.assertIn('https://downloads.wordpress.org/theme/twentytwentyfour.6.0.zip', urls)

    def test_wordpress_download_unzip_failure_preserves_existing_target(self):
        infra = self._infra()
        target = os.path.join(self.tmp, 'wp-content', 'plugins', 'akismet')
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, 'old.txt'), 'w') as fh:
            fh.write('old')
        backup_records = []

        def fake_run(argv, **kwargs):
            if argv[0] == 'curl':
                with open(argv[3], 'wb') as fh:
                    fh.write(b'PK\x03\x04')
                return mock.Mock(returncode=0, stdout='', stderr='')
            if argv[0] == 'unzip':
                return mock.Mock(returncode=2, stdout='', stderr='bad zip')
            raise AssertionError(f'unexpected command: {argv}')

        with mock.patch('wo.cli.plugins.multitenancy_functions.subprocess.run', side_effect=fake_run), \
                mock.patch('wo.core.logging.Log.debug'), \
                mock.patch('wo.core.logging.Log.warn'):
            ok = infra.download_plugin('akismet', version='latest', force=True, backup_records=backup_records)

        self.assertFalse(ok)
        self.assertTrue(os.path.exists(os.path.join(target, 'old.txt')))
        self.assertEqual(backup_records, [])

    def _make_asset_backup(self, stamp, kind, slug, root=None):
        root = root or self.tmp
        d = os.path.join(root, 'backups', 'assets', stamp, f'{kind}s', slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'marker.txt'), 'w') as fh:
            fh.write(stamp)
        return d

    def test_prune_asset_backups_keeps_newest_per_asset(self):
        infra = self._infra()
        for stamp in ('20260101-000000-000001', '20260102-000000-000002',
                      '20260103-000000-000003'):
            self._make_asset_backup(stamp, 'plugin', 'alpha')
        for stamp in ('20260101-000000-000001', '20260105-000000-000005'):
            self._make_asset_backup(stamp, 'theme', 'child')
        self._make_asset_backup('20260104-000000-000004', 'plugin', 'beta')

        infra.prune_asset_backups(2)

        base = os.path.join(self.tmp, 'backups', 'assets')

        def stamps_for(kind, slug):
            return sorted(
                stamp for stamp in os.listdir(base)
                if os.path.isdir(os.path.join(base, stamp, f'{kind}s', slug)))

        # alpha had 3 backups -> keep the 2 newest, drop the oldest.
        self.assertEqual(stamps_for('plugin', 'alpha'),
                         ['20260102-000000-000002', '20260103-000000-000003'])
        # child (2) and beta (1) are within keep and untouched.
        self.assertEqual(stamps_for('theme', 'child'),
                         ['20260101-000000-000001', '20260105-000000-000005'])
        self.assertEqual(stamps_for('plugin', 'beta'), ['20260104-000000-000004'])
        # The emptied plugins/ dir under the shared 01 stamp is removed, but the
        # stamp dir survives because theme 'child' still lives there.
        self.assertFalse(os.path.isdir(
            os.path.join(base, '20260101-000000-000001', 'plugins')))
        self.assertTrue(os.path.isdir(
            os.path.join(base, '20260101-000000-000001', 'themes', 'child')))

    def test_prune_asset_backups_zero_removes_all_negative_disables(self):
        infra = self._infra()
        self._make_asset_backup('20260101-000000-000001', 'plugin', 'alpha')
        self._make_asset_backup('20260102-000000-000002', 'plugin', 'alpha')
        base = os.path.join(self.tmp, 'backups', 'assets')

        infra.prune_asset_backups(-1)  # disabled: nothing removed
        self.assertEqual(sorted(os.listdir(base)),
                         ['20260101-000000-000001', '20260102-000000-000002'])

        infra.prune_asset_backups(0)  # remove every backup, clean empty dirs
        self.assertEqual(os.listdir(base), [])

    def test_controller_bulk_update_restores_assets_on_canary_failure(self):
        if mt is None:
            self.skipTest(f'multitenancy controller import unavailable: {_mt_import_error}')
        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        pargs = mock.Mock()
        pargs.force = False
        ctrl.app = mock.Mock()
        ctrl.app.pargs = pargs
        asset_records = [{
            'kind': 'plugin',
            'slug': 'x',
            'target': '/tmp/x',
            'backup': '/tmp/b',
        }]

        with contextlib.ExitStack() as stack:
            self._patch_logs(stack)
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'get_shared_sites', return_value=[{'domain': 'example.com'}]))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'load_config', return_value={'shared_root': self.tmp}))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'preflight_shared_config', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'test_site', return_value=False))
            stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'download_wordpress_core', return_value='wp-test'))
            stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'update_plugins_and_themes', return_value=(True, asset_records)))
            restore = stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'restore_asset_backups', return_value=True))
            switch_release = stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'switch_release'))
            stack.enter_context(mock.patch.object(mt.ReleaseManager, 'backup_current'))
            ctrl.update()

        restore.assert_called_once_with(asset_records)
        switch_release.assert_not_called()

    def test_add_plugin_github_records_source_in_baseline(self):
        if mt is None:
            self.skipTest(f'multitenancy controller import unavailable: {_mt_import_error}')
        self._write_baseline({'version': 2, 'plugins': [], 'theme': 'active', 'sources': {}})
        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        pargs = mock.Mock()
        pargs.plugin_slug = 'custom'
        pargs.site_name = None
        pargs.apply_now = False
        pargs.github = 'owner/repo'
        pargs.branch = 'main'
        pargs.tag = None
        pargs.url = None
        ctrl.app = mock.Mock()
        ctrl.app.pargs = pargs

        def download(github_repo, plugin_slug, branch=None, tag=None):
            plugin_dir = os.path.join(self.tmp, 'wp-content', 'plugins', plugin_slug)
            os.makedirs(plugin_dir, exist_ok=True)
            return True

        with contextlib.ExitStack() as stack:
            self._patch_logs(stack)
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'load_config', return_value={'shared_root': self.tmp}))
            stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'download_plugin_from_github', side_effect=download))
            stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'git_commit_baseline', return_value=True))
            ctrl.add_plugin()

        baseline = self._read_baseline()
        self.assertEqual(baseline['version'], 3)
        self.assertIn('custom', baseline['plugins'])
        self.assertEqual(baseline['sources']['plugins']['custom'], {
            'type': 'github',
            'repo': 'owner/repo',
            'ref_type': 'branch',
            'ref': 'main',
        })

    def test_add_theme_url_records_source_in_baseline(self):
        if mt is None:
            self.skipTest(f'multitenancy controller import unavailable: {_mt_import_error}')
        self._write_baseline({'version': 2, 'plugins': [], 'theme': 'active', 'sources': {}})
        ctrl = mt.WOMultitenancyController.__new__(mt.WOMultitenancyController)
        pargs = mock.Mock()
        pargs.theme_slug = 'custom-theme'
        pargs.site_name = None
        pargs.set_default = False
        pargs.apply_now = False
        pargs.github = None
        pargs.branch = None
        pargs.tag = None
        pargs.url = 'https://example.com/custom-theme.zip'
        ctrl.app = mock.Mock()
        ctrl.app.pargs = pargs

        def download(url, theme_slug):
            theme_dir = os.path.join(self.tmp, 'wp-content', 'themes', theme_slug)
            os.makedirs(theme_dir, exist_ok=True)
            return True

        with contextlib.ExitStack() as stack:
            self._patch_logs(stack)
            stack.enter_context(mock.patch.object(mt.MTDatabase, 'is_initialized', return_value=True))
            stack.enter_context(mock.patch.object(mt.MTFunctions, 'load_config', return_value={'shared_root': self.tmp}))
            stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'download_theme_from_url', side_effect=download))
            stack.enter_context(mock.patch.object(mt.SharedInfrastructure, 'git_commit_baseline', return_value=True))
            ctrl.add_theme()

        baseline = self._read_baseline()
        self.assertEqual(baseline['version'], 3)
        self.assertEqual(baseline['theme'], 'active')
        self.assertEqual(baseline['sources']['themes']['custom-theme'], {
            'type': 'url',
            'url': 'https://example.com/custom-theme.zip',
        })
if __name__ == '__main__':
    unittest.main()
