"""burnlens — local token-usage dashboard for Claude Code."""

import argparse
import json
import os
import pathlib
import sys
import webbrowser
from collections import Counter, defaultdict

from . import __version__, demo, fixes, parser, sync


def template_text():
    # importlib.resources.files needs 3.9; the file sits next to this module anyway
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def render(data, out):
    template = template_text()
    # Escape "</" so transcript text can never close the <script> tag.
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(template.replace("/*__DATA__*/null", payload))
    return out


def _tok(n):
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def summary(data):
    """Plain-text digest, so the /burnlens:burnlens command can answer in chat too."""
    calls, tools = data["calls"], data["tools"]
    tok = sum(c["i"] + c["o"] + c["cr"] + c["cw"] for c in calls)
    usd = sum(c["usd"] or 0 for c in calls)
    inp = sum(c["i"] + c["cr"] + c["cw"] for c in calls)
    hit = sum(c["cr"] for c in calls) / inp if inp else 0
    lines = [
        f"{len(data['sessions'])} sessions · {len(calls)} API calls · {_tok(tok)} tokens · "
        f"~${usd:,.2f} API-equivalent · cache hit {hit:.0%}",
    ]
    cats = Counter()
    for c in calls:
        cats[c.get("c") or "unknown"] += c["usd"] or 0
    if usd:
        lines.append("Work type (cost): " + ", ".join(f"{k} {v / usd:.0%}" for k, v in cats.most_common(6)))
    carried = defaultdict(lambda: [0, 0])
    for t in tools:
        carried[t["n"]][0] += t.get("cx", 0)
        carried[t["n"]][1] += 1
    top = sorted(carried.items(), key=lambda kv: -kv[1][0])[:5]
    if top:
        lines.append("Top tools by carried context: " + ", ".join(f"{n} ({_tok(cx)}, {k} calls)" for n, (cx, k) in top))
    servers = defaultdict(int)
    for t in tools:
        if t.get("srv"):
            servers[t["srv"]] += t.get("cx", 0)
    if servers:
        lines.append("MCP servers by carried context: " + ", ".join(
            f"{s} ({_tok(v)})" for s, v in sorted(servers.items(), key=lambda kv: -kv[1])[:5]))
    if not data.get("demo"):
        stamps = sorted(c["ts"] for c in calls if c.get("ts"))
        if stamps and (len(data["sessions"]) < 10 or stamps[-1][:10] == stamps[0][:10]):
            ret = data.get("retentionDays", 30)
            lines.append(f"Note: limited history ({len(data['sessions'])} sessions since {stamps[0][:10]}); "
                         f"Claude Code keeps transcripts for {ret} days (cleanupPeriodDays).")
    return "\n".join(lines)


def sync_main(argv):
    ap = argparse.ArgumentParser(prog="burnlens sync", description="Push usage to your org's burnlens server.")
    ap.add_argument("--full", action="store_true", help="re-send everything, not just changed transcripts")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--background", action="store_true", help="detach and return immediately (used by the hook)")
    ap.add_argument("--root", help="transcripts dir")
    args = ap.parse_args(argv)
    if args.background:
        sync.run_in_background()
        return 0
    return sync.run(full=args.full, quiet=args.quiet, root=args.root)


def connect_main(argv):
    ap = argparse.ArgumentParser(prog="burnlens connect", description="Save org server settings to ~/.burnlens/config.json.")
    ap.add_argument("--server", required=True, help="e.g. https://burnlens.internal.example.com")
    ap.add_argument("--token", required=True, help="ingest token from your burnlens admin")
    ap.add_argument("--team", help="your team name (an admin can override it)")
    ap.add_argument("--user", help="identity to report (default: git user.email)")
    args = ap.parse_args(argv)
    cfg = sync.connect(args.server, args.token, args.team, args.user)
    print(f"Saved. Reporting as {sync.identity(cfg)['email']} to {cfg['server']}. Running first sync...")
    return sync.run(full=True)


