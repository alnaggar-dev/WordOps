---
name: OpenSpec: Archive
description: Archive a deployed OpenSpec change and update specs.
category: OpenSpec
tags: [openspec, archive]
---
<!-- OPENSPEC:START -->
**Guardrails**
- Favor straightforward, minimal implementations first and add complexity only when it is requested or clearly required.
- Keep changes tightly scoped to the requested outcome.
- Refer to `openspec/AGENTS.md` (located inside the `openspec/` directory—run `ls openspec` or `openspec update` if you don't see it) if you need additional OpenSpec conventions or clarifications.

**Steps**
1. Determine the change ID to archive:
   - If this prompt already includes a specific change ID (for example inside a `<ChangeId>` block populated by slash-command arguments), use that value after trimming whitespace.
   - If the conversation references a change loosely (for example by title or summary), run `openspec list` to surface likely IDs, share the relevant candidates, and confirm which one the user intends.
   - Otherwise, review the conversation, run `openspec list`, and ask the user which change to archive; wait for a confirmed change ID before proceeding.
   - If you still cannot identify a single change ID, stop and tell the user you cannot archive anything yet.
2. Validate the change ID by running `openspec list` (or `openspec show <id>`) and stop if the change is missing, already archived, or otherwise not ready to archive.
3. Run `openspec archive <id> --yes` so the CLI moves the change and applies spec updates without prompts (use `--skip-specs` only for tooling-only work).
4. Review the command output to confirm the target specs were updated and the change landed in `changes/archive/`.
5. Validate with `openspec validate --strict` and inspect with `openspec show <id>` if anything looks off.
6. **Update Project Documentation:**
   - Read `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md` (the source of truth for this project).
   - Based on the archived change's `proposal.md` and implemented code, update the relevant section(s) in the documentation:
     - **New commands** → Add to "Commands Reference" section
     - **New configuration options** → Add to "Configuration" section
     - **Architecture changes** → Update "Architecture" or "How It Works" sections
     - **New troubleshooting items** → Add to "Troubleshooting" section
     - **New FAQ items** → Add to "FAQ" section
     - **Bug fixes with user impact** → Add to "Troubleshooting" if relevant
   - Follow the existing documentation style and format.
   - If the change is internal/refactoring with no user-facing impact, note this and skip documentation update.

**Reference**
- Use `openspec list` to confirm change IDs before archiving.
- Inspect refreshed specs with `openspec list --specs` and address any validation issues before handing off.
- `WORDOPS-MULTITENANCY-PLUGIN-DOCS-V2.md` is the main documentation file and must stay in sync with implemented features.
<!-- OPENSPEC:END -->
