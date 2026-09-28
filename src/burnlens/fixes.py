"""Recommend concrete fixes for token waste, and measure them once applied.

Each detector looks at transcripts plus local Claude Code config and returns
recommendations shaped like:

  id          stable across runs, e.g. "reads:1a2b3c4d"
  kind        detector name
  title       one line
  detail      the evidence
  tokens_30d  projected tokens a month the fix would save (None if unknown)
  usd_30d     the same in API-equivalent dollars (None if unknown)
  confidence  "measured"   cost that already happened and the fix removes
              "estimated"  depends on an assumption (stated in detail)
              "unquantified"
  action      what to change: settings_deny | claude_md | claude_task | command | settings_json
  metric      how `savings` re-measures it after the fix

Applying is left to Claude (the /burnlens-fix command) so every change goes
through Claude Code's own approval prompts. `mark_applied` records a
baseline; `savings` compares usage since then against it.
"""

import datetime as dt
import glob
import hashlib
import json
import os
import re
from collections import defaultdict

from .pricing import rates

HOME = os.path.expanduser("~")
LEDGER = os.path.join(HOME, ".burnlens", "fixes.json")
CHARS_PER_TOKEN = 3.6

GENERATED_FILES = re.compile(
    r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Pipfile\.lock|"
    r"Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum|[^/]+\.min\.(js|css)|[^/]+\.map|[^/]+\.log)$")
GENERATED_DIRS = ("node_modules", "dist", "build", ".next", "coverage", "vendor", "target", "__snapshots__")

MIN_FILE_TOKENS = 5000      # generated-file reads worth blocking, per project per window
MIN_CMD_AVG = 3000          # average result tokens for a noisy command
MIN_TOOL_AVG = 8000         # average result tokens for a heavy MCP tool
MIN_INSTRUCTION = 1500      # CLAUDE.md size worth trimming
TARGET_INSTRUCTION = 800
OUTPUT_REDUCTION = 0.6      # assumed cut in command output after the CLAUDE.md hint
MCP_REDUCTION = 0.5
EXPENSIVE = ("claude-opus", "claude-fable", "claude-mythos")
CHEAPER_MODEL = "claude-sonnet-5"
MIN_SUBAGENT_USD_30D = 1.0  # projected monthly saving worth suggesting a model switch


# ---------- small helpers ----------

def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _id(kind, *parts):
    return kind + ":" + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:8]


def _norm(path):
    return (path or "").replace("\\", "/")


def _rel(path, root):
    path, root = _norm(path), _norm(root).rstrip("/")
    return path[len(root) + 1:] if root and path.startswith(root + "/") else path


