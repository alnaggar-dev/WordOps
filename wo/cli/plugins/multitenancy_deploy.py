"""Deploy shared site content without changing the WordPress core."""

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from cement.core.controller import CementBaseController, expose

from wo.cli.plugins.multitenancy_backup_functions import fleet_operation
from wo.cli.plugins.multitenancy_content import (
    HISTORY_FILE, KEEP_RELEASES, RELEASE_NAME_RE, content_root, current_link,
    current_release, is_cut_over, list_releases, read_release_info,
)
from wo.cli.plugins.multitenancy_db import MTDatabase
from wo.cli.plugins.multitenancy_functions import MTFunctions
from wo.cli.plugins.multitenancy_health import HealthChecker
from wo.core.logging import Log
from wo.core.services import WOService


class DeployError(Exception):
    """Stop deployment and preserve the recoverable state."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_release(shared_root, name):
    if not name or not RELEASE_NAME_RE.fullmatch(name):
        raise DeployError('Invalid content release name.')
    path = os.path.join(content_root(shared_root), name)
    expected = os.path.join(os.path.realpath(content_root(shared_root)), name)
    if os.path.islink(path) or os.path.realpath(path) != expected:
        raise DeployError('The release must be a real folder under content/.')
    for item in ('wp-content/plugins', 'wp-content/themes'):
        if not os.path.isdir(os.path.join(path, item)):
            raise DeployError('Release is missing ' + item)
    for item in ('baseline.json', 'RELEASE'):
        if not os.path.isfile(os.path.join(path, item)):
            raise DeployError('Release is missing ' + item)
    info = read_release_info(path)
    if not info or not all(isinstance(info.get(key), dict) for key in ('plugins', 'themes')):
        raise DeployError('Invalid RELEASE metadata.')
    with open(os.path.join(path, 'baseline.json')) as handle:
        if not isinstance(json.load(handle), dict):
            raise DeployError('Invalid baseline.json.')
    return path


def switch_current(shared_root, name):
    """Replace only the relative content/current link."""
    link = current_link(shared_root)
    temporary = link + '.new'
    try:
        os.symlink(name, temporary)
        os.replace(temporary, link)
    finally:
        if os.path.islink(temporary):
            os.unlink(temporary)


def prune_releases(shared_root, previous):
    names = list_releases(shared_root)
    live = current_release(shared_root)
    keep = set(names[-KEEP_RELEASES:])
    keep.update(filter(None, [os.path.basename(live) if live else None, previous]))
    for name in names:
        if name not in keep:
            shutil.rmtree(os.path.join(content_root(shared_root), name))


def asset_versions(release):
    """Read WordPress headers without loading plugin code."""
    versions = {'plugins': {}, 'themes': {}}
    for kind in versions:
        folder = os.path.join(release, 'wp-content', kind)
        for entry in sorted(os.scandir(folder), key=lambda item: item.name):
            if entry.name.startswith('.'):
                continue
            if kind == 'themes':
                candidates = [os.path.join(entry.path, 'style.css')] if entry.is_dir() else []
                header = 'Theme Name'
            else:
                candidates = glob.glob(os.path.join(entry.path, '*.php')) if entry.is_dir() else [entry.path]
                header = 'Plugin Name'
            for candidate in sorted(candidates):
                if not os.path.isfile(candidate):
                    continue
                with open(candidate, errors='replace') as handle:
                    text = handle.read(8192)
                if not re.search(r'^[ \t/*#@]*' + header + r'\s*:', text, re.M | re.I):
                    continue
                match = re.search(r'^[ \t/*#@]*Version\s*:\s*(.+)', text, re.M | re.I)
                versions[kind][entry.name.removesuffix('.php')] = match.group(1).strip() if match else ''
                break
    return versions


class ContentDeployment:
    """One fleet operation. The controller owns the shared operation lock."""

    message = 'Site code maintenance is in progress. Please try again shortly.'

    def __init__(self, controller):
        self.controller = controller
        self.config = MTFunctions.load_config(controller)
        self.root = self.config.get('shared_root', '/var/www/shared')
        self.sites = MTDatabase.get_shared_sites(controller)
        self.gates = []
        self.preexisting_gates = set()
        self.cron_locks = {}
        self.workers = []
        self.restore = None
        self.log_offsets = {}

    @contextmanager
    def step(self, label):
        start = time.monotonic()
        try:
            yield
        finally:
            Log.info(self.controller, '{}: {:.2f}s'.format(label, time.monotonic() - start))

    def _history(self, name, previous, path, result):
        os.makedirs(content_root(self.root), exist_ok=True)
        record = dict(time=utc_now(), release=name, previous=previous,
                      path=path, restore_point=self.restore, result=result)
        with open(os.path.join(content_root(self.root), HISTORY_FILE), 'a') as handle:
            handle.write(json.dumps(record, sort_keys=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    def _maintenance(self):
        from wo.cli.plugins.multitenancy import _maintenance_enable, _maintenance_gate_exists
        self.workers, error = MTFunctions.capture_nginx_worker_pids()
        if error:
            raise DeployError('Cannot capture nginx workers: ' + str(error))
        for site in self.sites:
            domain = site['domain']
            if _maintenance_gate_exists(domain) and domain not in self.gates:
                self.preexisting_gates.add(domain)
            if domain not in self.gates:
                self.gates.append(domain)
            if not _maintenance_enable(self.controller, domain, self.message, self.config, loopback_bypass=False):
                raise DeployError('Cannot enable maintenance for ' + domain)
        self._reload_nginx()

    def _reload_nginx(self):
        if self.sites and not MTFunctions.safe_nginx_reload(self.controller, self.sites[-1]['domain']):
            raise DeployError('Cannot reload nginx.')

    def _bypass(self, enabled):
        from wo.cli.plugins.multitenancy import _maintenance_enable
        for site in self.sites:
            if not _maintenance_enable(self.controller, site['domain'], self.message,
                                       self.config, loopback_bypass=enabled):
                raise DeployError('Cannot change the loopback maintenance bypass.')
        self._reload_nginx()

    def _release_maintenance(self):
        from wo.cli.plugins.multitenancy import _release_update_maintenance
        errors = _release_update_maintenance(self.controller, self.gates)
        if errors:
            raise DeployError('; '.join(errors))
        self.gates = []

    def _drain(self):
        if not self.cron_locks:
            self.cron_locks, errors = MTFunctions.acquire_tenant_cron_locks(
                self.controller, self.sites, timeout=30)
            if errors:
                raise DeployError('Cannot drain cron: ' + str(errors))
        if not MTFunctions.sync_wp_cron_entries(self.controller):
            raise DeployError('Cannot install maintenance-aware cron entries.')
        drained, errors = MTFunctions.drain_tenant_active_work(
            self.controller, self.sites, self.cron_locks, self.workers,
            timeout=330, sleeper_horizon=60, poll_interval=2)
        if not drained:
            raise DeployError('Active work did not drain: ' + ', '.join(errors))

    def _restore_point(self):
        from wo.cli.plugins.multitenancy_backup_functions import backup_is_configured
        from wo.cli.plugins.multitenancy_backup_run import (
            _config_enabled, _load_config_or_error, _run_selected,
        )
        ok, errors, directory = MTFunctions.backup_tenant_databases(self.controller, self.sites, self.root)
        if not ok:
            raise DeployError('Database dumps failed: ' + str(errors))
        commands = []
        for site in self.sites:
            commands.append('wp db import {} --path={} --allow-root'.format(
                shlex.quote(os.path.join(directory, site['domain'] + '.sql')),
                shlex.quote(MTFunctions._site_htdocs(site))))
        self.restore = {'dumps': directory, 'command': ' && '.join(commands)}
        if not backup_is_configured():
            Log.warn(self.controller, 'Backups are not initialized. Skipping backup run --db; local dumps are ready.')
            return
        # Use backup run's implementation under this process's fleet lock.
        # A child wo process cannot reenter the lock held by this operation.
        config = _load_config_or_error(self.controller)
        if not _config_enabled(config):
            raise DeployError('Backups are disabled in the backup configuration.')
        results = _run_selected(self.controller, config, ['db'], cron=False)
        if not results.get('db', {}).get('ok'):
            raise DeployError('Backup run --db failed.')

    def _prepare(self, release):
        # rsync can hard-link this generated file to the live release. Unlink it
        # first so rendering never truncates a file used by the live stores.
        logger = os.path.join(release, 'wp-content', 'mu-plugins', 'wo-login-log.php')
        if os.path.lexists(logger):
            os.unlink(logger)
        if not MTFunctions.install_login_failure_logger(self.controller, release):
            raise DeployError('Cannot write wo-login-log.php into the release.')
        subprocess.run(['chown', '-R', 'www-data:www-data', release], check=True)

    def _reset_caches(self):
        if not MTFunctions.clear_all_caches(self.controller):
            raise DeployError('Page cache reset failed.')
        if not MTFunctions.reset_opcache(self.controller):
            raise DeployError('Opcache reset failed.')
        # Opcache reset does not clear each worker's realpath cache. Replace
        # FPM workers with a graceful reload before checking the new content.
        versions = {str(site.get('php_version') or self.config.get('php_version', '8.4')) for site in self.sites}
        for version in sorted(versions):
            service = 'php{}-fpm'.format(version)
            if not WOService.reload_service(self.controller, service):
                raise DeployError('Cannot reload ' + service)

    def _wp(self, site, arguments):
        result = subprocess.run(['wp'] + arguments + ['--allow-root', '--path=' + MTFunctions._site_htdocs(site)],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise DeployError('WP-CLI failed for {}: {}'.format(site['domain'], ' '.join(arguments[:3])))
        return result.stdout.strip()

    def _database_updates(self, old, new):
        before = read_release_info(old).get('plugins', {}).get('woocommerce')
        after = read_release_info(new).get('plugins', {}).get('woocommerce')
        if after:
            for site in self.sites:
                pending = before != after or self._wp(
                    site, ['option', 'get', 'woocommerce_db_version']) != after
                if pending:
                    self._wp(site, ['wc', 'update', '--yes'])

    def _apply(self, old, new):
        from wo.cli.plugins.multitenancy import WOMultitenancyController
        before = read_release_info(old).get('plugins', {}).get('object-cache-pro')
        after = read_release_info(new).get('plugins', {}).get('object-cache-pro')
        needs_apply = before != after or self._baseline(old) != self._baseline(new)
        history = os.path.join(content_root(self.root), HISTORY_FILE)
        if not needs_apply and os.path.isfile(history):
            with open(history) as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            for record in reversed(records):
                if record['release'] == os.path.basename(old) and record['result'] != 'aborted':
                    needs_apply = record['path'] == 'safe' and record['result'] == 'fail'
                    break
        if needs_apply:
            WOMultitenancyController._apply_impl(self.controller)

    @staticmethod
    def _baseline(release):
        with open(os.path.join(release, 'baseline.json'), 'rb') as handle:
            return handle.read()

    def _log_paths(self):
        paths = set(glob.glob('/var/log/php*-fpm.log'))
        paths.update(glob.glob('/var/log/php*/**/*.log', recursive=True))
        for site in self.sites:
            paths.update(glob.glob('/var/log/nginx/{}*error.log'.format(site['domain'])))
        return paths

    def _log_mark(self):
        self.log_offsets = {
            path: (os.stat(path).st_ino, os.path.getsize(path))
            for path in self._log_paths()
        }

    def _fatal_errors(self):
        for path in self._log_paths():
            inode, offset = self.log_offsets.get(path, (None, 0))
            stat = os.stat(path)
            with open(path, errors='replace') as handle:
                handle.seek(offset if stat.st_ino == inode and stat.st_size >= offset else 0)
                if 'PHP Fatal error' in handle.read():
                    raise DeployError('New PHP Fatal error in ' + path)

    def _page(self, site, url, loopback):
        domain = site['domain']
        apex = domain[4:] if domain.startswith('www.') else domain
        hosts = {apex, 'www.' + apex}
        command = ['curl', '--silent', '--show-error', '--insecure', '--proto', '=http,https',
                   '--max-time', '20', '--header', 'X-Requested-With: XMLHttpRequest']
        if loopback:
            for host in sorted(hosts):
                for port in (80, 443):
                    command += ['--resolve', '{}:{}:127.0.0.2'.format(host, port)]
        with tempfile.NamedTemporaryFile() as body:
            for _ in range(6):
                parsed = urlparse(url)
                if parsed.hostname not in hosts or parsed.scheme not in ('http', 'https'):
                    raise DeployError('Unexpected page redirect for ' + domain)
                result = subprocess.run(command + ['--output', body.name, '--write-out',
                                                   '%{http_code}\t%{redirect_url}', url],
                                        capture_output=True, text=True, timeout=25, check=False)
                if result.returncode:
                    raise DeployError('Page request failed for ' + domain)
                status, _, redirect = result.stdout.strip().partition('\t')
                with open(body.name, errors='replace') as handle:
                    text = handle.read().lower()
                if 'critical error' in text or not status.isdigit() or not 200 <= int(status) < 400:
                    raise DeployError('Page check failed for {}: HTTP {}'.format(url, status))
                if int(status) < 300:
                    return
                if not redirect:
                    raise DeployError('Empty redirect for ' + url)
                url = urljoin(url, redirect)
        raise DeployError('Too many page redirects for ' + domain)

    def _check_site(self, site, loopback, version):
        scheme = 'https' if site.get('is_ssl') else 'http'
        base = '{}://{}'.format(scheme, site['domain'])
        products = self._wp(site, ['post', 'list', '--post_type=product', '--post_status=publish',
                                   '--posts_per_page=1', '--field=ID']).split()
        if not products:
            raise DeployError('No published product to check for ' + site['domain'])
        product = self._wp(site, ['post', 'url', products[0]])
        for path in ('/', product, '/cart/', '/checkout/', '/my-account/', '/wp-login.php', '/wp-json/'):
            self._page(site, urljoin(base, path), loopback)
        if version and self._wp(site, ['option', 'get', 'woocommerce_db_version']) != version:
            raise DeployError('WooCommerce database version differs for ' + site['domain'])

    def _checks(self, loopback=False):
        release = current_release(self.root)
        version = read_release_info(release).get('plugins', {}).get('woocommerce')
        for site in self.sites:
            self._check_site(site, loopback, version)
        self._fatal_errors()
        result = HealthChecker(self.controller).register_defaults().run_all()
        if result.get('status') == 'unhealthy':
            raise DeployError('Fleet health is unhealthy.')

    def _recovery_message(self, switched_back=False):
        if switched_back:
            Log.warn(self.controller, 'Previous files are live. Maintenance remains on for database recovery.')
            if self.restore:
                Log.warn(self.controller, self.restore['command'])
            Log.warn(self.controller, 'After recovery and checks, run '
                     'wo multitenancy maintenance --all --disable.')
            return
        Log.warn(self.controller, 'Stores remain in maintenance. Choose one recovery path:')
        Log.warn(self.controller, '1. Fix forward with a new safe content deploy.')
        Log.warn(self.controller, '2. Run wo multitenancy deploy --back --force, then restore the database dumps:')
        if self.restore:
            Log.warn(self.controller, self.restore['command'])

    def _unlock(self):
        MTFunctions.release_tenant_cron_locks(self.cron_locks)
        self.cron_locks = {}

    def deploy(self, name, code_only=False):
        previous = current_release(self.root)
        old_name = os.path.basename(previous) if previous else None
        path = 'code-only' if code_only else 'safe'
        switched = False
        maintenance_started = False
        result = 'aborted'
        try:
            if not is_cut_over(self.root):
                raise DeployError('Run wo multitenancy cutover before deploying content.')
            release = validate_release(self.root, name)
            if not previous:
                if self.sites:
                    raise DeployError('Tracked sites have no live content release. '
                                      'Restore content/current before deploying.')
                if os.path.lexists(current_link(self.root)):
                    raise DeployError('content/current is broken. Restore its release before deploying.')
                if code_only:
                    raise DeployError('The first release requires the safe path, not --code-only.')
            if name == old_name:
                raise DeployError('The release is already live.')
            if code_only:
                old, new = read_release_info(previous), read_release_info(release)
                versions_changed = any(old.get(k) != new.get(k) for k in ('plugins', 'themes'))
                if versions_changed or self._baseline(previous) != self._baseline(release):
                    raise DeployError('Version or baseline changes require the safe path.')
            if not self.sites:
                with self.step('Prepare empty-fleet release'):
                    self._prepare(release)
                with self.step('Switch empty-fleet content'):
                    switch_current(self.root, name)
                    switched = True
                prune_releases(self.root, old_name)
                result = 'pass'
                return
            if not code_only:
                maintenance_started = True
                with self.step('1. Maintenance'):
                    self._maintenance()
                with self.step('2. Drain'):
                    self._drain()
            with self.step('3. Restore point'):
                self._restore_point()
                if self.restore:
                    self.restore['safe'] = not code_only
            with self.step('4. Login logger and ownership'):
                self._prepare(release)
            with self.step('5. Switch and caches'):
                self._log_mark()
                switch_current(self.root, name)
                switched = True
                self._reset_caches()
            if not code_only:
                with self.step('6. WooCommerce updates'):
                    self._database_updates(previous, release)
                with self.step('7. Apply'):
                    self._apply(previous, release)
            with self.step('8. Checks'):
                if not code_only:
                    self._bypass(True)
                self._checks(loopback=not code_only)
            with self.step('9. Resume'):
                if not code_only:
                    self._bypass(False)
                    self._release_maintenance()
                prune_releases(self.root, old_name)
            result = 'pass'
        except (Exception, SystemExit):
            if switched:
                result = 'fail'
                if not self.sites:
                    if previous:
                        switch_current(self.root, old_name)
                    else:
                        os.unlink(current_link(self.root))
                elif code_only:
                    with self.step('Automatic switch back'):
                        self._log_mark()
                        switch_current(self.root, old_name)
                        self._reset_caches()
                        self._checks()
                else:
                    self._bypass(False)
                    self._recovery_message()
            elif maintenance_started:
                self.gates = [domain for domain in self.gates if domain not in self.preexisting_gates]
                self._release_maintenance()
            raise
        finally:
            self._unlock()
            self._history(name, old_name, path, result)

    def _previous(self):
        live = os.path.basename(current_release(self.root) or '')
        history = os.path.join(content_root(self.root), HISTORY_FILE)
        if os.path.exists(history):
            with open(history) as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            for record in reversed(records):
                if record['release'] == live and record.get('previous') and record['result'] in ('pass', 'fail'):
                    return record
        raise DeployError('No previous content release is recorded.')

    def back(self, force=False):
        record = self._previous()
        self.restore = record.get('restore_point')
        safe = record['path'] == 'safe' or bool((self.restore or {}).get('safe'))
        if safe:
            if self.restore:
                Log.warn(self.controller, 'Database restore command: ' + self.restore['command'])
            if not force:
                self._history(record['previous'], record['release'], 'back', 'aborted')
                raise DeployError('The previous deploy used the safe path. '
                                  'Use --back --force after choosing database recovery.')
        name, previous = record['previous'], record['release']
        result = 'aborted'
        switched = False
        try:
            validate_release(self.root, name)
            if safe:
                with self.step('Maintenance and drain'):
                    self._maintenance()
                    self._drain()
            with self.step('Switch back and caches'):
                self._log_mark()
                switch_current(self.root, name)
                switched = True
                self._reset_caches()
            with self.step('Checks'):
                if safe:
                    self._bypass(True)
                self._checks(loopback=safe)
            if safe:
                self._bypass(False)
                Log.warn(self.controller, 'Files switched back. Maintenance remains on for database recovery.')
                if self.restore:
                    Log.warn(self.controller, self.restore['command'])
                Log.warn(self.controller, 'After recovery and checks, run '
                         'wo multitenancy maintenance --all --disable.')
            prune_releases(self.root, previous)
            result = 'pass'
        except (Exception, SystemExit):
            result = 'fail' if switched else 'aborted'
            if safe and switched:
                self._bypass(False)
                self._recovery_message(switched_back=True)
            elif safe:
                self.gates = [domain for domain in self.gates if domain not in self.preexisting_gates]
                self._release_maintenance()
            raise
        finally:
            self._unlock()
            self._history(name, previous, 'back', result)

    def cutover(self):
        if not self.sites:
            raise DeployError('Cutover requires tracked shared sites.')
        if is_cut_over(self.root):
            raise DeployError('Content is already cut over.')
        name = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-pre-cutover')
        release = os.path.join(content_root(self.root), name)
        content = os.path.join(self.root, 'wp-content')
        baseline = os.path.join(self.root, 'config', 'baseline.json')
        undo = []
        result = 'aborted'
        try:
            if not os.path.isdir(content) or not os.path.isfile(baseline):
                raise DeployError('The old content folder or baseline.json is missing.')
            with self.step('Maintenance'):
                self._maintenance()
            with self.step('Drain'):
                self._drain()
            with self.step('Move content and create links'):
                os.makedirs(release)
                undo.append(lambda: shutil.rmtree(release))
                for source, target in ((content, os.path.join(release, 'wp-content')),
                                       (baseline, os.path.join(release, 'baseline.json'))):
                    os.rename(source, target)
                    undo.append(lambda source=source, target=target: os.rename(target, source))
                info = dict(commit='pre-cutover', short='pre-cutover', branch='', date=utc_now(), built=utc_now())
                info.update(asset_versions(release))
                with open(os.path.join(release, 'RELEASE'), 'w') as handle:
                    json.dump(info, handle, sort_keys=True)
                subprocess.run(['chown', '-R', 'www-data:www-data', release], check=True)
                switch_current(self.root, name)
                undo.append(lambda: os.unlink(current_link(self.root)))
                for source, target in ((content, 'content/current/wp-content'),
                                       (baseline, '../content/current/baseline.json')):
                    os.symlink(target, source)
                    undo.append(lambda source=source: os.unlink(source))
            with self.step('Caches and resume'):
                self._log_mark()
                self._reset_caches()
                self._release_maintenance()
            with self.step('Checks'):
                self._checks()
            result = 'pass'
        except (Exception, SystemExit):
            result = 'fail'
            with self.step('Undo cutover'):
                if undo:
                    self._maintenance()
                    self._drain()
                for action in reversed(undo):
                    action()
                if undo:
                    self._reset_caches()
                self._release_maintenance()
            raise
        finally:
            self._unlock()
            self._history(name, None, 'cutover', result)


def _run(controller, action):
    try:
        action(ContentDeployment(controller))
    except (Exception, SystemExit) as error:
        Log.error(controller, 'Content operation failed: {}'.format(error), exit=False)
        controller.app.close(1)


class WOMultitenancyDeployController(CementBaseController):
    class Meta:
        label = 'deploy'
        stacked_on = 'multitenancy'
        stacked_type = 'nested'
        description = 'Deploy a content release (safe path by default)'
        arguments = [
            (['release'], dict(nargs='?', help='content release name')),
            (['--code-only'], dict(action='store_true', help='deploy reviewed code without maintenance')),
            (['--back'], dict(action='store_true', help='switch to the previous content release')),
            (['--force'], dict(action='store_true', help='allow back after a safe deploy')),
        ]

    @expose(hide=True)
    @fleet_operation('content deploy')
    def default(self):
        args = self.app.pargs
        if (args.back and (args.release or args.code_only)) or (args.force and not args.back):
            Log.error(self, 'Use deploy RELEASE [--code-only] or deploy --back [--force].')
            return
        if args.back:
            return _run(self, lambda operation: operation.back(force=args.force))
        if not args.release:
            Log.error(self, 'A content release name is required.')
            return
        return _run(self, lambda operation: operation.deploy(args.release, code_only=args.code_only))


class WOMultitenancyCutoverController(CementBaseController):
    class Meta:
        label = 'cutover'
        stacked_on = 'multitenancy'
        stacked_type = 'nested'
        description = 'Move existing shared content into its first release'

    @expose(hide=True)
    @fleet_operation('content cutover')
    def default(self):
        return _run(self, lambda operation: operation.cutover())
