"""Multitenancy helper unit tests (stdlib unittest + mock, no live stack).

Covers the two safety-relevant behaviors introduced by the simplification
patch: the shared-config `php -l` preflight, and the admin-IP-free maintenance
render context. Run with:

    python3 -m unittest tests.cli.40_test_multitenancy -v
"""

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
        download_plugin.assert_called_once_with('wp-source')
        download_github.assert_called_once_with('owner/repo', 'github-duplicate', branch='main')
        download_url.assert_called_once_with(
            'https://example.com/url-duplicate.zip',
            'url-duplicate',
        )
        download_theme.assert_called_once_with('wp-theme')
        download_theme_github.assert_called_once_with('owner/theme', 'github-theme', branch='main')
        download_theme_url.assert_called_once_with(
            'https://example.com/url-theme.zip',
            'url-theme',
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
            mock.call('legacy-one'),
            mock.call('legacy-two'),
        ])
        download_theme.assert_called_once_with('legacy-theme')

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



class BaselineApplicatorTests(unittest.TestCase):

    def setUp(self):
        self.app = mock.Mock()
        self.site_path = '/var/www/example.com/htdocs'

    def _wp_result(self, returncode=0, stdout='', stderr=''):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

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
                mock.patch.object(BaselineApplicator, 'restore_plugins_from_json') as restore_plugins, \
                mock.patch.object(mtf.subprocess, 'run', side_effect=run_wp) as run, \
                mock.patch('wo.core.logging.Log.warn') as log_warn:
            result = BaselineApplicator.apply_baseline_to_site(
                self.app, 'example.com', self.site_path, baseline
            )

        self.assertEqual(result, {'success': True, 'error': None})
        restore_plugins.assert_not_called()
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

        self.assertEqual(result, {'success': True, 'error': None})
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

        self.assertEqual(result, {'success': True, 'error': None})

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

if __name__ == '__main__':
    unittest.main()