def _fmt_rec(i, r):
    if r.get("extra_usd_30d") is not None:
        money = f"costs ~${r['extra_usd_30d']:,.2f}/mo more, for better results"
    else:
        money = f"~${r['usd_30d']:,.2f}/mo" if r["usd_30d"] is not None else "saving not quantifiable"
    toks = f", ~{_tok(r['tokens_30d'])} tokens/mo" if r.get("tokens_30d") else ""
    lines = [f"{i}. [{r['id']}] {r['title']}", f"   {money}{toks} ({r['confidence']})"]
    lines += ["   " + ln for ln in r["detail"].splitlines()]
    a = r["action"]
    how = {"settings_deny": lambda: f"add permissions.deny {a['rules']} to {a['file']}",
           "settings_json": lambda: f"merge {a['append']} into {a['file']}",
           "claude_md": lambda: f"append to {a['file']}:\n      " + a["text"].replace("\n", "\n      "),
           "claude_task": lambda: "ask Claude: " + a["prompt"],
           "command": lambda: "run: " + a["command"]}[a["type"]]()
    lines.append("   Fix: " + how)
    return "\n".join(lines)


def fixes_main(argv):
    ap = argparse.ArgumentParser(prog="burnlens fixes", description="Recommend fixes for token waste.")
    ap.add_argument("action", nargs="?", choices=["list", "applied", "dismiss"], default="list")
    ap.add_argument("id", nargs="?", help="recommendation id, for applied / dismiss")
    ap.add_argument("--days", type=int, default=30, help="history window to analyse (default 30)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", help="transcripts dir")
    ap.add_argument("--demo", action="store_true", help="recommendations for synthetic data (nothing is recorded)")
    args = ap.parse_args(argv)
    if args.demo:
        if args.action != "list":
            print("--demo only lists example recommendations; nothing is applied or recorded.")
            return 1
        recs = fixes.recommend(demo.generate(), args.days, local=False)
        print(json.dumps(recs, indent=2) if args.json else "\n\n".join(_fmt_rec(i + 1, r) for i, r in enumerate(recs[:8])))
        return 0
    data = parser.load(args.root)
    if args.action == "dismiss":
        fixes.dismiss(args.id)
        print(f"Dismissed {args.id}; it won't be recommended again.")
        return 0
    recs = fixes.recommend(data, args.days)
    if args.action == "applied":
        rec = next((r for r in recs if r["id"] == args.id), None)
        if not rec:
            print(f"No open recommendation with id {args.id} (already applied, dismissed, or no longer detected).")
            return 1
        fixes.mark_applied(data, rec, args.days)
        print(f"Recorded {args.id} as applied with a baseline from the last {args.days} days. "
              "`burnlens savings` will measure it once there is new usage.")
        return 0
    if args.json:
        print(json.dumps(recs, indent=2))
        return 0
    if not recs:
        print("No fixes to recommend right now. Nice.")
        return 0
    total = sum(r["usd_30d"] or 0 for r in recs)
    print(f"{len(recs)} recommended fixes, ~${total:,.2f}/month API-equivalent (projected from the last {args.days} days):\n")
    print("\n\n".join(_fmt_rec(i + 1, r) for i, r in enumerate(recs)))
    return 0


def savings_main(argv):
    ap = argparse.ArgumentParser(prog="burnlens savings", description="Measured effect of applied fixes.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", help="transcripts dir")
    args = ap.parse_args(argv)
    rows = fixes.savings(parser.load(args.root))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No fixes applied yet. Run `burnlens fixes` (or /burnlens:burnlens-fix in Claude Code).")
        return 0
    for r in rows:
        head = f"- {r['title']} (applied {r['applied_at'][:10]}): {r['status']}"
        if r["status"] == "measured":
            head += f", saved ~${r['saved_usd']:,.2f}" + (f" / {_tok(r['saved_tokens'])} tokens" if r.get("saved_tokens") else "")
        elif r["status"] == "measuring":
            head += f" ({r['units_after']} {r['unit'] or 'unit'}s of new usage so far)"
        print(head + (f"\n    {r['detail']}" if r.get("detail") else ""))
    return 0


