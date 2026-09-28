"""Live guardrails, run as Claude Code hooks (see hooks/hooks.json).

  pre-tool     PreToolUse on Read: large or generated files read without a range
  post-tool    PostToolUse on Bash/Read: the same result coming back again and again
  prompt       UserPromptSubmit: context-size alert
  statusline   status line: session cost, context size, cache hit rate

Every entry point reads the hook's JSON from stdin, prints a JSON reply (or
nothing), and never raises: a guardrail bug must not break a session.
Settings: ~/.burnlens/guard.json. `burnlens guard off` (or BURNLENS_GUARD=off for one
session) turns every guardrail off; the status line keeps working since it only displays.
"""

import hashlib
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".burnlens")
CONFIG = os.path.join(BASE, "guard.json")
STATE_DIR = os.path.join(BASE, "guard-state")
EVENTS = os.path.join(BASE, "guard-events.jsonl")      # what each guardrail did, for the dashboard
SESSIONS = os.path.join(BASE, "guard-sessions.json")    # session id -> "on" | "off"
CHARS_PER_TOKEN = 3.6
BYTES_PER_TOKEN = 3.6  # close enough for text files; binary files aren't read as text anyway

DEFAULTS = {
    "enabled": True,                # master switch for every guardrail
    "read_guard": "ask",            # ask | deny | off
    "max_read_tokens": 20000,       # larger reads without offset/limit get questioned
    "generated_min_tokens": 2000,   # generated files (lockfiles, bundles) above this get questioned
    "loop_guard": True,
    "loop_threshold": 3,            # same command + same output this many times
    "context_warn_tokens": 150000,  # alert when a session's context passes this, then every +50k
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


# ---------- evidence for the on/off comparison ----------

def log_event(kind, session_id, **detail):
    try:
        os.makedirs(BASE, exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(detail, kind=kind, s=session_id,
                                    ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))) + "\n")
    except OSError:
        pass


def record_session(session_id, on):
    """Remember whether guardrails were on for a session ("mixed" if toggled mid-session)."""
    if not session_id:
        return
    try:
        with open(SESSIONS, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    tag = "on" if on else "off"
    old = seen.get(session_id)
    new = tag if old in (None, tag) else "mixed"
    if new == old:
        return
    seen[session_id] = new
    os.makedirs(BASE, exist_ok=True)
    with open(SESSIONS + ".tmp", "w", encoding="utf-8") as f:
        json.dump(seen, f)
    os.replace(SESSIONS + ".tmp", SESSIONS)


def load_events():
    try:
        with open(EVENTS, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def load_sessions():
    try:
        with open(SESSIONS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


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
    inp = event.get("tool_input") or {}
    try:
        tokens = int(os.path.getsize(inp.get("file_path", "")) / BYTES_PER_TOKEN)
    except OSError:
        tokens = 0
    log_event("read", event.get("session_id"), path=inp.get("file_path"), tokens=tokens, decision=decision)
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
    log_event("loop", sid, tool=name, repeats=entry["n"])
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
    log_event("context", event.get("session_id"), tokens=tokens)
    from .pricing import rates

    r = rates(model)
    per_turn = f" (about ${tokens * r[2] / 1e6:.2f} per turn from cache, ${tokens * r[0] * 1.25 / 1e6:.2f} if the cache has expired)" if r else ""
    return (f"burnlens: this session's context is {_fmt_tok(tokens)} tokens, re-sent on every turn{per_turn}. "
            f"For a new task, /compact or a fresh session will cut that.")


def prompt(event, cfg):
    sid = event.get("session_id")
    state = load_state(sid)
    msg = context_alert(event, cfg, state)
    if msg:
        save_state(sid, state)
        return {"systemMessage": msg}
    return None


# ---------- on/off comparison for the dashboard ----------

MIN_SESSIONS_EACH = 3


def report(data):
    """Guardrails on vs off, and what the guardrails did, from local records + transcripts."""
    from .pricing import rates

    tags = load_sessions()
    per = {}
    for c in data["calls"]:
        e = per.setdefault(c["s"], {"usd": 0.0, "tok": 0, "main": 0, "ctx": 0, "tools": 0})
        e["usd"] += c.get("usd") or 0
        e["tok"] += c["i"] + c["o"] + c["cr"] + c["cw"]
        if not c.get("a"):
            e["main"] += 1
            e["ctx"] += c["i"] + c["cr"] + c["cw"]
    for t in data["tools"]:
        if t["s"] in per:
            per[t["s"]]["tools"] += t.get("rt", 0)
    groups = {"on": [], "off": []}
    for sid, e in per.items():
        tag = tags.get(sid, "off")  # untagged = guardrails weren't running (off, or before install)
        if tag in groups and e["main"]:
            groups[tag].append(e)

    def summarize(rows):
        n = len(rows)
        if not n:
            return {"sessions": 0}
        calls = sum(r["main"] for r in rows) or 1
        return {"sessions": n, "usd_per_session": sum(r["usd"] for r in rows) / n,
                "tokens_per_session": sum(r["tok"] for r in rows) / n,
                "ctx_per_call": sum(r["ctx"] for r in rows) / calls,
                "tool_tokens_per_session": sum(r["tools"] for r in rows) / n}

    # Which questioned reads were prevented: no full read of that file followed in the session.
    by_session = {}
    for c in data["calls"]:
        if not c.get("a"):
            by_session.setdefault(c["s"], []).append(c)
    compactions = data.get("compactions") or {}
    reads = {"questioned": 0, "prevented": 0, "tokens_avoided": 0, "usd_avoided": 0.0}
    counts = {"loop": 0, "context": 0}
    for ev in load_events():
        if ev.get("kind") in counts:
            counts[ev["kind"]] += 1
            continue
        if ev.get("kind") != "read":
            continue
        reads["questioned"] += 1
        sid, ts, path = ev.get("s"), ev.get("ts", ""), ev.get("path")
        went_ahead = any(t["s"] == sid and t["n"] == "Read" and t.get("path") == path and not t.get("ranged")
                         and (t.get("ts") or "")[:19] >= ts[:19] for t in data["tools"])
        if went_ahead:
            continue
        reads["prevented"] += 1
        calls = by_session.get(sid, [])
        stop = min([x for x in compactions.get(sid, []) if x and x > ts] or ["\uffff"])
        later = sum(1 for c in calls if ts < (c.get("ts") or "") < stop)
        tok = ev.get("tokens") or 0
        reads["tokens_avoided"] += tok * (1 + later)
        r = rates(calls[0]["m"]) if calls else None
        if r:
            reads["usd_avoided"] += (tok * r[0] * 1.25 + tok * later * r[2]) / 1e6
    on, off = summarize(groups["on"]), summarize(groups["off"])
    return {"on": on, "off": off, "reads": reads, "loops": counts["loop"], "context_alerts": counts["context"],
            "enough": on["sessions"] >= MIN_SESSIONS_EACH and off["sessions"] >= MIN_SESSIONS_EACH,
            "min_sessions": MIN_SESSIONS_EACH, "enabled": config()["enabled"]}


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


def disabled_by_env():
    return os.environ.get("BURNLENS_GUARD", "").lower() in ("off", "0", "false")


def main(argv):
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
        on = cfg["enabled"] and not disabled_by_env()
        if which == "prompt":
            record_session(event.get("session_id"), on)  # tagged even when off, for the comparison
        if not on:
            return 0
        handler = HANDLERS.get(which)
        out = handler(event, cfg) if handler else None
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - never break the user's session
        if os.environ.get("BURNLENS_DEBUG"):
            raise
    return 0
