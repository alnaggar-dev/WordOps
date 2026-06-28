# Repository Guidelines

## Project Overview

WordOps is a **root-only Python CLI** (`wo`) that automates a WordPress server stack — Nginx, PHP-FPM, MariaDB, Redis, Let's Encrypt — on Ubuntu/Debian. This is the **`alnaggar-dev` fork**: Git-only install/update (no PyPI, no mainline/beta channels) plus a custom **multitenancy plugin** that lets many WordPress sites share one WP core via symlinks (`/var/www/shared/current` → `releases/wp-<ts>`) for ~90% disk savings and atomic symlink-swap rollback.

Operating model (see `CLAUDE.md`): **trust-model-of-one** — solo operator, own code, own fleet. Prefer **deleting code over adding abstractions**; reducing surface area is a feature. Per-tenant isolation, audit trails, tamper detection, and multi-operator workflows are explicit **non-goals** — do not propose them.

## Architecture & Data Flow

Built on the **Cement 2.10.14** CLI framework (pinned; dependabot ignores cement ≥3).

- **Entry point**: `wo = wo.cli.main:main` (`setup.py`). `main()` in `wo/cli/main.py` requires `geteuid() == 0`, then `with app: app.run()`. App classes `WOApp` / `WOTestApp` (CementApp) configure `mustache` output, `colorlog` logging, `argcomplete`, and a custom `WOArgHandler`.
- **Bootstrap**: `wo/cli/bootstrap.py` registers `WOBaseController` (`wo/cli/controllers/base.py`, label `base`, `--version`).
- **Commands = stacked controllers**: each feature is `wo/cli/plugins/<name>.py` defining `WO<Feature>Controller(CementBaseController)` with `class Meta: label=…` and `stacked_on='base'` (or nested: `stacked_on='site', stacked_type='nested'` → `wo site create`). Handler methods use `@expose(...)`; argparse-driven subcommands use `@expose(hide=True)` on `default()`.
- **Plugin registration**: every plugin module exposes `def load(app): app.handler.register(...)`. A plugin runs only when enabled in `config/plugins.d/<name>.conf` (`enable_plugin = true`). Some register Cement hooks (`post_setup`, `post_argument_parsing`, `wo_site_hook` → `init_db`).
- **Business logic** lives in `wo/core/*` service classes and `wo/cli/plugins/*_functions.py` (and `*_db.py`). There is **no formal DI**: the controller instance `self` is passed as the implicit context into plain functions and service methods (`Log.debug(self, …)`, `WOService.reload_service(self, 'nginx')`).
- **Templates**: Mustache files in `wo/cli/templates/*.mustache`, rendered via `self.app.render(data, 'virtualconf.mustache', out=path)`.
- **State**: SQLite at `/var/lib/wo/dbase.db` via SQLAlchemy (`wo/core/database.py` `init_db`, models in `wo/cli/plugins/models.py`); multitenancy adds its own tables via `MTDatabase`.

**Example flow — `wo site create example.com --wp`:**
1. Cement routes to `WOSiteCreateController.default` (`wo/cli/plugins/site_create.py`).
2. `detSitePar(vars(pargs))` resolves site type/cache; builds `data` dict (webroot = `WOVar.wo_webroot + domain`).
3. Validate: `WODomain.validate`, `check_domain_exists` (`getSiteInfo`), `pre_run_checks(self)` (`nginx -t`).
4. `setupdomain(self, data)` renders the vhost to `/etc/nginx/sites-available/`, symlinks, re-tests nginx (`wo/cli/plugins/site_functions.py`).
5. `addNewSite` / `updateSiteInfo` (`wo/cli/plugins/sitedb.py`) record the site in SQLite.
6. `setupdatabase` (`WOMysql`), `setupwordpress` (WP-CLI via `WOShellExec.cmd_exec('wp --allow-root …')`).
7. `WOService.reload_service(self, 'nginx')`; optional `WOAcme.setupletsencrypt`. On `SiteError`, `doCleanupAction` rolls back vhost/db/webroot.

## Key Directories

| Path | Purpose |
|------|---------|
| `wo/cli/` | `main.py`, `bootstrap.py`, `controllers/base.py`, `plugins/` (commands), `templates/` (`.mustache`) |
| `wo/cli/plugins/` | One module per command area: `site*`, `stack*`, `debug`, `update`, `secure`, `log`, `clean`, `maintenance`, `sync`, `info`, `multitenancy*` |
| `wo/core/` | ~27 flat service modules (`variables`, `logging`, `exc`, `shellexec`, `services`, `aptget`, `mysql`, `acme`, `fileutils`, `template`, `nginx`, `wpcli`, …) |
| `wo/utils/` | `test.py` — Cement test harness (`WOTestCase`) |
| `config/` | `wo.conf` (Cement app config), `plugins.d/*.conf` (per-plugin toggles), `logrotate.d/`, `bash_completion.d/` |
| `tests/cli/` | nose + unittest test modules (`tests/core/` is empty) |
| `openspec/` | Spec-driven change workflow for multitenancy (`specs/`, `changes/`, `AGENTS.md`, `project.md`) |
| `docs/` | `wo.8` man page |

## Development Commands

```bash
# Production install (root, default branch main); installs to /opt/wo venv from the git fork
sudo bash install                 # interactive
sudo bash install --force         # silent
sudo bash install -b <branch>     # specific branch

# Update the installed CLI (fork: git-based, no PyPI)
wo update                         # latest main
wo update --force
wo update --branch <name>

# Run commands (must be root)
wo site create example.com --wp
wo stack install --nginx --php
wo multitenancy init|create|update|rollback|baseline

# Local dev / editable install with test deps
pip install -e ".[testing]"

# Build sdist + wheel (the CI release workflow, .github/workflows/pypi.yml)
python3 -m pip install --upgrade setuptools wheel
python3 setup.py sdist bdist_wheel    # artifacts in dist/

# Lint (config in setup.cfg)
flake8 wo                        # max-line-length=120, max-complexity=10

# Debug a command + tail logs
wo --debug multitenancy update
tail -f /var/log/wo/wordops.log
```

