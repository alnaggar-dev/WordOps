# WordOps Multi-tenancy — Simplification Plan

## Context

The current plugin (~13,000 LOC across 4 modules, 4,486 LOC of docs) was architected for multi-tenant SaaS ergonomics (audit trails, webhooks, structured JSON logs, tagging, quarantine, staging auto-gate, shared-config-as-a-service, full source-control baselines). The **core** — shared WP core via symlinks, atomic release switching, per-site wp-config with unique Redis prefix, native WordOps nginx-include reuse, SSL via WOAcme — is excellent and stays.

This refactor strips the enterprise scaffolding for a **solo operator** with curated first-party plugins, adds three small safety primitives (`doctor`, preflight syntax check, plugins-dir immutability), and trims ~7–8k LOC.

Target end-state: ~3,500–4,000 LOC of plugin code, ~300 LOC of docs. Every line pays rent.

---

## Critical files

| File | Role |
|---|---|
| `wo/cli/plugins/multitenancy.py` | Cement controller (2,571 LOC) — trim ~800 LOC |
| `wo/cli/plugins/multitenancy_functions.py` | Logic (4,314 LOC) — trim ~2,200 LOC |
| `wo/cli/plugins/multitenancy_db.py` | ORM + migrations (1,063 LOC) — trim ~350 LOC |
| `wo/cli/plugins/multitenancy_health.py` | Health checker (356 LOC) — keep, minor edits |
| `config/plugins.d/multitenancy.conf` | User config — drop `[logging]`, `[audit]`, `[webhooks]`, `[maintenance]` sections; add `lock_plugins_dir` |
| `config/logrotate.d/wo-multitenancy` | Logrotate for the JSON structured log — **delete** |
| `wo/cli/templates/multitenancy-maintenance.mustache` | Maintenance nginx include — simplify (drop admin-IP bypass) |
| `setup.py` | Drop `config/logrotate.d/wo-multitenancy` from `data_files` |
| `install-multitenancy.sh` | Loose install script — **delete** |
| `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md` | Master doc (4,486 LOC) — **replace** with ~300-LOC reference |
| `tests/cli/40_test_multitenancy_devops.py` | Tests for deleted features — **delete** |
| `openspec/changes/refactor-simplify-multitenancy/` | New OpenSpec proposal — **create** |

---

## Guiding decisions (approved)

- **Dead SQLite columns are left in place.** SQLite 3.31 (Ubuntu 20.04 / Debian 11) lacks `DROP COLUMN`. A table-rebuild dance (`CREATE TABLE new → INSERT SELECT → DROP → RENAME`) carries non-zero outage risk on a live DB for zero payoff. Remove the columns from the ORM model + stop writing to them. `multitenancy_audit` table is also left in place (inert) — remove the ORM class and helpers only.
- **`redis_prefix` column + its unique index stay.** Essential for cache isolation.
- **`chattr` failure degrades gracefully.** Log a WARN, continue. ext4/xfs is the norm for solo Ubuntu hosts; a container/btrfs user just loses the lock feature.
- **Plugins locked, themes unlocked.** Theme activation is a DB write; theme dir doesn't need immutability.
- **`doctor` exits 1 on ERROR only.** Cron-safe; WARN doesn't page.
- **GitHub + URL plugin/theme sources stay untouched.** User's primary workflow — no simplification there.
- **Existing `wo_mt_baseline_version` WP option in tenant DBs is left alone.** Inert after the MU-plugin is removed.
- **MU-plugin file is cleaned up on `init --force`** for existing installs. New installs never create it.

---

## Phased implementation

Each phase = one commit. Order minimizes import-failure windows and lets intermediate commits run.

### Phase 0 — Prework (read-only audit, single commit allowed for tiny fixes)

- Fix the **duplicate `@staticmethod`** decorators at `multitenancy_functions.py:25-26` and `:444-445`, and the duplicate `return result` at `:164-165`, and the duplicate `config_file = ...` at `:3421-3422`. These are cosmetic but tell you the file has outgrown one brain; do them with the delete-heavy Phase 2 edits.

