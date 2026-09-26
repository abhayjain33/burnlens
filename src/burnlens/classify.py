"""Heuristic work-type classification for a user turn.

A turn is one user prompt plus every API call and tool call it triggered.
Signals come from what the agent *did* (tools, file types), falling back to
prompt keywords only when no tools ran. Prompt text is never stored.
"""

import os
import re

CATEGORIES = ["Development", "Documentation", "Analysis", "Design", "Research", "Conversation"]

DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc", ".org", ".tex"}
DESIGN_EXT = {".css", ".scss", ".sass", ".less", ".svg", ".fig", ".sketch", ".xd", ".pen"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
WEB_TOOLS = {"WebSearch", "WebFetch"}
DEV_BASH = re.compile(
    r"\b(npm|pnpm|yarn|bun|pip|poetry|uv|cargo|go|mvn|gradle|make|pytest|jest|vitest|tsc|docker|git\s+(commit|push|checkout|merge|rebase))\b"
)
KEYWORDS = [
    ("Documentation", re.compile(r"\b(doc|docs|documentation|readme|write[- ]?up|changelog|guide)\b", re.I)),
    ("Design", re.compile(r"\b(design|ui|ux|mockup|wireframe|figma|layout|style|theme)\b", re.I)),
    ("Development", re.compile(r"\b(implement|fix|bug|build|refactor|code|function|test|deploy|feature)\b", re.I)),
    ("Analysis", re.compile(r"\b(analy[sz]e|why|explain|investigate|understand|review|compare|how does)\b", re.I)),
    ("Research", re.compile(r"\b(research|search|look up|find out|latest|docs for)\b", re.I)),
]


def _ext(path):
    return os.path.splitext(path or "")[1].lower()


def classify_turn(tool_uses, prompt=""):
    """tool_uses: list of (name, input_dict). Returns a CATEGORIES entry."""
    score = dict.fromkeys(CATEGORIES, 0)
    for name, inp in tool_uses:
        inp = inp if isinstance(inp, dict) else {}
        if name in EDIT_TOOLS:
            ext = _ext(inp.get("file_path") or inp.get("notebook_path"))
            if ext in DOC_EXT:
                score["Documentation"] += 3
            elif ext in DESIGN_EXT:
                score["Design"] += 3
            else:
                score["Development"] += 3
        elif name in READ_TOOLS:
            score["Analysis"] += 1
        elif name == "Bash":
            if DEV_BASH.search(inp.get("command", "")):
                score["Development"] += 1
            else:
                score["Analysis"] += 1
        elif name in WEB_TOOLS:
            score["Research"] += 2
        elif name in ("Agent", "Task"):
            st = (inp.get("subagent_type") or "").lower()
            score["Analysis" if st in ("explore", "plan") else "Development"] += 1
        elif name == "Artifact" or "design" in name.lower() or "figma" in name.lower():
            score["Design"] += 2
        elif name.startswith("mcp__"):
            low = name.lower()
            if any(k in low for k in ("browser", "search", "fetch", "web")):
                score["Research"] += 1
            elif any(k in low for k in ("confluence", "notion", "docs", "wiki")):
                score["Documentation"] += 1
            else:
                score["Analysis"] += 1
    best = max(CATEGORIES, key=lambda c: score[c])
    if score[best] > 0:
        return best
    for cat, rx in KEYWORDS:
        if prompt and rx.search(prompt):
            return cat
    return "Conversation"
