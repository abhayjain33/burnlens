---
description: View or change burnlens live guardrails (read guard, loop guard, context alert, budget) and set up the cost status line
argument-hint: "[what you want, e.g. 'daily budget $20' or 'status line']"
allowed-tools: Bash(bash:*), Read
---

Current guardrail settings:

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" guard status`

The user asked: $ARGUMENTS

What each setting does (explain only what's relevant, briefly):
- `read_guard` (ask | deny | off): before Claude reads a generated file (lockfile, bundle) above `generated_min_tokens`, or any file above `max_read_tokens` without a line range, `ask` shows the user a permission prompt with the token cost, `deny` makes Claude read a part or grep instead.
- `loop_guard`, `loop_threshold`: when a Bash command or file read returns the same result `loop_threshold` times, Claude is told to change approach.
- `context_warn_tokens`: tells the user when a session's context passes this size (and every 50k after), with the per-turn cost.
- `daily_usd`, `monthly_usd`, `budget_action` (warn | block): API-equivalent spend limits. `warn` shows a message at 80% and 100%; `block` also pauses new prompts once a limit is reached. `none` turns a limit off.

Do this:
1. If the user didn't ask for anything specific, summarize the current settings in a few lines (mention anything marked "changed") and offer the common changes: a daily or monthly budget, read guard ask/deny/off, and the status line. Then stop.
2. To change a setting, run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" guard set <key> <value>` (for example `guard set daily_usd 20`, `guard set budget_action block`, `guard set monthly_usd none`). Confirm the new value in one line. `guard reset` restores defaults.
3. Status line (shows session cost, context size, cache hit rate, and today's spend against the budget): run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" guard install-statusline`. Then read `~/.claude/settings.json`. If it has no `statusLine`, add `"statusLine": {"type": "command", "command": "bash ~/.burnlens/statusline.sh"}` with your edit tool so the user approves it. If a `statusLine` already exists, show it and ask before replacing it. Tell the user the status line appears on their next message.
4. To turn all guardrails off for a session, the user can set the environment variable `BURNLENS_GUARD=off`.