### Phase 1 — OpenSpec proposal

Create `openspec/changes/refactor-simplify-multitenancy/` with:
- `proposal.md` — single-paragraph "why"
- `tasks.md` — mirrors the phases below
- `specs/multitenancy/spec.md` — delta file

Delta structure:
- `## REMOVED Requirements` for Structured Logging, Machine-Readable Output (--json), Site Tagging, Audit Logging (+ retention scenario), Webhook Notifications, Staging Auto-Gate, Site Quarantine, Baseline MU-Plugin Enforcer, SharedConfig history/git/diff/rollback
- `## MODIFIED Requirements` for Health Check System (drop --json scenario), Maintenance Mode (drop admin-IP bypass scenario), Baseline Apply (drop tag/quarantine/staging scenarios), Shared Configuration (now: `edit` only)
- `## ADDED Requirements` for Preflight Syntax Check, Doctor Command, Plugins Directory Lock

Copy each MODIFIED requirement block verbatim from `openspec/specs/multitenancy/spec.md` before editing — OpenSpec replaces the full requirement on archive.

Run `openspec validate refactor-simplify-multitenancy --strict` and clear errors.

**No code changes in this phase.**

### Phase 2 — Remove telemetry (StructuredLogger / AuditLogger / WebhookNotifier / JsonOutput / json_quiet_stdout / _emit_event)

**Scope:** highest-surface delete; must go first so later phases don't import dead names.

Remove from `multitenancy_functions.py`:
- `StructuredLogger` (lines 3951–4050)
- `JsonOutput` (4053–4081)
- `json_quiet_stdout` (4084–4104)
- `AuditLogger` (4107–4198)
- `WebhookNotifier` (4201–4271)
- `_RESERVED_LOG_KEYS` + `_emit_event` (4274–4314)
- Top-of-file module-level imports that only these classes used: `_hmac`, `_hashlib`, `_urlrequest`, `_urlerror`, `_socket`, `_logging`, `_uuid`, `_time`, `_contextmanager` shims. Grep before deleting — `datetime` and `os` are still used by surviving code.

Remove from `multitenancy_db.py`:
- `MultitenancyAudit` model (62–91)
- `insert_audit_record` / `query_audit` / `prune_audit` (~971–1063)
- `from sqlalchemy import Index` if only audit used it
- The `CREATE INDEX IF NOT EXISTS idx_audit_*` block in `initialize_tables` (~211–221)

Remove from `multitenancy.py`:
- Imports on line 28–32 (`StructuredLogger, JsonOutput, AuditLogger, WebhookNotifier, _emit_event, json_quiet_stdout`)
- Every `_structured = StructuredLogger(self)` local + `.info/.warn/.error` call
- Every `_emit_event(...)` call (lines 478, 512, 628, 642, 703, 717, 1172, 1188, 1204, 1727, 2381)
- Every `JsonOutput.should_emit(pargs)` branch (keep the plain-text branch under it)
- Every `with json_quiet_stdout(pargs):` wrapper (in `create`, `_delete_impl`, `apply`, `maintenance`, `webhook`)
- The `audit` subcommand + `_parse_since` helper
- The `webhook` subcommand
- `--json`, `--since`, `--target`, `--format` from `Meta.arguments` (lines 108, 110–112)
- `--verbose` flag on apply (no longer needed since per-site prints are simpler)

Replace each structured-log call with a single plain `Log.info(self, f"{event} target={domain} result={result}")`. WordOps' log file (`/var/log/wo/wordops.log`) captures it — no correlation IDs.

