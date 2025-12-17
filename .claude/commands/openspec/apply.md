---
name: OpenSpec: Apply
description: Implement an approved OpenSpec change and keep tasks in sync.
category: OpenSpec
tags: [openspec, apply]
---
<!-- OPENSPEC:START -->
**Guardrails**
- Favor straightforward, minimal implementations first and add complexity only when it is requested or clearly required.
- Keep changes tightly scoped to the requested outcome.
- Refer to `openspec/AGENTS.md` (located inside the `openspec/` directory—run `ls openspec` or `openspec update` if you don't see it) if you need additional OpenSpec conventions or clarifications.
- Follow the "Extend First, Customize Second" principle from `openspec/AGENTS.md` - prefer WordOps native functions over custom implementations.

**Steps**
Track these steps as TODOs and complete them one by one.
1. Read `changes/<id>/proposal.md`, `design.md` (if present), and `tasks.md` to confirm scope and acceptance criteria. Reference `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md` for existing patterns, command syntax, and documentation style to maintain consistency.
2. Work through tasks sequentially, keeping edits minimal and focused on the requested change. Follow existing code patterns in `wo/cli/plugins/multitenancy*.py`.
3. Test changes before marking complete:
   - `nginx -t` for nginx configuration changes
   - `wo multitenancy --help` to verify plugin loads
   - Manual verification of new commands/features
4. Confirm completion before updating statuses—make sure every item in `tasks.md` is finished.
5. Update the checklist after all work is done so each task is marked `- [x]` and reflects reality.
6. Reference `openspec list` or `openspec show <item>` when additional context is required.

**Reference**
- Use `openspec show <id> --json --deltas-only` if you need additional context from the proposal while implementing.
- See "WordOps Plugin Guidelines" in `openspec/AGENTS.md` for native functions to use (setupdatabase, WOAcme, WOService, Log, etc.).
- Key files: `multitenancy.py` (controller), `multitenancy_functions.py` (logic), `multitenancy_db.py` (database).
<!-- OPENSPEC:END -->
