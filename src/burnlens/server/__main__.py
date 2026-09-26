"""burnlens-server: run the org server and manage tokens, users and teams.

  burnlens-server serve [--host 0.0.0.0] [--port 8080]
  burnlens-server create-token ingest|viewer|admin [--label TEXT]
  burnlens-server list-tokens | revoke-token ID
  burnlens-server list-users | set-team EMAIL TEAM
  burnlens-server seed-demo          # fake org data for trying the dashboard
  burnlens-server purge-demo         # remove it again
"""

import argparse
import sys

from . import db


def seed_demo():
    """Load synthetic usage for a handful of fake people across three teams."""
    from .. import demo

    people = [("priya@demo.local", "Priya", "Payments"), ("marco@demo.local", "Marco", "Payments"),
              ("aiko@demo.local", "Aiko", "Platform"), ("sam@demo.local", "Sam", "Platform"),
              ("lena@demo.local", "Lena", "Platform"), ("omar@demo.local", "Omar", "Mobile"),
              ("jules@demo.local", "Jules", "Mobile"), ("ravi@demo.local", "Ravi", "Payments")]
    total = 0
    for n, (email, name, team) in enumerate(people):
        d = demo.generate(days=30, seed=100 + n)
        # demo rows are anonymous; give them stable ids and per-person session ids
        prefix = f"{email.split('@')[0]}-"
        for i, c in enumerate(d["calls"]):
            c["id"], c["s"] = f"{prefix}c{i}", prefix + c["s"]
        for i, t in enumerate(d["tools"]):
            t["id"], t["s"] = f"{prefix}t{i}", prefix + t["s"]
        for i, a in enumerate(d["agents"]):
            a["id"], a["s"] = f"{prefix}a{i}", prefix + a["s"]
        sessions = {prefix + k: v for k, v in d["sessions"].items()}
        comps = {prefix + k: v for k, v in d["compactions"].items()}
        keep = min(len(d["calls"]), max(1, int(len(d["calls"]) * (0.3 + 0.1 * n))))  # vary how heavy each person is
        cut = d["calls"][keep - 1]["ts"]
        calls = [c for c in d["calls"] if c["ts"] <= cut]
        live = {c["s"] for c in calls}
        db.ingest({"user": {"email": email, "name": name, "team": team}, "client": {"version": "demo"},
                   "sessions": {k: v for k, v in sessions.items() if k in live}, "calls": calls,
                   "tools": [t for t in d["tools"] if t["s"] in live],
                   "agents": [a for a in d["agents"] if a["s"] in live],
                   "compactions": {k: v for k, v in comps.items() if k in live}})
        db.set_team(email, team)
        total += len(calls)
    print(f"Seeded {len(people)} demo people, {total} API calls.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="burnlens-server", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    t = sub.add_parser("create-token")
    t.add_argument("kind", choices=["ingest", "viewer", "admin"])
    t.add_argument("--label")
    sub.add_parser("list-tokens")
    r = sub.add_parser("revoke-token")
    r.add_argument("id", type=int)
    sub.add_parser("list-users")
    st = sub.add_parser("set-team")
    st.add_argument("email")
    st.add_argument("team")
    sub.add_parser("seed-demo")
    sub.add_parser("purge-demo")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("burnlens.server.app:app", host=args.host, port=args.port, proxy_headers=True)
        return 0
    db.init()
    if args.cmd == "create-token":
        tok = db.create_token(args.kind, args.label)
        print(tok)
        print(f"# {args.kind} token created. It is shown once; store it somewhere safe.", file=sys.stderr)
    elif args.cmd == "list-tokens":
        for r in db.list_tokens():
            state = "revoked" if r["revoked_at"] else "active"
            print(f"{r['id']:>4}  {r['kind']:<7} {state:<8} {r['created_at']:%Y-%m-%d}  {r['label'] or ''}")
    elif args.cmd == "revoke-token":
        print("revoked" if db.revoke_token(args.id) else "no active token with that id")
    elif args.cmd == "list-users":
        for u in db.list_users():
            seen = f"{u['last_seen']:%Y-%m-%d %H:%M}" if u["last_seen"] else "never"
            print(f"{u['email']:<32} {u['team'] or '-':<14}{'(locked)' if u['team_locked'] else '':<9} last sync {seen}  v{u['client_version'] or '?'}")
    elif args.cmd == "set-team":
        print("updated" if db.set_team(args.email, args.team) else "no such user (they appear after their first sync)")
    elif args.cmd == "seed-demo":
        seed_demo()
    elif args.cmd == "purge-demo":
        print(f"Removed {db.purge_demo()} demo people and all their usage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