Config/setup:
- Remove `[logging]`, `[audit]`, `[webhooks]` sections from `config/plugins.d/multitenancy.conf` (lines 100–123)
- Delete `config/logrotate.d/wo-multitenancy`
- Drop the `('/etc/logrotate.d/', ['config/logrotate.d/wo-multitenancy'])` entry in `setup.py` (lines 82–83)
- Drop the `MTFunctions.load_config` branches that parse `[logging]`, `[audit]`, `[webhooks]` (lines 108–147 in functions.py)

**Delete `tests/cli/40_test_multitenancy_devops.py` in this phase** to keep CI green — the file imports symbols removed here. Phase 11 adds replacement tests.

### Phase 3 — Remove tagging

Remove from `multitenancy.py`:
- `--tags` argparse flag (line 109)
- Every `validate_tags(pargs.tags)` call (267, 851, 1691) and the surrounding `tags_list`/`tag_filter` plumbing
- `MTDatabase.update_site_tags(...)` calls (458, 502)
- `tags` display column in `list` and `site_data` result payloads

Remove from `multitenancy_functions.py`:
- `validate_tags` function (3927–3944) and any module-level `_TAG_RE` helper near it
- `tag_filter` parameter + filtering branches in `apply_baseline_to_sites` (2449–2628): drop the `wanted = set(tag_filter)` filter, the `'tags': site['tags']` keys, and the `'tag_filter'` return field

Remove from `multitenancy_db.py`:
- `tags = Column(Text)` (line 57) — ORM-level only; **column stays in SQLite**
- The `tags` migration block (193–202)
- `tags=site_data.get('tags')` in `add_shared_site` (426)
- `'tags': ...` key in `get_shared_sites` return (458)
- `update_site_tags` method (~936)
- `get_sites_by_tags` method (~952)

### Phase 4 — Remove quarantine / staging / unquarantine

Remove from `multitenancy.py`:
- `staging` controller command (~1049–1103)
- `unquarantine` baseline subcommand (~2054–2142)
- Quarantine/staging counters in `status` (770–771)
- Quarantine printout + `unquarantine` hint in `validate` (1019–1029, plus the `get_quarantined_sites` call at 1017)

Rewrite `apply_baseline_to_sites` in `multitenancy_functions.py` (2449–2628):
- Delete the entire staging gate (2465–2504)
- Simplify the production_sites comprehension to `[{'domain': s.domain, 'site_path': s.site_path} for s in sites if s.is_enabled]`
- Change the failure branch (2588–2595) from `MTDatabase.mark_site_quarantined(...)` to `Log.warn(app, f"  ❌ {domain}: {result['error']}")` and increment a `failed_count`. Status becomes `'status': 'failed'` not `'status': 'quarantined'`.
- Drop the `'quarantined'` key from the return summary; keep `attempted / succeeded / failed / sites`.

Remove from `multitenancy_db.py`:
- `is_staging`, `is_quarantined`, `quarantine_reason`, `quarantine_date` from `MultitenancySite` model (50–53) — **columns stay in SQLite**
- The quarantine/staging migration block (115–154)
- `get_staging_site` (~622), `mark_site_quarantined` (~653), `unquarantine_site` (~675), `get_quarantined_sites` (~697)
- `is_staging` / `is_quarantined` keys in `get_shared_sites` return (455–457)

Remove from `multitenancy_health.py`:
- The four `'quarantined': bool(site.get('is_quarantined'))` keys in `_check_single_site` return dicts (lines 280, 292, 301, 310). The health module is otherwise self-contained and untouched.

### Phase 5 — Replace SharedConfig with `edit`-only

In `multitenancy_functions.py`:
- Delete the entire `SharedConfig` class (2638–3925).
- Keep **only** two pieces of functionality as free functions:
  - `create_shared_config_file(app, shared_root)` — lifted unchanged from the deleted class; still seeds the initial PHP file on `init`.
  - `edit_shared_config(app, shared_root)` — new: (1) `cp` the file to `wp-config-shared.php.bak.<YYYYMMDD_HHMMSS>`, (2) prune `.bak.*` files beyond the 10 newest, (3) exec `os.environ.get('EDITOR', 'vi')` on the file via `subprocess.call`, (4) run `php -l` after the editor exits, (5) if `php -l` fails, restore the latest `.bak` and log error with the path.

