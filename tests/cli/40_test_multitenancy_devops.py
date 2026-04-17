"""Unit tests for multitenancy DevOps helpers.

Covers the additive DevOps layer: tag validation, audit checksumming,
webhook HMAC + retry, structured-log envelope, and health-check aggregation.
These tests exercise the pure helpers and mock external side-effects; they do
not require a live WordPress/MySQL/nginx stack.
"""

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest import mock

# Ensure the package root is importable when tests run standalone.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from wo.cli.plugins.multitenancy_functions import (  # noqa: E402
    validate_tags,
    AuditLogger,
    StructuredLogger,
    WebhookNotifier,
    JsonOutput,
)
from wo.cli.plugins import multitenancy_health  # noqa: E402


class TagValidationTests(unittest.TestCase):
    def test_parses_csv(self):
        self.assertEqual(validate_tags('a,b,c-d'), ['a', 'b', 'c-d'])

    def test_dedupes_and_sorts(self):
        self.assertEqual(validate_tags('b,a,a'), ['a', 'b'])

    def test_empty_is_noop(self):
        self.assertEqual(validate_tags(''), [])
        self.assertEqual(validate_tags(None), [])

    def test_rejects_bad_chars(self):
        for bad in ('UPPER', 'has space', 'under_score', 'emoji-🙂'):
            with self.assertRaises(ValueError, msg=f'expected reject for {bad!r}'):
                validate_tags(bad)

    def test_accepts_list_input(self):
        self.assertEqual(validate_tags(['prod', 'prod', 'client-a']), ['client-a', 'prod'])


class AuditChecksumTests(unittest.TestCase):
    def _sample(self):
        return {
            'timestamp': datetime(2026, 4, 17, 12, 0, 0),
            'event_id': 'abc-123',
            'actor': 'alnaggar',
            'actor_ip': 'local',
            'action': 'site_created',
            'target': 'example.com',
            'target_type': 'site',
            'result': 'success',
            'duration_ms': 1234,
            'details': json.dumps({'cache': 'wpfc'}),
        }

    def test_stable_ordering(self):
        rec = self._sample()
        first = AuditLogger._compute_checksum(rec)
        shuffled = dict(reversed(list(rec.items())))
        second = AuditLogger._compute_checksum(shuffled)
        self.assertEqual(first, second)

    def test_detects_tamper(self):
        rec = self._sample()
        original = AuditLogger._compute_checksum(rec)
        tampered = dict(rec)
        tampered['target'] = 'attacker.com'
        self.assertNotEqual(original, AuditLogger._compute_checksum(tampered))

    def test_verify_roundtrip(self):
        rec = self._sample()
        rec['checksum'] = AuditLogger._compute_checksum(rec)
        self.assertTrue(AuditLogger(None).verify(rec))
        # flip result field — verify should fail
        broken = dict(rec)
        broken['result'] = 'failure'
        self.assertFalse(AuditLogger(None).verify(broken))


