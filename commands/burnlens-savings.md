---
description: Show what the fixes you applied with /burnlens-fix have actually saved
allowed-tools: Bash(bash:*)
---

burnlens savings:

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/burnlens.sh" savings`

Summarize this for the user in a few lines: for each fix, whether it's measured yet and, if so, the tokens and API-equivalent dollars saved, with the before → after figure; then the total measured saving. Fixes marked "measuring" need a few more sessions before there's enough data to compare. If nothing has been applied yet, suggest `/burnlens-fix`.