def guard_main(argv):
    from . import guard

    if argv[:1] in (["pre-tool"], ["post-tool"], ["prompt"]):
        return guard.main(argv)
    ap = argparse.ArgumentParser(prog="burnlens guard", description="Live guardrails: turn on/off, show or change settings.")
    ap.add_argument("action", nargs="?", choices=["status", "on", "off", "set", "reset", "install-statusline"], default="status")
    ap.add_argument("key", nargs="?")
    ap.add_argument("value", nargs="?")
    args = ap.parse_args(argv)
    cfg = guard.config()
    if args.action == "install-statusline":
        cmd = guard.install_statusline()
        print(f"Wrote {os.path.join(guard.BASE, 'statusline.sh')}.")
        print('Add to ~/.claude/settings.json: "statusLine": {"type": "command", "command": "' + cmd + '"}')
        return 0
    if args.action in ("on", "off"):
        cfg["enabled"] = args.action == "on"
        guard.save_config(cfg)
        print("Guardrails are now " + ("ON." if cfg["enabled"] else "OFF. The status line, if installed, keeps working."))
        return 0
    if args.action == "reset":
        guard.save_config(dict(guard.DEFAULTS))
        print("Guardrail settings reset to defaults (guardrails on).")
        return 0
    if args.action == "set":
        if args.key not in guard.DEFAULTS or args.key == "enabled":
            print(f"Unknown setting {args.key!r}. Settings: {', '.join(k for k in guard.DEFAULTS if k != 'enabled')} "
                  "(use `burnlens guard on|off` for the master switch)")
            return 1
        default, v = guard.DEFAULTS[args.key], args.value or ""
        if isinstance(default, bool):
            cfg[args.key] = v.lower() in ("1", "true", "on", "yes")
        elif isinstance(default, int):
            try:
                cfg[args.key] = int(v)
            except ValueError:
                print(f"{args.key} needs a whole number")
                return 1
        else:
            if args.key == "read_guard" and v not in ("ask", "deny", "off"):
                print("read_guard must be ask, deny or off")
                return 1
            cfg[args.key] = v
        guard.save_config(cfg)
        print(f"Set {args.key} = {cfg[args.key]!r}")
        return 0
    state = "ON" if cfg["enabled"] else "OFF"
    if cfg["enabled"] and guard.disabled_by_env():
        state = "OFF for this session (BURNLENS_GUARD is set)"
    print(f"burnlens guardrails: {state}")
    for k in guard.DEFAULTS:
        if k == "enabled":
            continue
        mark = "" if cfg[k] == guard.DEFAULTS[k] else "   (changed)"
        print(f"  {k:<22} {cfg[k]!r}{mark}")
    print(f"Settings file: {guard.CONFIG}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if argv[:1] == ["sync"]:
        sys.exit(sync_main(argv[1:]))
    if argv[:1] == ["connect"]:
        sys.exit(connect_main(argv[1:]))
    if argv[:1] == ["guard"]:
        sys.exit(guard_main(argv[1:]))
    if argv[:1] == ["statusline"]:
        from . import guard

        sys.exit(guard.main(["statusline"]))
    if argv[:1] == ["fixes"]:
        sys.exit(fixes_main(argv[1:]))
    if argv[:1] == ["savings"]:
        sys.exit(savings_main(argv[1:]))
    ap = argparse.ArgumentParser(prog="burnlens", description=__doc__,
                                 epilog="More: `burnlens fixes` (what to change), `burnlens savings` (what changing it saved), "
                                        "`burnlens connect --server URL --token T` (org mode).")
    ap.add_argument("--root", help="transcripts dir (default: ~/.claude/projects or $CLAUDE_CONFIG_DIR/projects)")
    ap.add_argument("--out", default=os.path.expanduser("~/.burnlens/dashboard.html"), help="output HTML path")
    ap.add_argument("--json", metavar="PATH", help="also write the normalized dataset as JSON")
    ap.add_argument("--demo", action="store_true", help="use a synthetic dataset instead of your transcripts")
    ap.add_argument("--redact", action="store_true", help="replace session titles and agent descriptions")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    data = demo.generate() if args.demo else parser.load(args.root, redact=args.redact)
    if not data["calls"]:
        sys.exit(f"No Claude Code API calls found under {data['source']}. Try --demo to preview.")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f)
    data["version"] = __version__
    from . import guard

    if args.demo:
        data["fixes"], data["savings"], data["guard"] = fixes.recommend(data, local=False), demo.savings(), demo.guard_report()
    else:
        data["fixes"], data["savings"], data["guard"] = fixes.recommend(data), fixes.savings(data), guard.report(data)
    if args.redact:
        for t in data["tools"]:
            t.pop("path", None), t.pop("cmd", None)
        for s in data["sessions"].values():
            s.pop("cwd", None)
    path = render(data, args.out)
    print(summary(data))
    print(f"Dashboard: {os.path.abspath(path)}")
    if not args.no_open:
        webbrowser.open(pathlib.Path(path).resolve().as_uri())