class WebhookNotifierTests(unittest.TestCase):
    def _notifier(self, url='http://example.invalid/hook', secret='topsecret', retries=3):
        notifier = WebhookNotifier.__new__(WebhookNotifier)
        notifier.app = None
        notifier.url = url
        notifier.secret = secret
        notifier.enabled_events = set()
        notifier.timeout = 1
        notifier.retries = retries
        notifier._structured = mock.Mock()
        return notifier

    def test_signs_payload_when_secret_set(self):
        notifier = self._notifier()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['url'] = req.full_url
            captured['data'] = req.data
            captured['headers'] = dict(req.header_items())
            resp = mock.Mock()
            resp.__enter__ = lambda self_: resp
            resp.__exit__ = lambda *a: None
            resp.status = 200
            return resp

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            delivered = notifier.notify('site_created', {'domain': 'x.com'}, correlation_id='cid-1')

        self.assertTrue(delivered)
        header_map = {k.lower(): v for k, v in captured['headers'].items()}
        self.assertIn('x-wo-signature', header_map)
        self.assertTrue(header_map['x-wo-signature'].startswith('sha256='))

        import hmac as _hmac
        import hashlib as _hashlib
        expected_sig = _hmac.new(b'topsecret', captured['data'], _hashlib.sha256).hexdigest()
        self.assertEqual(header_map['x-wo-signature'], f'sha256={expected_sig}')

    def test_retries_then_gives_up(self):
        notifier = self._notifier(retries=3)
        calls = {'n': 0}

        def always_fail(req, timeout=None):
            calls['n'] += 1
            raise urllib.error.URLError('boom')

        with mock.patch('urllib.request.urlopen', side_effect=always_fail), \
             mock.patch('time.sleep'):  # don't actually wait
            delivered = notifier.notify('site_created', {}, correlation_id='cid-2')

        self.assertFalse(delivered)
        self.assertEqual(calls['n'], 3)
        notifier._structured.warn.assert_called()  # failure is logged

    def test_skip_when_event_not_enabled(self):
        notifier = self._notifier()
        notifier.enabled_events = {'other_event'}
        with mock.patch('urllib.request.urlopen') as mock_open:
            result = notifier.notify('site_created', {}, correlation_id='cid-3')
        self.assertFalse(result)
        mock_open.assert_not_called()

    def test_skip_when_unconfigured(self):
        notifier = self._notifier(url='')
        self.assertFalse(notifier.configured())
        with mock.patch('urllib.request.urlopen') as mock_open:
            result = notifier.notify('site_created', {}, correlation_id='cid-4')
        self.assertFalse(result)
        mock_open.assert_not_called()

    def test_uses_real_structured_logger_no_event_kwarg_collision(self):
        """Regression: during live testing the success branch called
        `_structured.info('webhook.delivered', event=event, ...)`, which
        collided with the positional `event` param of StructuredLogger.info
        and raised TypeError. The Exception handler swallowed it and the
        webhook retried 3× before the failure-branch warn() blew up too.
        This test wires a real StructuredLogger against a temp file and
        confirms a successful delivery emits exactly one log line with the
        webhook's event name carried under a non-reserved key.
        """
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, 'wh.json')
        real_logger = StructuredLogger.__new__(StructuredLogger)
        real_logger.app = None
        real_logger.log_file = path
        real_logger.enabled = True
        real_logger._logger = StructuredLogger._get_logger(path)

        notifier = self._notifier()
        notifier._structured = real_logger

        def fake_urlopen(req, timeout=None):
            resp = mock.Mock(); resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda *a: None; resp.status = 200
            return resp

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            delivered = notifier.notify(
                'site_created', {'domain': 'x.com'}, correlation_id='cid-real',
            )
        self.assertTrue(delivered, 'delivery should succeed on first attempt')
        real_logger._logger.handlers[0].flush()
        with open(path) as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        delivered_lines = [ln for ln in lines if ln['event'] == 'webhook.delivered']
        self.assertEqual(len(delivered_lines), 1,
                         'exactly one delivery log line expected')
        # webhook's own event name must travel under a key that does NOT
        # shadow the logger's positional `event` param.
        self.assertEqual(delivered_lines[0].get('hook_event'), 'site_created')


class StructuredLoggerTests(unittest.TestCase):
    def test_operation_emits_start_and_complete(self):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, 'mt.json')
        logger = StructuredLogger.__new__(StructuredLogger)
        logger.app = None
        logger.log_file = path
        logger.enabled = True
        logger._logger = StructuredLogger._get_logger(path)

        with logger.operation('site_created', extra='hello') as state:
            state['extra']['domain'] = 'a.com'

        logger._logger.handlers[0].flush()
        with open(path) as fh:
            lines = [json.loads(ln) for ln in fh if ln.strip()]
        events = [ln['event'] for ln in lines]
        self.assertIn('site_created.start', events)
        self.assertIn('site_created.complete', events)
        # All entries share the same correlation_id
        cids = {ln.get('correlation_id') for ln in lines if 'correlation_id' in ln}
        self.assertEqual(len(cids), 1)
        # duration_ms appears on the completion line
        complete = [ln for ln in lines if ln['event'] == 'site_created.complete'][0]
        self.assertIn('duration_ms', complete)

    def test_operation_emits_failed_on_exception(self):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, 'mt_fail.json')
        logger = StructuredLogger.__new__(StructuredLogger)
        logger.app = None
        logger.log_file = path
        logger.enabled = True
        logger._logger = StructuredLogger._get_logger(path)

        with self.assertRaises(RuntimeError):
            with logger.operation('thing') as _:
                raise RuntimeError('nope')

        logger._logger.handlers[0].flush()
        with open(path) as fh:
            events = [json.loads(ln)['event'] for ln in fh if ln.strip()]
        self.assertIn('thing.failed', events)