In `multitenancy.py`:
- Replace the 94-line `shared_config` method with a ~15-line body that: if `pargs.config_action == 'edit'` calls `edit_shared_config(self, shared_root)`; any other action errors with "only 'edit' is supported."
- Drop `--key`, `--value`, `--dry-run`, `--config-version`, `--to-commit` from `Meta.arguments` (101–106). Keep `--action` since `--action edit` is the only entry point.
- Drop the `initialize_config_git` call in `init` (line 229); git history for this one file is not worth the subsystem.

### Phase 6 — Remove MU-plugin enforcer

In `multitenancy_functions.py`:
- Delete `create_mu_plugin` (~1951) and `get_mu_plugin_content` (~1960 through the end of the PHP heredoc).

In `multitenancy.py::init`:
- Remove the `infra.create_mu_plugin()` call (~line 167).
- Add a cleanup line: `mu_path = f"{shared_root}/wp-content/mu-plugins/wo-baseline-enforcer.php"; os.path.exists(mu_path) and os.remove(mu_path)` so `wo multitenancy init --force` cleans old installs.

The `wo_mt_baseline_version` WP option in per-site DBs is left alone — inert after the enforcer is gone. `wo multitenancy baseline apply` becomes the sole propagation mechanism (already does the imperative work).

### Phase 7 — Simplify maintenance mode

In `multitenancy.py::_maintenance_enable`:
- Drop `admin_ips` fetch + `admin_regex` build; drop `has_admin_ips` / `admin_ips_regex` from `nginx_data`.
- Delete the module-level `_escape_ip` helper (~2546).

In `config/plugins.d/multitenancy.conf`:
- Delete the `[maintenance]` section entirely (128–130). Hard-code retry-after to 600 seconds in the nginx template.

In `wo/cli/templates/multitenancy-maintenance.mustache`:
- Delete the `{{#has_admin_ips}}…{{/has_admin_ips}}` block (lines 5–9).
- Replace `{{retry_after_seconds}}` with the literal `600`, or keep the placeholder but pass `retry_after_seconds=600` unconditionally.

In `multitenancy_functions.py::load_config`:
- Remove the `[maintenance]` parser block (149–161).

### Phase 8 — Add preflight + doctor + plugins-dir immutability

New in `multitenancy_functions.py`:

1. `MTFunctions.preflight_shared_config(app, shared_root) -> bool`:
   - Runs `subprocess.run(['php', '-l', f'{shared_root}/config/wp-config-shared.php'])`.
   - On non-zero return, `Log.error(app, "wp-config-shared.php has a PHP syntax error. Fix via: wo multitenancy shared-config --action edit")` and return `False`.
   - Returns `True` when the file doesn't yet exist (first `init` call) or validates cleanly.
   - Called at the top of `init`, `_create_impl`, `_apply_impl`, `update`.

2. `MTFunctions.lock_plugins_dir(app, shared_root)` / `unlock_plugins_dir(app, shared_root)`:
   - Reads `lock_plugins_dir` (default `true`) from `[multitenancy]` config.
   - Runs `chattr +i -R` / `chattr -i -R` on `{shared_root}/wp-content/plugins`.
   - **Graceful fallback**: if `chattr` binary missing, or exit code indicates unsupported FS, or EPERM, log `Log.warn(app, "lock_plugins_dir skipped: chattr unavailable or unsupported filesystem")` and return — never raise.
   - Wrapper pattern for mutating ops: try/finally where `unlock` is first and `lock` is in `finally`. Call sites: `seed_plugins_and_themes`, `download_plugin`, `download_plugin_from_github`, `download_plugin_from_url`, `update_plugin`, `remove_plugin`, `apply_baseline` (if it ever writes to plugins — currently only reads, so no unlock needed there).

