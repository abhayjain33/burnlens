"""Parse Claude Code session transcripts (~/.claude/projects/**/*.jsonl).

Each assistant API response is written as one JSONL record *per content
block*, all repeating the same `usage`, so calls are de-duplicated by
requestId / message.id before anything is summed.
"""

import glob
import json
import os
from collections import defaultdict

from .classify import classify_turn
from .pricing import cost

CHARS_PER_TOKEN = 3.6  # rough estimate for tool-result text; real usage fields are exact
IMAGE_TOKENS = 1600


DEFAULT_RETENTION_DAYS = 30  # Claude Code's cleanupPeriodDays default


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def default_root():
    return os.path.join(config_dir(), "projects")


def retention_days():
    """cleanupPeriodDays from user settings: how long Claude Code keeps transcripts."""
    try:
        with open(os.path.join(config_dir(), "settings.json")) as f:
            days = json.load(f).get("cleanupPeriodDays")
        return int(days) if days is not None else DEFAULT_RETENTION_DAYS
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_RETENTION_DAYS


def _est_tokens(content):
    if content is None:
        return 0
    if isinstance(content, str):
        return int(len(content) / CHARS_PER_TOKEN)
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "image":
                    total += IMAGE_TOKENS
                elif "text" in b:
                    total += int(len(b.get("text") or "") / CHARS_PER_TOKEN)
                else:
                    total += int(len(json.dumps(b)) / CHARS_PER_TOKEN)
        return total
    return int(len(json.dumps(content)) / CHARS_PER_TOKEN)


def split_tool(name):
    """mcp__server__tool -> ('server', 'tool'); built-ins -> (None, name)."""
    if name and name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
    return None, name


def _prompt_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        texts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(texts) if texts else None
    return None


def _agent_label(path):
    """Subagent transcripts live under <session>/subagents/; read a sibling meta if present."""
    if "/subagents/" not in path:
        return None
    for meta in (path[:-6] + ".meta.json", os.path.join(os.path.dirname(path), "meta.json")):
        try:
            with open(meta) as f:
                m = json.load(f)
            return m.get("agentType") or m.get("subagent_type") or "subagent"
        except (OSError, ValueError):
            pass
    return "subagent"


