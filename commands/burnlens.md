---
description: Open your burnlens token-usage dashboard and summarize where your tokens went
argument-hint: "[--redact] [--demo] [--no-open] | sync | connect --server URL --token T [--team NAME]"
allowed-tools: Bash(python3:*)
---

burnlens output:

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/run.py" $ARGUMENTS`

If the arguments started with `sync` or `connect`, report the result in one or two lines and stop.

Otherwise, using only the output above, give the user a short readout:
- One line with the totals (tokens, API-equivalent cost, cache hit rate).
- The two or three most useful observations, e.g. which tool or MCP server carries the most context, or which work type costs most, each with one concrete way to reduce it.
- The dashboard path as a clickable link.

Keep it under ten lines. If the output shows an error, explain it and suggest `--demo` to preview the dashboard.
