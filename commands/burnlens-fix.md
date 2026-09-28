---
description: Find fixes that cut your Claude Code token usage, apply the ones you pick, and track what they save
argument-hint: "[--days N] [--demo]"
allowed-tools: Bash(bash:*), Read, Glob
---

burnlens recommendations (JSON list):

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" fixes --json $ARGUMENTS`

Work through these with the user:

1. If the arguments include `--demo`, these are example recommendations for synthetic data: show them as in step 2, say they're examples, and don't apply or record anything.

   If the output above is not a JSON list, it's an error: explain it in plain words and stop. If the list is empty, say there is nothing to fix right now, mention `/burnlens-savings` for fixes applied earlier, and stop.

2. Show at most 8 recommendations, in the order given, as a numbered list. For each: the title; the saving as `~$X/month` (or "saving not quantifiable" when `usd_30d` is null) with its `confidence`; one line of evidence from `detail`; and one line saying what the fix changes. Then ask which to apply: numbers, "all", or "none". Wait for the answer.

3. Apply each chosen fix according to `action.type`, using your normal file and shell tools so the user approves every change:
   - `settings_deny`: in `action.file` (create it as `{}` if missing), add each of `action.rules` to `permissions.deny` without duplicating existing entries or touching anything else. Mention `action.note`.
   - `settings_json`: merge `action.append` into `action.file`, adding list items without duplicates.
   - `claude_md`: append `action.text` to `action.file` under a `## Token hygiene (added by burnlens)` heading, creating the file or heading if needed and skipping lines that are already there.
   - `claude_task`: carry out `action.prompt`, including its instruction to show a diff and wait for approval.
   - `command`: show `action.command` and `action.note`. If it starts with `/`, it's a Claude Code command the user must type themselves; otherwise run it only after the user confirms.

4. After a fix is applied successfully, record it so its savings can be measured:
   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" fixes applied <id>`
   If the user says a recommendation should never be shown again, run the same with `dismiss <id>` instead.

5. Finish with a short summary: what was applied, the combined projected saving per month, and that `/burnlens-savings` will show measured before/after numbers once there has been some new usage (a few sessions).