def _file_tokens(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return int(len(f.read()) / CHARS_PER_TOKEN)
    except OSError:
        return 0


def _generated(path):
    p = _norm(path)
    if GENERATED_FILES.search(p):
        return os.path.basename(p), None
    parts = p.split("/")
    for d in GENERATED_DIRS:
        if d in parts[:-1]:
            return None, d
    return None


class Context:
    """Transcript data narrowed to a time window, with lookups the detectors share."""

    def __init__(self, data, start=None, end=None):
        self.data = data
        self.start, self.end = start, end
        ok = lambda r: (not start or (r.get("ts") or "") >= start) and (not end or (r.get("ts") or "") < end)  # noqa: E731
        self.calls = [c for c in data["calls"] if ok(c)]
        self.tools = [t for t in data["tools"] if ok(t)]
        self.sessions = data["sessions"]
        model_tokens = defaultdict(lambda: defaultdict(int))
        for c in self.calls:
            model_tokens[c["s"]][c["m"]] += c["i"] + c["o"] + c["cr"] + c["cw"]
        self.session_model = {s: max(m, key=m.get) for s, m in model_tokens.items()}
        self.session_ids = {c["s"] for c in self.calls if not c.get("a")}
        stamps = sorted(c["ts"] for c in self.calls if c.get("ts"))
        span = (_ts(stamps[-1]) - _ts(stamps[0])).days + 1 if stamps else 1
        self.observed_days = max(7, span)  # damp projections from very short histories

    def cwd(self, sid):
        return (self.sessions.get(sid) or {}).get("cwd")

    def scale(self, x):
        return x * 30 / self.observed_days

    def tool_usd(self, t):
        """Cost of a tool result: written to cache once, then re-read while carried."""
        r = rates(self.session_model.get(t["s"]))
        if not r:
            return 0.0
        return (t.get("rt", 0) * r[0] * 1.25 + t.get("cx", 0) * r[2]) / 1e6

    def sessions_in(self, scope):
        """Main-thread sessions whose working directory is inside scope (None = all)."""
        root = _norm(scope).rstrip("/") if scope else None
        return {s for s in self.session_ids if not root or _norm(self.cwd(s) or "").startswith(root)}


# ---------- detectors ----------

def _large_file_reads(ctx):
    by_cwd = defaultdict(lambda: defaultdict(lambda: {"n": 0, "tok": 0, "usd": 0.0, "paths": set()}))
    for t in ctx.tools:
        if t["n"] != "Read" or not t.get("path") or t.get("a"):
            continue
        g = _generated(t["path"])
        cwd = ctx.cwd(t["s"])
        if not g or not cwd:
            continue
        e = by_cwd[cwd][g]
        e["n"] += 1
        e["tok"] += t.get("rt", 0) + t.get("cx", 0)
        e["usd"] += ctx.tool_usd(t)
        e["paths"].add(_rel(t["path"], cwd))
    out = []
    for cwd, groups in by_cwd.items():
        hits = {g: e for g, e in groups.items() if e["tok"] >= MIN_FILE_TOKENS}
        if not hits:
            continue
        rules, lines, match = [], [], []
        for (base, d), e in sorted(hits.items(), key=lambda kv: -kv[1]["tok"]):
            rules.append(f"Read(**/{base})" if base else f"Read(**/{d}/**)")
            match.append({"base": base} if base else {"dir": d})
            lines.append(f"{', '.join(sorted(e['paths'])[:3])}: {e['n']} reads, {e['tok']:,} tokens incl. re-sends")
        tok, usd = sum(e["tok"] for e in hits.values()), sum(e["usd"] for e in hits.values())
        out.append({
            "id": _id("reads", cwd, *sorted(rules)), "kind": "large_file_reads",
            "title": f"Stop reading generated files in {os.path.basename(cwd.rstrip('/'))}",
            "detail": "Generated files were read into context and re-sent on every later turn:\n  " + "\n  ".join(lines),
            "tokens_30d": int(ctx.scale(tok)), "usd_30d": round(ctx.scale(usd), 2), "confidence": "measured",
            "scope": cwd,
            "action": {"type": "settings_deny", "file": os.path.join(cwd, ".claude", "settings.local.json"), "rules": rules,
                       "note": "Personal settings for this project. Move the rules to .claude/settings.json to apply them for the whole team."},
            "metric": {"type": "reads", "scope": cwd, "match": match},
        })
    return out


def _noisy_commands(ctx):
    by_cwd = defaultdict(lambda: defaultdict(lambda: {"n": 0, "rt": 0, "tok": 0, "usd": 0.0}))
    for t in ctx.tools:
        if t["n"] != "Bash" or not t.get("cmd") or t.get("a") or not ctx.cwd(t["s"]):
            continue
        e = by_cwd[ctx.cwd(t["s"])][t["cmd"]]
        e["n"] += 1
        e["rt"] += t.get("rt", 0)
        e["tok"] += t.get("rt", 0) + t.get("cx", 0)
        e["usd"] += ctx.tool_usd(t)
    out = []
    for cwd, cmds in by_cwd.items():
        hits = {c: e for c, e in cmds.items() if e["n"] >= 3 and e["rt"] / e["n"] >= MIN_CMD_AVG}
        if not hits:
            continue
        ranked = sorted(hits.items(), key=lambda kv: -kv[1]["tok"])
        text = "\n".join(
            f"- When running `{c}`, keep output short: use its quiet or summary flags, or pipe through `tail -n 60`, "
            f"and show full output only for failures." for c, _ in ranked)
        tok, usd = sum(e["tok"] for e in hits.values()), sum(e["usd"] for e in hits.values())
        out.append({
            "id": _id("cmds", cwd, *sorted(hits)), "kind": "noisy_commands",
            "title": f"Trim noisy command output in {os.path.basename(cwd.rstrip('/'))}",
            "detail": "Commands whose output floods the context:\n  " + "\n  ".join(
                f"`{c}`: {e['n']} runs, avg {e['rt'] // e['n']:,} tokens per run" for c, e in ranked)
                + f"\nSavings assume the output shrinks by {int(OUTPUT_REDUCTION * 100)}%.",
            "tokens_30d": int(ctx.scale(tok * OUTPUT_REDUCTION)), "usd_30d": round(ctx.scale(usd * OUTPUT_REDUCTION), 2),
            "confidence": "estimated", "scope": cwd,
            "action": {"type": "claude_md", "file": os.path.join(cwd, "CLAUDE.md"), "text": text},
            "metric": {"type": "tool_output", "scope": cwd, "tool": "Bash", "cmds": sorted(hits)},
        })
    return out


def _heavy_mcp_tools(ctx):
    agg = defaultdict(lambda: {"n": 0, "rt": 0, "tok": 0, "usd": 0.0})
    for t in ctx.tools:
        if t.get("srv"):
            e = agg[t["n"]]
            e["n"] += 1
            e["rt"] += t.get("rt", 0)
            e["tok"] += t.get("rt", 0) + t.get("cx", 0)
            e["usd"] += ctx.tool_usd(t)
    out = []
    for name, e in sorted(agg.items(), key=lambda kv: -kv[1]["tok"]):
        if e["n"] < 2 or e["rt"] / e["n"] < MIN_TOOL_AVG:
            continue
        avg = e["rt"] // e["n"]
        out.append({
            "id": _id("mcp", name), "kind": "heavy_mcp_tool",
            "title": f"Rein in {name.split('__', 2)[-1]} ({name.split('__')[1]}): ~{avg:,} tokens per call",
            "detail": f"{e['n']} calls, {e['tok']:,} tokens incl. re-sends. Large results stay in context for every later turn.\n"
                      f"Savings assume calls return {int(MCP_REDUCTION * 100)}% less once Claude prefers narrower queries.",
            "tokens_30d": int(ctx.scale(e["tok"] * MCP_REDUCTION)), "usd_30d": round(ctx.scale(e["usd"] * MCP_REDUCTION), 2),
            "confidence": "estimated", "scope": None,
            "action": {"type": "claude_md", "file": os.path.join(HOME, ".claude", "CLAUDE.md"),
                       "text": f"- `{name}` returns ~{avg:,} tokens per call. Prefer narrower alternatives (filters, limits, "
                               f"pagination, targeted queries, screenshots instead of full snapshots) and avoid repeating it."},
            "metric": {"type": "tool_output", "scope": None, "tool": name},
        })
    return out


def instruction_files(ctx):
    """CLAUDE.md-style files Claude Code loads for the projects in these transcripts."""
    found = {}
    global_md = os.path.join(HOME, ".claude", "CLAUDE.md")
    if os.path.exists(global_md):
        found[global_md] = None
    for cwd in {ctx.cwd(s) for s in ctx.session_ids} - {None}:
        d = cwd
        while True:  # Claude Code loads CLAUDE.md from the working directory upwards
            for name in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md")):
                p = os.path.join(d, name)
                if os.path.exists(p):
                    found.setdefault(p, d)
            parent = os.path.dirname(d.rstrip("/\\"))
            if not parent or parent == d or _norm(d) == _norm(HOME) or len(parent) < len(HOME):
                break
            d = parent
    return found


def _big_instructions(ctx):
    out = []
    for path, scope in instruction_files(ctx).items():
        size = _file_tokens(path)
        if size < MIN_INSTRUCTION:
            continue
        sessions = ctx.sessions_in(scope)
        calls = [c for c in ctx.calls if c["s"] in sessions and not c.get("a")]
        cut = size - TARGET_INSTRUCTION
        usd = sum(cut * (rates(c["m"]) or (0, 0, 0))[2] for c in calls) / 1e6
        out.append({
            "id": _id("instr", path), "kind": "large_instructions",
            "title": f"Trim {path.replace(HOME, '~')} (~{size:,} tokens on every request)",
            "detail": f"Loaded into {len(calls):,} API calls in this period. Cutting it to ~{TARGET_INSTRUCTION} tokens "
                      f"saves ~{cut:,} tokens per call (mostly cache reads, so the dollar figure is small but it also frees context).",
            "tokens_30d": int(ctx.scale(cut * len(calls))), "usd_30d": round(ctx.scale(usd), 2),
            "confidence": "estimated", "scope": scope,
            "action": {"type": "claude_task", "file": path, "prompt": (
                f"Trim {path} (currently ~{size:,} tokens, sent with every request). Remove content that is outdated, "
                f"duplicated, generic advice Claude already follows, or long examples; keep project-specific commands, "
                f"conventions and gotchas. Aim for under ~{TARGET_INSTRUCTION} tokens. Show the proposed version as a diff "
                f"and wait for approval before saving.")},
            "metric": {"type": "instructions", "file": path, "scope": scope, "tokens_before": size},
        })
    return out


def _mcp_configs(ctx):
    """(name, scope_dir_or_None, how_to_remove) for configured MCP servers."""
    servers = []
    try:
        with open(os.path.join(HOME, ".claude.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    for name in cfg.get("mcpServers") or {}:
        servers.append((name, None, {"type": "command", "command": f"claude mcp remove {name} -s user"}))
    for proj, p in (cfg.get("projects") or {}).items():
        for name in (p or {}).get("mcpServers") or {}:
            servers.append((name, proj, {"type": "command", "command": f"cd {json.dumps(proj)} && claude mcp remove {name} -s local"}))
    for cwd in {ctx.cwd(s) for s in ctx.session_ids} - {None}:
        try:
            with open(os.path.join(cwd, ".mcp.json"), encoding="utf-8") as f:
                names = (json.load(f).get("mcpServers") or {}).keys()
        except (OSError, ValueError, AttributeError):
            continue
        disabled = set(((cfg.get("projects") or {}).get(cwd) or {}).get("disabledMcpjsonServers") or [])
        for name in set(names) - disabled:
            servers.append((name, cwd, {"type": "settings_json", "file": os.path.join(cwd, ".claude", "settings.local.json"),
                                        "append": {"disabledMcpjsonServers": [name]}}))
    try:
        with open(os.path.join(HOME, ".claude", "settings.json"), encoding="utf-8") as f:
            enabled = [p for p, on in (json.load(f).get("enabledPlugins") or {}).items() if on]
    except (OSError, ValueError, AttributeError):
        enabled = []
    for plugin in enabled:
        pname, _, market = plugin.partition("@")
        for mcp in glob.glob(os.path.join(HOME, ".claude", "plugins", "cache", market, pname, "*", ".mcp.json")):
            try:
                with open(mcp, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            for name in (d.get("mcpServers") or d).keys():
                servers.append((name, None, {"type": "command", "command": f"/plugin disable {plugin}",
                                             "note": "Disables the whole plugin, not only its MCP server."}))
            break
    return servers


def _unused_mcp_servers(ctx):
    used = {t["srv"] for t in ctx.tools if t.get("srv")}
    out, seen = [], set()
    for name, scope, action in _mcp_configs(ctx):
        key = (name, scope)
        sessions = ctx.sessions_in(scope)
        if key in seen or len(sessions) < 5:
            continue
        seen.add(key)
        if any(name == u or name in u for u in used):  # plugin servers get prefixed names
            continue
        out.append({
            "id": _id("mcpsrv", name, scope), "kind": "unused_mcp_server",
            "title": f"Remove unused MCP server '{name}'" + (f" in {os.path.basename(scope.rstrip('/'))}" if scope else ""),
            "detail": f"Not called once in {len(sessions)} sessions. Each enabled server adds its tool list to requests "
                      f"(less when Claude Code defers tool definitions), and a server that fails to start slows every session.",
            "tokens_30d": None, "usd_30d": None, "confidence": "unquantified", "scope": scope,
            "action": action, "metric": {"type": "mcp_server", "name": name},
        })
    return out


def _subagent_models(ctx):
    agg = defaultdict(lambda: {"tok": 0, "usd": 0.0, "cheap": 0.0, "runs": set(), "model": None})
    cheap = rates(CHEAPER_MODEL)
    for c in ctx.calls:
        if not c.get("a") or not (c.get("m") or "").startswith(EXPENSIVE) or c.get("usd") is None:
            continue
        e = agg[c["a"]]
        e["tok"] += c["i"] + c["o"] + c["cr"] + c["cw"]
        e["usd"] += c["usd"]
        e["cheap"] += (c["i"] * cheap[0] + c["o"] * cheap[1] + c["cr"] * cheap[2] + c["cw"] * cheap[0] * 1.25) / 1e6
        e["runs"].add(c["s"])
        e["model"] = c["m"]
    out = []
    for agent, e in agg.items():
        saving = e["usd"] - e["cheap"]
        if ctx.scale(saving) < MIN_SUBAGENT_USD_30D:
            continue
        files = [p for p in [os.path.join(HOME, ".claude", "agents", agent + ".md")] +
                 [os.path.join(cwd, ".claude", "agents", agent + ".md") for cwd in {ctx.cwd(s) for s in e["runs"]} - {None}]
                 if os.path.exists(p)]
        if files:
            action = {"type": "claude_task", "file": files[0], "prompt": (
                f"In {files[0]}, set `model: sonnet` in the YAML frontmatter (add the field if missing). "
                f"Show the diff and wait for approval.")}
        else:
            action = {"type": "claude_task", "prompt": (
                f"'{agent}' is a built-in subagent currently running on {e['model']}. Explain the options for running this "
                f"kind of work on a cheaper model (for example a custom agent in ~/.claude/agents/ with `model: sonnet`), "
                f"and create one only if I approve.")}
        out.append({
            "id": _id("agentmodel", agent), "kind": "subagent_model",
            "title": f"Run '{agent}' subagents on Sonnet instead of {e['model'].replace('claude-', '')}",
            "detail": f"{e['tok']:,} tokens across {len(e['runs'])} sessions cost ${e['usd']:.2f}; the same tokens on "
                      f"{CHEAPER_MODEL} would cost ${e['cheap']:.2f}. Assumes Sonnet does the job in a similar number of tokens.",
            "tokens_30d": None, "usd_30d": round(ctx.scale(saving), 2), "confidence": "estimated", "scope": None,
            "action": action, "metric": {"type": "subagent_model", "agent": agent},
        })
    return out


DETECTORS = [_large_file_reads, _noisy_commands, _heavy_mcp_tools, _big_instructions, _unused_mcp_servers, _subagent_models]


# ---------- ledger ----------

def load_ledger():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"applied": {}, "dismissed": {}}


def save_ledger(ledger):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, LEDGER)


TRANSCRIPT_ONLY = [_large_file_reads, _noisy_commands, _heavy_mcp_tools, _subagent_models]


def recommend(data, days=30, local=True):
    """local=False skips detectors that read this machine's config and the ledger (demo data)."""
    start = (_now() - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z") if days else None
    ctx = Context(data, start)
    ledger = load_ledger() if local else {"applied": {}, "dismissed": {}}
    recs = [r for d in (DETECTORS if local else TRANSCRIPT_ONLY) for r in d(ctx)]
    recs = [r for r in recs if r["id"] not in ledger["applied"] and r["id"] not in ledger["dismissed"]]
    recs.sort(key=lambda r: (r["usd_30d"] is None, -(r["usd_30d"] or 0), -(r["tokens_30d"] or 0)))
    return recs


def _metric(ctx, m):
    """Usage attributable to a fix in ctx's window, normalized so before/after compare."""
    if m["type"] == "reads":
        sessions = ctx.sessions_in(m["scope"])
        tok = usd = 0
        for t in ctx.tools:
            g = _generated(t.get("path")) if t["n"] == "Read" and t.get("path") and t["s"] in sessions else None
            if g and ({"base": g[0]} if g[0] else {"dir": g[1]}) in m["match"]:
                tok += t.get("rt", 0) + t.get("cx", 0)
                usd += ctx.tool_usd(t)
        return {"units": len(sessions), "unit": "session", "tok": tok, "usd": usd}
    if m["type"] == "tool_output":
        sessions = ctx.sessions_in(m.get("scope"))
        rows = [t for t in ctx.tools if t["n"] == m["tool"] and t["s"] in sessions and not t.get("a")
                and (not m.get("cmds") or t.get("cmd") in m["cmds"])]
        return {"units": len(rows), "unit": "call", "tok": sum(t.get("rt", 0) + t.get("cx", 0) for t in rows),
                "usd": sum(ctx.tool_usd(t) for t in rows)}
    if m["type"] == "instructions":
        sessions = ctx.sessions_in(m["scope"])
        calls = [c for c in ctx.calls if c["s"] in sessions and not c.get("a")]
        size = _file_tokens(m["file"])
        return {"units": len(calls), "unit": "call", "size": size,
                "cr_price": (sum((rates(c["m"]) or (0, 0, 0))[2] for c in calls) / len(calls)) if calls else 0}
    if m["type"] == "subagent_model":
        rows = [c for c in ctx.calls if c.get("a") == m["agent"]]
        tok = sum(c["i"] + c["o"] + c["cr"] + c["cw"] for c in rows)
        return {"units": tok, "unit": "token", "tok": tok, "usd": sum(c.get("usd") or 0 for c in rows)}
    return {"units": 0, "unit": None}


def mark_applied(data, rec, days=30):
    now = _now()
    start = (now - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")
    ledger = load_ledger()
    ledger["applied"][rec["id"]] = dict(rec, applied_at=now.isoformat().replace("+00:00", "Z"),
                                        baseline=_metric(Context(data, start), rec["metric"]))
    ledger["dismissed"].pop(rec["id"], None)
    save_ledger(ledger)


def dismiss(rec_id):
    ledger = load_ledger()
    ledger["dismissed"][rec_id] = _now().isoformat().replace("+00:00", "Z")
    save_ledger(ledger)


def savings(data, min_units=3):
    """Before/after for every applied fix. Savings are only claimed once there's
    enough usage since the fix to compare (status 'measuring' until then)."""
    out = []
    for rid, fx in load_ledger()["applied"].items():
        m, before = fx["metric"], fx.get("baseline") or {}
        after = _metric(Context(data, fx["applied_at"]), m)
        row = {"id": rid, "title": fx["title"], "kind": fx["kind"], "applied_at": fx["applied_at"],
               "expected_usd_30d": fx.get("usd_30d"), "status": "measuring", "saved_tokens": None, "saved_usd": None,
               "units_after": after.get("units", 0), "unit": after.get("unit")}
        if m["type"] == "mcp_server":
            row["status"] = "applied (not measurable)"
        elif m["type"] == "instructions":
            cut = max(0, m["tokens_before"] - after["size"])
            row["detail"] = f"{m['tokens_before']:,} → {after['size']:,} tokens per call"
            if after["units"] >= min_units:
                row.update(status="measured", saved_tokens=cut * after["units"],
                           saved_usd=round(cut * after["units"] * after["cr_price"] / 1e6, 4))
        elif after.get("units", 0) >= min_units and before.get("units"):
            rate_tok = before["tok"] / before["units"] - after["tok"] / after["units"]
            rate_usd = before["usd"] / before["units"] - after["usd"] / after["units"]
            row.update(status="measured", saved_usd=round(rate_usd * after["units"], 4),
                       saved_tokens=None if m["type"] == "subagent_model" else int(rate_tok * after["units"]))
            row["detail"] = (f"{before['tok'] / before['units']:,.0f} → {after['tok'] / after['units']:,.0f} tokens per {after['unit']}")
        out.append(row)
    return out
