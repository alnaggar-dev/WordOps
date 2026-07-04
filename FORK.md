# WordOps Fork Notes (alnaggar-dev)

This fork installs and updates itself from Git only, using the `alnaggar-dev/WordOps` repository as the source for operator-facing install and update flows. The default branch is `main`, and version `3.22.0` comes directly from `setup.py`; there is no PyPI install path and no GitHub releases API version check.

For the multi-tenancy plugin that ships with this fork, see `MULTITENANCY.md`.

## Install

Use the fork's raw install script from the `main` branch.

```bash
wget -qO wo https://raw.githubusercontent.com/alnaggar-dev/WordOps/main/install && sudo bash wo
```

For silent automation, append `--force`.

```bash
wget -qO wo https://raw.githubusercontent.com/alnaggar-dev/WordOps/main/install && sudo bash wo --force
```

Install behavior:

- The default branch is `main`.
- A Travis override branch, `updating-configuration`, exists for CI only.
- Real installs use pip against the fork Git URL:

```bash
pip install -I "git+https://github.com/alnaggar-dev/WordOps.git@<branch>#egg=wordops"
```

- The old `pip install wordops` path is not used.
- CI local mode can install from the checkout with `pip install .`.
- The installer bootstraps Git identity silently when it is missing, so `wo` does not block on prompts.
- That bootstrap writes `~/.gitconfig` or `/root/.gitconfig` with `user.name=${USER:-WordOps}`, `user.email=root@$HOSTNAME.local`, and a `safe.directory` entry.
- The Debian nginx repo key is intentionally still fetched from the upstream WordOps repository.

## Update

Use the standard update command for the fork default branch.

```bash
wo update
```

Use `--force` for a silent update.

```bash
wo update --force
```

Use `--branch <name>` to override the default branch.

```bash
wo update --branch <name>
```

Update behavior:

- The default update branch is `main`.
- `--branch <name>` overrides the branch.
- `--mainline` and `--beta` still exist in the CLI, but they error out with a fork-specific unsupported-branch message.
- The changelog URL points to `https://github.com/alnaggar-dev/WordOps/commits/main`.
- The update install-script download points to the fork raw install script.
- Version information is read from `setup.py`.
- There is no GitHub releases check.

## Removed vs kept

| Status | Item |
| --- | --- |
| Removed/disabled | PyPI install path |
| Removed/disabled | GitHub releases version check |
| Removed/disabled | Mainline and beta branches; the flags remain but error out |
| Kept intentionally upstream | `docs.wordops.net` |
| Kept intentionally upstream | `community.wordops.net` |
| Kept intentionally upstream | `demo.wordops.eu` |
| Kept intentionally upstream | `github.com/WordOps/docs.wordops.net` |
| Kept intentionally upstream | Debian nginx `repo.key` fetched from `WordOps/WordOps` |

## Where the fork identity lives

| File | Fork identity carried there |
| --- | --- |
| `install` | Default branch `main`; fork Git install URL; silent Git identity bootstrap; `safe.directory`; CI-only Travis override branch. |
| `wo/cli/plugins/update.py` | Default update branch `main`; `--branch <name>` override; fork changelog URL; fork raw install URL; `--mainline` and `--beta` unsupported-branch error. |
| `setup.py` | Version `3.22.0`; `url`, `Source`, and `Tracker` metadata point to the fork; docs and community URLs remain upstream. |
| `wo/core/variables.py` | Silent Git identity defaults via `getpass.getuser()` and `socket.getfqdn()`, with fallbacks `WordOps` and `localhost`; version `3.22.0`. |
| `wo/cli/templates/wo-update.mustache` | Reads the latest version by grepping `version='` from the fork raw `setup.py`; no releases API. |
| `wo/cli/templates/sysctl.mustache` | URL updated to the fork. |
| `README.md` | Carries fork URLs. |
| `CHANGELOG.md` | Carries fork URLs, with about 45 references. |
| `CONTRIBUTING.md` | Carries fork URLs. |
| `LICENSE` | Carries fork URLs. |
| `docs/wo.8` | Carries fork URLs. |
| `tests/issue.sh` | Carries fork URLs. |