## Code Conventions & Common Patterns

- **Formatting** (`.editorconfig`): 4-space indent, `LF`, UTF-8, trim trailing whitespace, **no final newline**. `flake8` caps line length at **120** and complexity at **10**; ignored codes live in `setup.cfg [flake8]`.
- **Naming**: snake_case functions (`setupdomain`, `check_domain_exists`); service classes `WO*` (`WOMysql`, `WOService`, `WOAcme`); controllers `WO<Feature>Controller`; plugin hooks `wo_<plugin>_hook`. Test modules `{order}_test_{area}.py`.
- **Config access**: runtime settings from `self.app.config` (loaded from `/etc/wo/wo.conf`, sections `[wo]`, `[php]`, `[mysql]`, `[wordpress]`, `[letsencrypt]`, `[multitenancy]`, …). Static install-time constants from `WOVar` (`wo/core/variables.py`) — **not** `app.config`.
- **Logging**: `Log.info/warn/debug/error` (`wo/core/logging.py`); first arg is the controller `self`. `Log.error(msg)` defaults to `exit=True` → `app.close(1)`, so it doubles as a fatal-exit path.
- **Errors**: domain exceptions in `wo/core/exc.py` (`WOError`, `WOConfigError`, `WORuntimeError`, `WOArgumentError`); plugin-level `SiteError`, `CommandExecutionError`. `main()` catches `WOError`, `CaughtSignal`, `FrameworkError`.
- **Shell execution**: prefer `WOShellExec.cmd_exec` / `cmd_exec_stdout` (`wo/core/shellexec.py`); raw `subprocess` and `sh.git` / `sh.apt_get` also appear. `WOService` wraps `service …` and guards nginx with `nginx -t`.
- **Multitenancy work is spec-governed**: for new capabilities or breaking changes, follow the OpenSpec workflow (`openspec/AGENTS.md`) — proposal → implement → archive (`openspec list|validate|archive`). Extend WordOps natives (`setupdatabase`, `WOAcme`, `WOService`, `Log`) rather than reinventing them.

## Important Files

- **Entry/bootstrap**: `wo/cli/main.py`, `wo/cli/bootstrap.py`, `wo/cli/controllers/base.py`
- **Core services**: `wo/core/variables.py` (`WOVar`), `wo/core/logging.py` (`Log`), `wo/core/exc.py`, `wo/core/shellexec.py`, `wo/core/services.py`, `wo/core/database.py`, `wo/core/mysql.py`, `wo/core/acme.py`
- **Site command**: `wo/cli/plugins/site.py`, `site_create.py`, `site_functions.py`, `sitedb.py`, `models.py`
- **Multitenancy**: `wo/cli/plugins/multitenancy.py`, `multitenancy_functions.py`, `multitenancy_db.py`, `multitenancy_health.py`, `config/plugins.d/multitenancy.conf`
- **Packaging/config**: `setup.py`, `setup.cfg`, `requirements.txt`, `config/wo.conf`
- **AI/spec context**: `CLAUDE.md`, `openspec/AGENTS.md`, `openspec/project.md`; deep plugin reference `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md`; operator cheat sheet `short-guide.md`

## Runtime/Tooling Preferences

- **Python 3** (`python_requires >= 3.4`; CI runs Ubuntu 22.04/24.04). Production CLI runs from the **`/opt/wo` virtualenv**.
- **Must run as root** — `wo` exits otherwise.
- **Package manager**: `pip`, installed from the git fork (`git+https://github.com/alnaggar-dev/WordOps.git@main`). `setup.py` `install_requires` is the source of truth; `requirements.txt` mirrors pins.
- **Pinned deps that matter**: `cement==2.10.14` (stay on 2.x), `SQLAlchemy==1.4.54`, `PyMySQL`, `pystache`, `pynginxconfig`, `psutil`, `sh`, `distro`, `argcomplete`, `colorlog`.

## Testing & QA

- **Frameworks**: nose + Cement `WOTestCase` (`wo/utils/test.py`) for CLI smoke tests; stdlib `unittest`(+`mock`) for multitenancy helpers. No pytest/tox/codecov in official config.
- **Naming**: files `{order}_test_{area}.py`; CLI classes `CliTestCase{Area}`, methods `test_wo_cli_{command}_{variant}` (e.g. `tests/cli/18_test_site_create.py`); multitenancy classes `{Feature}Tests`, methods `test_{behavior}` (`tests/cli/40_test_multitenancy_devops.py`).
- **CLI test pattern**: `with WOTestApp(argv=['site', 'create', …]) as app: app.run()`.

```bash
nosetests                                              # full suite + HTML coverage → coverage_report/
python3 setup.py test                                 # nose.collector
nosetests tests/cli/40_test_multitenancy_devops.py    # single module (nose)
python3 -m unittest tests.cli.40_test_multitenancy_devops   # single module (stdlib, no live stack)
```

- **Coverage** via the nose plugin (`setup.cfg [nosetests]`, `cover-package=wo`); artifacts are gitignored.
- **CI does not run the Python unit suite.** `.github/workflows/main.yml` runs the **integration smoke** `tests/travis.sh --actions` — a full `wo stack install` + `wo site create/update` matrix on a live server — on Ubuntu 22.04/24.04. Multitenancy unit tests (`40_test_multitenancy_devops.py`) are the only ones runnable without a live stack. `pypi.yml` (release) builds sdist/wheel and does not run tests.
