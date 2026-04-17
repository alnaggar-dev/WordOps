# Project Context

## Who I am

I am the **solo devops operator and sole author/maintainer** of this WordOps fork and the multi-tenancy plugin it ships. No team, no CI gatekeepers, no multi-operator fleet, no enterprise SaaS constraints. I write the WordPress plugins that run on the tenant sites myself (most of them live in my own GitHub repos), and I trust my own code.

## What this repo is

A fork of [WordOps](https://github.com/WordOps/WordOps) with a custom **multi-tenancy plugin** at `wo/cli/plugins/multitenancy*.py` that lets many WordPress sites share a single WordPress core via symlinks. Goal: ~90% disk savings, one-command updates across all sites, atomic rollback via symlink-swap — for **my own fleet**, not a SaaS product.

**Core architecture:**
- Shared WP core at `/var/www/shared/releases/wp-<ts>/` + `current` symlink
- Per-site `wp-config.php` + "router" `wp-config.php` inside the shared core (resolves site from `DOCUMENT_ROOT`)
- Unique Redis prefix per tenant (cache isolation)
- Native WordOps integration: `setupdatabase()`, `WOAcme`, `WOService`, modular nginx includes (`common/wpfc-php83.conf`, etc.)

## How to work with me

- This is a **trust-model-of-one** tool. Per-tenant isolation, audit trails, tamper detection, multi-operator workflows, staging-gate / quarantine mechanics are **non-goals**. Do not propose them.
- Prefer **deleting code** over adding abstractions. Reducing surface area is a feature. If something only pays for itself in a team setting, it doesn't pay.
- When I ask for a review/critique/plan, **be direct** — lead with the recommendation, name what to delete/keep/simplify, skip "it depends on your team" hedging.
- Default to one recommended approach, not a menu of enterprise options.
- Trade-offs that would be unsafe in a multi-operator environment (shared wp-content, `wp-config-shared.php` as a global single point of failure, WP admin writing to shared plugin files) are **accepted risks** because the blast radius is "my own sites running my own code". Don't re-litigate them unless I ask.



<!-- OPENSPEC:START -->
# OpenSpec Instructions

These instructions are for AI assistants working in this project.

Always open `@/openspec/AGENTS.md` when the request:
- Mentions planning or proposals (words like proposal, spec, change, plan)
- Introduces new capabilities, breaking changes, architecture shifts, or big performance/security work
- Sounds ambiguous and you need the authoritative spec before coding

Use `@/openspec/AGENTS.md` to learn:
- How to create and apply change proposals
- Spec format and conventions
- Project structure and guidelines

Keep this managed block so 'openspec update' can refresh the instructions.

<!-- OPENSPEC:END -->