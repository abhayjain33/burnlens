"""Live guardrails, run as Claude Code hooks (see hooks/hooks.json).

  pre-tool     PreToolUse on Read: large or generated files read without a range
  post-tool    PostToolUse on Bash/Read: the same result coming back again and again
  prompt       UserPromptSubmit: context-size alert and spend budget
  statusline   status line: session cost, context, cache hit rate, today vs budget

Every entry point reads the hook's JSON from stdin, prints a JSON reply (or
nothing), and never raises: a guardrail bug must not break a session.
Settings: ~/.burnlens/guard.json. BURNLENS_GUARD=off disables everything.
"""

import calendar
import hashlib
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".burnlens")
CONFIG = os.path.join(BASE, "guard.json")
STATE_DIR = os.path.join(BASE, "guard-state")
SPEND_CACHE = os.path.join(BASE, "spend-cache.json")
CHARS_PER_TOKEN = 3.6
BYTES_PER_TOKEN = 3.6  # close enough for text files; binary files aren't read as text anyway

DEFAULTS = {
    "read_guard": "ask",            # ask | deny | off
    "max_read_tokens": 20000,       # larger reads without offset/limit get questioned
    "generated_min_tokens": 2000,   # generated files (lockfiles, bundles) above this get questioned
    "loop_guard": True,
    "loop_threshold": 3,            # same command + same output this many times
    "context_warn_tokens": 150000,  # alert when a session's context passes this, then every +50k
    "daily_usd": None,              # API-equivalent budget; None = off
    "monthly_usd": None,
    "budget_action": "warn",        # warn | block (block stops new prompts once the budget is used)
}


# ---------- plumbing ----------