3. New `multitenancy.py::doctor` command:
   - Runs `HealthChecker(self).register_defaults().run_all()` and renders text.
   - Adds four local cross-checks:
     - Read `baseline.json["version"]` and compare against `MTDatabase.get_baseline_version(self)` (which reads the `baseline_version` key from `multitenancy_config`).
     - For each `MultitenancySite`, compare its `baseline_version` column to the file value — flag drift.
     - For each site, `os.readlink('/var/www/<domain>/htdocs/wp')` and assert it resolves inside `{shared_root}/current`.
     - Run `preflight_shared_config(self, shared_root)` and flag failure.
   - Return exit code `1` on any ERROR (via `sys.exit(1)`), `0` otherwise. WARN does not fail.

4. Add `lock_plugins_dir = true` under `[multitenancy]` in `config/plugins.d/multitenancy.conf` with a `###` comment explaining the chattr fallback.

### Phase 9 — Drop install script + setup cleanup

- Delete `/Users/alnaggar/dev/WordOps/install-multitenancy.sh`.
- Verify no references in README.md, setup.py, CI, or docs (already checked: none).
- Confirm `setup.py` logrotate deletion from Phase 2 landed.

### Phase 10 — Shrink docs

Replace `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md` (4,486 lines) with a ~300-line reference:
- **Overview** — what this does, core model (shared core + per-site wp-config).
- **Install & activate** — one `pip install .` + activation conf.
- **Commands** — `init | create | update | rollback | status | list | validate | remove | delete | baseline add/remove/set/update/apply/history | shared-config edit | maintenance | health | doctor`.
- **Directory structure** — the two canonical trees.
- **Tenancy model caveats** — one paragraph: "This is a trust-model of one operator with curated plugins. Plugins that write to their own directory (caches, rules) will collide. Store anything mutable under `wp-content/uploads`." (User accepted the blind spot; still worth documenting for future-self.)
- **Troubleshooting** — 5–10 common issues, not 2,000 lines.

`short-guide.md` remains untouched — it already is the primary user doc.

Move the deleted 4,186 lines to `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V1-archive.md` if retention matters; otherwise just delete.

### Phase 11 — Tests

**Delete** `tests/cli/40_test_multitenancy_devops.py` (already queued for Phase 2 to keep CI green).

**Add** `tests/cli/40_test_multitenancy.py` with four unit tests — no live stack required, all mockable:

| Test | Asserts |
|---|---|
| `test_preflight_rejects_bad_php` | Write syntactically broken PHP to a tmpfile, call `MTFunctions.preflight_shared_config`, assert returns `False` and logs an error. |
| `test_doctor_detects_version_drift` | Mock `MTDatabase.get_baseline_version` vs a site row's `baseline_version` column returning different integers; assert doctor flags the mismatch and exit code is 1. |
| `test_chattr_wrap_order` | Mock `subprocess.run`; call `lock_plugins_dir` → plugin write → `unlock_plugins_dir`; assert exactly the two chattr invocations in order and with `-R`. |
| `test_maintenance_enable_writes_include_without_admin_regex` | Mock `renderer.render`; call `_maintenance_enable`; assert the rendered mustache context has no `has_admin_ips` / `admin_ips_regex` keys. |

All other tests in `tests/cli/` remain untouched — none reference multitenancy beyond the deleted file.

### Phase 12 — OpenSpec archive

After phases 2–11 land:
- `openspec archive refactor-simplify-multitenancy` moves the proposal into `openspec/changes/archive/2026-04-17-refactor-simplify-multitenancy/` and applies the REMOVED / MODIFIED / ADDED deltas to `openspec/specs/multitenancy/spec.md`.
- `openspec validate --strict` to confirm the spec file is coherent.

---

## Reused existing utilities

None of this invents fresh machinery; everything leverages what's already there:

- `HealthChecker.register_defaults().run_all()` at `multitenancy_health.py:45-82` — doctor's foundation.
- `MTDatabase.get_baseline_version` at `multitenancy_db.py:362` — for version drift check.
- `MTFunctions.load_config` at `multitenancy_functions.py:27` — for reading new `lock_plugins_dir` flag.
- `MTFunctions.safe_nginx_reload` at `multitenancy_functions.py:312` — used by the simplified maintenance mode unchanged.
- `MTFunctions.validate_nginx_config` at `multitenancy_functions.py:204` — preflight for nginx after maintenance changes.
- `WOFileUtils.create_symlink` and `WOService.reload_service` — already-plumbed WordOps primitives.
- `create_shared_config_file` — lifted out of deleted `SharedConfig`, reused as a free function.

---

## Verification

After each phase:

```bash
# Static sanity
python3 -c "from wo.cli.plugins.multitenancy import WOMultitenancyController; print('import ok')"
python3 -m py_compile wo/cli/plugins/multitenancy*.py
grep -rn "StructuredLogger\|AuditLogger\|WebhookNotifier\|JsonOutput\|json_quiet_stdout\|_emit_event\|validate_tags\|mark_site_quarantined\|get_staging_site\|unquarantine\|create_mu_plugin\|_escape_ip" wo/cli/plugins/
# expect: no hits after Phase 7

# Unit tests
pytest tests/cli/40_test_multitenancy.py -v

# CLI smoke (in a throwaway VM or container; do NOT run on a live host from the plan)
wo multitenancy --help                     # no trace of --json / --tags / --since / --format
wo multitenancy init --force               # clean init, creates wp-config-shared.php, no MU-plugin
wo multitenancy doctor                     # exits 0, shows all checks green
wo multitenancy create test.local --php83 --wpfc
  # confirm: preflight ran, chattr +i applied to plugins dir, site works
wo multitenancy baseline add-plugin hello-dolly
  # confirm: unlock → download → lock cycle
wo multitenancy shared-config --action edit
  # confirm: $EDITOR opens, php -l runs on save, .bak file created
wo multitenancy maintenance --enable --site=test.local
  # confirm: 503 from all IPs; simple mustache include, no admin regex
wo multitenancy remove --force             # clean teardown

# OpenSpec
openspec validate refactor-simplify-multitenancy --strict
openspec archive refactor-simplify-multitenancy
openspec validate --strict
```

---

## Rollback strategy

- Each phase = one commit on a feature branch. If any phase breaks something, `git revert <phase-commit>` restores that slice cleanly — later phases don't depend on earlier ones except via the import-order guardrail in Phase 2.
- The DB columns and `multitenancy_audit` table are left in place, so a full revert requires no DB rollback.
- Existing sites continue working unchanged; the plugin keeps the same core architecture, same wp-config, same nginx template, same Redis prefix. Only DevOps metadata and the MU-plugin file go away.

---

## Estimated impact

| Area | Before | After | Delta |
|---|---|---|---|
| multitenancy.py | 2,571 LOC | ~1,750 LOC | −820 |
| multitenancy_functions.py | 4,314 LOC | ~2,100 LOC | −2,214 |
| multitenancy_db.py | 1,063 LOC | ~700 LOC | −363 |
| multitenancy_health.py | 356 LOC | ~345 LOC | −11 |
| docs (V2 md) | 4,486 LOC | ~300 LOC | −4,186 |
| tests devops | 387 LOC | 0 | −387 |
| tests new | 0 | ~150 LOC | +150 |
| **Total plugin+docs** | **~13,200 LOC** | **~5,350 LOC** | **−7,831 LOC (−59%)** |

New reliability wins:
- No telemetry code → no telemetry bugs (10 P1/P2 bugs avoided per the A.12 history).
- Preflight syntax check catches the "shared config typo = all sites down" class.
- `chattr +i` closes the "WP-admin clicks 'update plugin' and nukes neighbors" class.
- `doctor` gives one command for "is anything drifted?"