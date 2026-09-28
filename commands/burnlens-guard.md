---
description: Turn burnlens live guardrails on or off, change their settings, or set up the cost status line
argument-hint: "[on | off | what you want, e.g. 'read guard deny' or 'status line']"
allowed-tools: Bash(bash:*), Read
---

Current guardrail settings:

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" guard status`

The user asked: $ARGUMENTS

What the guardrails do (explain only what's relevant, briefly):
- Master switch: `guard on` / `guard off` turns every guardrail on or off. The status line is not a guardrail and keeps working either way.
- `read_guard` (ask | deny | off): before Claude reads a generated file (lockfile, bundle) above `generated_min_tokens`, or any file above `max_read_tokens` without a line range, `ask` shows a permission prompt with the token cost; `deny` makes Claude read a part or grep instead.
- `loop_guard`, `loop_threshold`: when a Bash command or file read returns the same result `loop_threshold` times, Claude is told to change approach.
- `context_warn_tokens`: tells the user when a session's context passes this size (and every 50k after), with the per-turn cost.

Do this:
1. If the arguments are `on` or `off`, run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" guard on` (or `off`) and confirm in one line. Stop.
2. If the user didn't ask for anything specific, summarize the current state in a few lines (on/off, and anything marked "changed") and offer: turning guardrails off or on, read guard ask/deny/off, and the status line. Then stop.
3. To change one setting, run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" guard set <key> <value>` (e.g. `guard set read_guard deny`, `guard set context_warn_tokens 200000`, `guard set loop_guard false`) and confirm the new value in one line. `guard reset` restores defaults.
4. Status line (shows session cost, context size and cache hit rate): run `bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" guard install-statusline`. Then read `~/.claude/settings.json`. If it has no `statusLine`, add `"statusLine": {"type": "command", "command": "bash ~/.burnlens/statusline.sh"}` with your edit tool so the user approves it. If a `statusLine` already exists, show it and ask before replacing it. The status line appears from the next message.
5. For a single session only, the user can instead set the environment variable `BURNLENS_GUARD=off`.