def parse_file(path, sessions, calls, tools, invocations, compactions, redact):
    agent_file = _agent_label(path)
    seen_calls = {}
    turn = None  # {'id', 'prompt', 'tool_uses', 'call_idx', 'tool_idx'}
    turns = []
    pending_tools = {}  # tool_use_id -> tool record index, until its result arrives
    tool_ids = {}  # tool_use_id -> tool record index, for content injected later (skills)

    def new_turn(prompt, ts):
        nonlocal turn
        turn = {"prompt": prompt or "", "tool_uses": [], "calls": [], "tools": [], "ts": ts}
        turns.append(turn)

    if agent_file:
        new_turn("", None)

    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            sid = d.get("sessionId")
            if not sid:
                continue
            s = sessions.setdefault(sid, {"title": None, "project": None, "first_prompt": None, "start": None, "end": None})
            if t == "custom-title" and d.get("customTitle"):
                s["title"] = d["customTitle"]
            elif t == "summary" and d.get("summary") and not s["title"]:
                s["title"] = d["summary"]
            if d.get("cwd") and not s["project"]:
                s["project"] = os.path.basename(d["cwd"].rstrip("/")) or d["cwd"]
            ts = d.get("timestamp")
            if ts:
                s["start"] = min(s["start"] or ts, ts)
                s["end"] = max(s["end"] or ts, ts)

            if t == "system" and d.get("subtype") == "compact_boundary":
                compactions[sid].append(ts)
                continue

            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            sidechain = bool(d.get("isSidechain")) or bool(agent_file)

            if t == "user":
                src = tool_ids.get(d.get("sourceToolUseID"))
                if d.get("isMeta") and src is not None:
                    # e.g. a Skill's instructions, injected as a meta message after the tool result
                    tools[src]["rt"] += _est_tokens(msg.get("content"))
                    continue
                prompt = None if d.get("isMeta") or sidechain else _prompt_text(msg)
                if prompt is not None:
                    if not s["first_prompt"]:
                        s["first_prompt"] = prompt.strip().splitlines()[0][:90] if prompt.strip() else None
                    new_turn(prompt, ts)
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            idx = pending_tools.pop(b.get("tool_use_id"), None)
                            if idx is not None:
                                tools[idx]["rt"] += _est_tokens(b.get("content"))
                                tools[idx]["err"] = bool(b.get("is_error"))
                                tools[idx]["rts"] = ts
                    tur = d.get("toolUseResult")
                    if isinstance(tur, dict) and (tur.get("totalTokens") or tur.get("usage")):
                        for inv in invocations:
                            if inv["tuid"] == d.get("sourceToolUseID") or inv["tuid"] in {
                                b.get("tool_use_id") for b in content if isinstance(b, dict)
                            }:
                                inv["tokens"] = tur.get("totalTokens") or 0
                                inv["toolCount"] = tur.get("totalToolUseCount")
                continue

            if t != "assistant":
                continue
            usage = msg.get("usage") or {}
            model = msg.get("model")
            if model == "<synthetic>" or not usage:
                continue
            key = d.get("requestId") or msg.get("id") or d.get("uuid")
            if turn is None:
                new_turn("", ts)

            if key not in seen_calls:
                cc = usage.get("cache_creation") or {}
                cw = usage.get("cache_creation_input_tokens") or 0
                cw1h = cc.get("ephemeral_1h_input_tokens") or 0
                cw5m = cc.get("ephemeral_5m_input_tokens", cw - cw1h) or 0
                rec = {
                    "ts": ts,
                    "s": sid,
                    "m": model,
                    "i": usage.get("input_tokens") or 0,
                    "o": usage.get("output_tokens") or 0,
                    "cr": usage.get("cache_read_input_tokens") or 0,
                    "cw": cw,
                    "th": (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
                    "a": agent_file or ("subagent" if sidechain else None),
                    "fast": usage.get("speed") == "fast",
                }
                rec["usd"] = cost(model, rec["i"], rec["o"], rec["cr"], cw5m, cw1h, rec["fast"])
                seen_calls[key] = len(calls)
                calls.append(rec)
                turn["calls"].append(len(calls) - 1)
            call_idx = seen_calls[key]

            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name") or "?"
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                server, short = split_tool(name)
                tr = {"ts": ts, "s": sid, "n": name, "srv": server, "t": short, "rt": 0, "ci": call_idx,
                      "a": calls[call_idx]["a"]}
                if name == "Skill":
                    tr["skill"] = inp.get("skill") or inp.get("command")
                if name in ("Agent", "Task"):
                    invocations.append({"tuid": b.get("id"), "s": sid, "ts": ts,
                                        "type": inp.get("subagent_type") or "general-purpose",
                                        "desc": None if redact else (inp.get("description") or "")[:60],
                                        "tokens": 0})
                tools.append(tr)
                pending_tools[b.get("id")] = len(tools) - 1
                tool_ids[b.get("id")] = len(tools) - 1
                turn["tools"].append(len(tools) - 1)
                turn["tool_uses"].append((name, inp))

    for tn in turns:
        cat = classify_turn(tn["tool_uses"], tn["prompt"])
        for ci in tn["calls"]:
            calls[ci]["c"] = cat
        for ti in tn["tools"]:
            tools[ti]["c"] = cat


def _carried(calls, tools, compactions):
    """Tokens a tool result costs *after* it lands: it is re-sent on every later
    main-thread call in the session until the next compaction."""
    by_session = defaultdict(list)
    for c in calls:
        if not c["a"]:
            by_session[c["s"]].append(c["ts"] or "")
    for v in by_session.values():
        v.sort()
    for tr in tools:
        tr["cx"] = 0
        if tr["a"] or not tr.get("rts") or not tr["rt"]:
            continue
        ts_list = by_session.get(tr["s"], [])
        stop = min([c for c in compactions.get(tr["s"], []) if c and c > tr["rts"]] or ["￿"])
        later = sum(1 for t in ts_list if tr["rts"] < t < stop)
        tr["cx"] = tr["rt"] * later


def load(root=None, redact=False):
    root = root or default_root()
    files = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
    sessions, calls, tools, invocations = {}, [], [], []
    compactions = defaultdict(list)
    for p in files:
        parse_file(p, sessions, calls, tools, invocations, compactions, redact)
    _carried(calls, tools, compactions)

    for sid, s in sessions.items():
        s["title"] = s["title"] or s["first_prompt"] or sid[:8]
        if redact:
            s["title"] = "session " + sid[:8]
        s.pop("first_prompt", None)
    used = {c["s"] for c in calls}
    sessions = {k: v for k, v in sessions.items() if k in used}
    for tr in tools:
        tr.pop("ci", None)
        tr.pop("rts", None)
    for inv in invocations:
        inv.pop("tuid", None)
    return {
        "source": root,
        "files": len(files),
        "demo": False,
        "retentionDays": retention_days(),
        "sessions": sessions,
        "calls": calls,
        "tools": tools,
        "agents": invocations,
        "compactions": {k: v for k, v in compactions.items() if k in used},
    }