def config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(BASE, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump({k: cfg[k] for k in DEFAULTS}, f, indent=2)


def _state_path(session_id):
    safe = "".join(ch for ch in (session_id or "none") if ch.isalnum() or ch in "-_")[:80]
    return os.path.join(STATE_DIR, safe + ".json")


def load_state(session_id):
    try:
        with open(_state_path(session_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(session_id, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(session_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _state_path(session_id))
    _prune_states()


def _prune_states(max_age=3 * 86400):
    """Drop state for sessions untouched for a few days (cheap: at most once an hour)."""
    marker = os.path.join(STATE_DIR, ".pruned")
    try:
        if time.time() - os.path.getmtime(marker) < 3600:
            return
    except OSError:
        pass
    for name in os.listdir(STATE_DIR):
        p = os.path.join(STATE_DIR, name)
        try:
            if name.endswith(".json") and time.time() - os.path.getmtime(p) > max_age:
                os.remove(p)
        except OSError:
            pass
    open(marker, "w").close()


def _fmt_tok(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(int(n))


# ---------- read guard ----------

def check_read(tool_input, cfg):
    """Return (decision, reason) for a Read, or None to stay out of the way."""
    if cfg["read_guard"] == "off" or not isinstance(tool_input, dict):
        return None
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None or tool_input.get("pages"):
        return None  # a targeted read is exactly what we want
    path = tool_input.get("file_path") or ""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    tokens = int(size / BYTES_PER_TOKEN)
    from .fixes import _generated  # imported lazily: only when a file is actually checked

    generated = _generated(path)
    name = os.path.basename(path)
    if generated and tokens >= cfg["generated_min_tokens"]:
        why = (f"burnlens: {name} is a generated file (~{_fmt_tok(tokens)} tokens). Reading it puts all of that into "
               f"context, re-sent on every later turn. Search it with Grep, or read only the lines you need (offset/limit).")
    elif tokens >= cfg["max_read_tokens"]:
        why = (f"burnlens: {name} is large (~{_fmt_tok(tokens)} tokens) and would be read in full. Read the relevant "
               f"part with offset/limit, or find it with Grep first.")
    else:
        return None
    return ("deny" if cfg["read_guard"] == "deny" else "ask"), why


def pre_tool(event, cfg):
    if event.get("tool_name") != "Read":
        return None
    verdict = check_read(event.get("tool_input"), cfg)
    if not verdict:
        return None
    decision, reason = verdict
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


# ---------- loop guard ----------

def _digest(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def post_tool(event, cfg):
    """Nudge Claude when a call keeps returning the same thing."""
    if not cfg["loop_guard"]:
        return None
    name, inp = event.get("tool_name"), event.get("tool_input") or {}
    if name not in ("Bash", "Read"):
        return None
    sid = event.get("session_id")
    state = load_state(sid)
    seen = state.setdefault("repeats", {})
    key = _digest([name, inp])
    out = _digest(event.get("tool_response"))
    entry = seen.get(key)
    if entry and entry["out"] == out:
        entry["n"] += 1
    else:
        entry = seen[key] = {"out": out, "n": 1}
    if len(seen) > 300:  # keep state small
        for k in list(seen)[:100]:
            seen.pop(k, None)
    save_state(sid, state)
    if entry["n"] < cfg["loop_threshold"] or entry.get("warned"):
        return None
    entry["warned"] = True
    save_state(sid, state)
    what = f"`{inp.get('command', '')[:80]}`" if name == "Bash" else os.path.basename(inp.get("file_path", ""))
    msg = (f"burnlens: {name} {what} has returned the same result {entry['n']} times in this session. Repeating it "
           f"won't change the outcome and adds the same output to context again. Change approach: check the "
           f"assumption behind it, look at a different file or log, or ask the user.")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}


# ---------- context alert ----------

def last_context(transcript_path, tail_bytes=262144):
    """(context tokens, model) of the latest main-thread API call, from the transcript's tail."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return None
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        if d.get("type") != "assistant" or d.get("isSidechain") or not isinstance(msg, dict):
            continue
        u = msg.get("usage") or {}
        if msg.get("model") == "<synthetic>":
            continue
        return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
            (u.get("cache_creation_input_tokens") or 0), msg.get("model")
    return None


def context_alert(event, cfg, state):
    limit = cfg["context_warn_tokens"]
    found = last_context(event.get("transcript_path")) if limit else None
    if not found:
        return None
    tokens, model = found
    step = 50000
    level = limit + ((tokens - limit) // step) * step if tokens >= limit else 0
    if not level or state.get("context_warned", 0) >= level:
        return None
    state["context_warned"] = level
    from .pricing import rates

    r = rates(model)
    per_turn = f" (about ${tokens * r[2] / 1e6:.2f} per turn from cache, ${tokens * r[0] * 1.25 / 1e6:.2f} if the cache has expired)" if r else ""
    return (f"burnlens: this session's context is {_fmt_tok(tokens)} tokens, re-sent on every turn{per_turn}. "
            f"For a new task, /compact or a fresh session will cut that.")


# ---------- budget ----------

def spend(now=None):
    """API-equivalent spend today and this month (local time), from transcripts.
    Per-file results are cached by (mtime, size), so only changed files are re-parsed."""
    from . import parser

    now = now or time.time()
    today, month = time.strftime("%Y-%m-%d", time.localtime(now)), time.strftime("%Y-%m", time.localtime(now))
    month_start = time.mktime(time.strptime(month + "-01", "%Y-%m-%d"))
    try:
        with open(SPEND_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    root = parser.default_root()
    fresh = {}
    for p in parser.glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime < month_start:
            continue  # nothing this month
        sig = [st.st_mtime, st.st_size]
        hit = cache.get(p)
        if hit and hit["sig"] == sig:
            fresh[p] = hit
            continue
        from collections import defaultdict

        sessions, calls, tools, inv, comp = {}, [], [], [], defaultdict(list)
        parser.parse_file(p, sessions, calls, tools, inv, comp, redact=True)
        days = defaultdict(float)
        for c in calls:
            if c.get("ts") and c.get("usd"):
                t = calendar.timegm(time.strptime(c["ts"][:19], "%Y-%m-%dT%H:%M:%S"))  # transcript times are UTC
                days[time.strftime("%Y-%m-%d", time.localtime(t))] += c["usd"]
        fresh[p] = {"sig": sig, "days": dict(days)}
    os.makedirs(BASE, exist_ok=True)
    with open(SPEND_CACHE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(fresh, f)
    os.replace(SPEND_CACHE + ".tmp", SPEND_CACHE)
    day_total = sum(v["days"].get(today, 0) for v in fresh.values())
    month_total = sum(u for v in fresh.values() for d, u in v["days"].items() if d.startswith(month))
    return {"today": day_total, "month": month_total}


def cached_spend():
    """Spend from the cache only (for the status line, which must be instant)."""
    try:
        with open(SPEND_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return None
    today, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    return {"today": sum(v["days"].get(today, 0) for v in cache.values()),
            "month": sum(u for v in cache.values() for d, u in v["days"].items() if d.startswith(month))}


def budget_check(cfg, state, spent=None):
    """(block_reason or None, message or None)."""
    limits = [(k, cfg[k + "_usd"]) for k in ("daily", "monthly") if cfg.get(k + "_usd")]
    if not limits:
        return None, None
    spent = spent or spend()
    for period, limit in limits:
        used = spent["today" if period == "daily" else "month"]
        label = "today" if period == "daily" else "this month"
        if used >= limit:
            text = (f"burnlens budget: {label}'s API-equivalent spend is ${used:,.2f}, over your {period} limit of "
                    f"${limit:,.2f}.")
            if cfg["budget_action"] == "block":
                return text + " New prompts are paused. Raise the limit or turn blocking off with /burnlens-guard.", None
            key = f"{period}_over_{time.strftime('%Y-%m-%d')}"
            if not state.get(key):
                state[key] = True
                return None, text
        elif used >= 0.8 * limit:
            key = f"{period}_80_{time.strftime('%Y-%m-%d' if period == 'daily' else '%Y-%m')}"
            if not state.get(key):
                state[key] = True
                return None, f"burnlens budget: ${used:,.2f} of your ${limit:,.2f} {period} limit used ({used / limit:.0%})."
    return None, None


def prompt(event, cfg):
    sid = event.get("session_id")
    state = load_state(sid)
    blocked, budget_msg = budget_check(cfg, state)
    ctx_msg = context_alert(event, cfg, state)
    save_state(sid, state)
    if blocked:
        return {"decision": "block", "reason": blocked}
    msgs = [m for m in (budget_msg, ctx_msg) if m]
    return {"systemMessage": "\n".join(msgs)} if msgs else None


# ---------- status line ----------

def statusline(event, cfg):
    parts = []
    cost = (event.get("cost") or {}).get("total_cost_usd")
    if cost is not None:
        parts.append(f"🔥 ${cost:,.2f}")
    cw = event.get("context_window") or {}
    used, total = cw.get("used_percentage"), cw.get("total_input_tokens")
    if total:
        parts.append(f"ctx {_fmt_tok(total)}" + (f" ({used:.0f}%)" if isinstance(used, (int, float)) else ""))
    cu = cw.get("current_usage") or {}
    inp = (cu.get("input_tokens") or 0) + (cu.get("cache_read_input_tokens") or 0) + (cu.get("cache_creation_input_tokens") or 0)
    if inp:
        parts.append(f"cache {(cu.get('cache_read_input_tokens') or 0) / inp:.0%}")
    spent = cached_spend()
    if spent:
        today = f"today ${spent['today']:,.2f}"
        if cfg.get("daily_usd"):
            limit = cfg["daily_usd"]
            today += f"/{limit:,.0f}" if limit >= 10 else f"/{limit:,.2f}"
        parts.append(today)
    return " · ".join(parts) or "🔥 burnlens"


STATUSLINE_SCRIPT = """#!/usr/bin/env bash
# burnlens status line. Stable path for settings.json; finds the newest burnlens plugin install,
# since the plugin's own folder changes with every update.
newest="$(ls -t ~/.claude/plugins/cache/*/burnlens/*/scripts/burnlens.sh ~/.claude/plugins/marketplaces/burnlens/scripts/burnlens.sh 2>/dev/null | head -n 1)"
for l in "$newest" {fallback}; do  # one path per word, so spaces in paths are safe
  [ -f "$l" ] && exec bash "$l" statusline
done
command -v burnlens >/dev/null 2>&1 && exec burnlens statusline
echo "🔥 burnlens"
"""


def install_statusline():
    """Write ~/.burnlens/statusline.sh and return the command to put in settings.json."""
    here = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts", "burnlens.sh"))
    fallback = json.dumps(here.replace("\\", "/")) if os.path.exists(here) else ""
    path = os.path.join(BASE, "statusline.sh")
    os.makedirs(BASE, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(STATUSLINE_SCRIPT.format(fallback=fallback))
    os.chmod(path, 0o755)
    return "bash ~/.burnlens/statusline.sh"


# ---------- entry ----------

HANDLERS = {"pre-tool": pre_tool, "post-tool": post_tool, "prompt": prompt}


def main(argv):
    if os.environ.get("BURNLENS_GUARD", "").lower() in ("off", "0", "false"):
        return 0
    which = argv[0] if argv else ""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    try:
        cfg = config()
        if which == "statusline":
            print(statusline(event, cfg))
            return 0
        handler = HANDLERS.get(which)
        out = handler(event, cfg) if handler else None
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - never break the user's session
        if os.environ.get("BURNLENS_DEBUG"):
            raise
    return 0
