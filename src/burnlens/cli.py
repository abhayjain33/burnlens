"""burnlens — local token-usage dashboard for Claude Code."""

import argparse
import json
import os
import sys
import webbrowser
from collections import Counter, defaultdict
from importlib import resources

from . import __version__, demo, parser, sync


def render(data, out):
    template = resources.files("burnlens").joinpath("dashboard.html").read_text()
    # Escape "</" so transcript text can never close the <script> tag.
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        f.write(template.replace("/*__DATA__*/null", payload))
    return out


def _tok(n):
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def summary(data):
    """Plain-text digest, so the /burnlens command can answer in chat too."""
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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["sync"]:
        sys.exit(sync_main(argv[1:]))
    if argv[:1] == ["connect"]:
        sys.exit(connect_main(argv[1:]))
    ap = argparse.ArgumentParser(prog="burnlens", description=__doc__,
                                 epilog="Org mode: `burnlens connect --server URL --token T`, then `burnlens sync`.")
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
        with open(args.json, "w") as f:
            json.dump(data, f)
    data["version"] = __version__
    path = render(data, args.out)
    print(summary(data))
    print(f"Dashboard: {os.path.abspath(path)}")
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(path))