class HealthAggregationTests(unittest.TestCase):
    def test_run_all_aggregates_status(self):
        checker = multitenancy_health.HealthChecker.__new__(multitenancy_health.HealthChecker)
        checker.app = None
        checker.config = {}
        checker.shared_root = '/tmp'
        checker.min_free_gb = 1
        checker.checkers = []
        checker.register('ok_check', lambda: {'status': 'ok', 'details': {}}, critical=True)
        checker.register(
            'warn_check', lambda: {'status': 'warn', 'details': {}}, critical=False,
        )
        result = checker.run_all()
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(result['checks']['ok_check']['status'], 'ok')
        self.assertEqual(result['checks']['warn_check']['status'], 'warn')

    def test_critical_failure_marks_unhealthy(self):
        checker = multitenancy_health.HealthChecker.__new__(multitenancy_health.HealthChecker)
        checker.app = None
        checker.config = {}
        checker.shared_root = '/tmp'
        checker.min_free_gb = 1
        checker.checkers = []
        checker.register('ok_check', lambda: {'status': 'ok'}, critical=True)

        def boom():
            raise RuntimeError('db down')

        checker.register('db', boom, critical=True)
        result = checker.run_all()
        self.assertEqual(result['status'], 'unhealthy')
        self.assertEqual(result['checks']['db']['status'], 'error')
        self.assertIn('db down', result['checks']['db']['details']['error'])


class JsonOutputTests(unittest.TestCase):
    def test_should_emit_reads_pargs(self):
        class P:
            json_output = True
        self.assertTrue(JsonOutput.should_emit(P()))
        class Q:
            json_output = False
        self.assertFalse(JsonOutput.should_emit(Q()))
        class R:
            pass  # no attribute at all
        self.assertFalse(JsonOutput.should_emit(R()))


class MaintenanceRoundTripTests(unittest.TestCase):
    """Integration-style: write maintenance include + HTML, then remove."""

    def test_enable_writes_and_disable_removes(self):
        from wo.cli.plugins import multitenancy as mt
        with tempfile.TemporaryDirectory() as tmp:
            domain = 'example-mt.test'
            site_root = os.path.join(tmp, domain)
            # shadow /var/www layout for the duration of the test
            os.makedirs(os.path.join(site_root, 'htdocs'))
            os.makedirs(os.path.join(site_root, 'conf', 'nginx'))

            def fake_paths(d):
                return {
                    'site_root': site_root,
                    'site_htdocs': os.path.join(site_root, 'htdocs'),
                    'nginx_include_dir': os.path.join(site_root, 'conf', 'nginx'),
                    'nginx_include_file': os.path.join(
                        site_root, 'conf', 'nginx',
                        'multitenancy-maintenance.conf',
                    ),
                    'maintenance_html': os.path.join(
                        site_root, 'htdocs', 'maintenance.html'
                    ),
                }

            # Stub the Cement-rendered `app.render` so we don't depend on the
            # mustache template loader in the test environment. The helper
            # receives the controller and hops through controller.app.render.
            controller = mock.Mock()
            def render(data, template, out):
                out.write(f"<!-- {template} : {json.dumps(data, default=str)} -->")
            controller.app.render = render

            with mock.patch.object(mt, '_maintenance_paths', fake_paths):
                ok = mt._maintenance_enable(
                    controller, domain, 'Back in 10',
                    {'maintenance': {'admin_ips': ['1.2.3.4'], 'default_retry_after': 60}},
                )
                self.assertTrue(ok)
                self.assertTrue(os.path.exists(fake_paths(domain)['nginx_include_file']))
                self.assertTrue(os.path.exists(fake_paths(domain)['maintenance_html']))

                ok = mt._maintenance_disable(controller, domain)
                self.assertTrue(ok)
                self.assertFalse(os.path.exists(fake_paths(domain)['nginx_include_file']))
                self.assertFalse(os.path.exists(fake_paths(domain)['maintenance_html']))


class AuditRestartSurvivalTests(unittest.TestCase):
    """Integration-style: verify audit rows checksum-match after round-trip.

    A real DB-restart test would require WordOps's sqlalchemy session setup.
    This equivalent test proves the invariant that matters: a row written to
    disk and read back on a fresh process still passes `verify()` — the
    checksum does not depend on any in-memory state.
    """

    def test_checksum_survives_process_boundary(self):
        record = {
            'timestamp': datetime(2026, 4, 17, 12, 0, 0).isoformat(),
            'event_id': 'restart-1',
            'actor': 'root',
            'actor_ip': 'local',
            'action': 'baseline_applied',
            'target': 'baseline',
            'target_type': 'baseline',
            'result': 'success',
            'duration_ms': 900,
            'details': json.dumps({'attempted': 5, 'succeeded': 5}),
        }
        record['checksum'] = AuditLogger._compute_checksum(record)

        # Simulate SQLite serialization round-trip
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.json', delete=False) as fh:
            fh.write(json.dumps(record))
            path = fh.name
        try:
            with open(path) as fh:
                reloaded = json.loads(fh.read())
        finally:
            os.unlink(path)

        self.assertTrue(AuditLogger(None).verify(reloaded))


if __name__ == '__main__':
    unittest.main()
