"""Multitenancy helper unit tests (stdlib unittest + mock, no live stack).

Covers the two safety-relevant behaviors introduced by the simplification
patch: the shared-config `php -l` preflight, and the admin-IP-free maintenance
render context. Run with:

    python3 -m unittest tests.cli.40_test_multitenancy -v
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from wo.cli.plugins.multitenancy_functions import MTFunctions
from wo.cli.plugins import multitenancy_functions as mtf
from wo.cli.plugins import multitenancy as mt


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
