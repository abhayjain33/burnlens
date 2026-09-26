"""Postgres storage for the burnlens org server."""

import hashlib
import os
import secrets
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
create table if not exists users (
  id serial primary key,
  email text unique not null,
  name text,
  team text,
  team_locked boolean not null default false,
  created_at timestamptz not null default now(),
  last_seen timestamptz,
  client_version text
);
create table if not exists tokens (
  id serial primary key,
  token_hash text unique not null,
  kind text not null check (kind in ('ingest', 'viewer', 'admin')),
  label text,
  created_at timestamptz not null default now(),
  revoked_at timestamptz
);
create table if not exists sessions (
  user_id int not null references users(id) on delete cascade,
  session_id text not null,
  project text,
  title text,
  started_at timestamptz,
  ended_at timestamptz,
  primary key (user_id, session_id)
);
create table if not exists calls (
  user_id int not null references users(id) on delete cascade,
  call_id text not null,
  session_id text not null,
  ts timestamptz,
  model text,
  input bigint not null default 0,
  output bigint not null default 0,
  cache_read bigint not null default 0,
  cache_write bigint not null default 0,
  thinking bigint not null default 0,
  agent text,
  category text,
  usd double precision,
  fast boolean not null default false,
  primary key (user_id, call_id)
);
create index if not exists calls_ts on calls (ts);
create table if not exists tools (
  user_id int not null references users(id) on delete cascade,
  tool_id text not null,
  session_id text not null,
  ts timestamptz,
  name text not null,
  server text,
  tool text,
  result_tokens bigint not null default 0,
  carried bigint not null default 0,
  err boolean not null default false,
  category text,
  agent text,
  skill text,
  primary key (user_id, tool_id)
);
create index if not exists tools_ts on tools (ts);
create table if not exists agent_runs (
  user_id int not null references users(id) on delete cascade,
  run_id text not null,
  session_id text not null,
  ts timestamptz,
  type text,
  tokens bigint not null default 0,
  primary key (user_id, run_id)
);
create table if not exists compactions (
  user_id int not null references users(id) on delete cascade,
  session_id text not null,
  ts timestamptz not null,
  primary key (user_id, session_id, ts)
);
"""


def dsn():
    return os.environ.get("DATABASE_URL", "postgresql://burnlens:burnlens@localhost:5432/burnlens")


@contextmanager
def connect():
    with psycopg.connect(dsn(), row_factory=dict_row) as conn:
        yield conn


def init():
    with connect() as conn:
        conn.execute(SCHEMA)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(kind, label=None):
    token = f"bl_{kind}_{secrets.token_hex(20)}"
    with connect() as conn:
        conn.execute("insert into tokens (token_hash, kind, label) values (%s, %s, %s)", (_hash(token), kind, label))
    return token


def token_kind(token):
    if not token:
        return None
    with connect() as conn:
        row = conn.execute("select kind from tokens where token_hash = %s and revoked_at is null",
                           (_hash(token),)).fetchone()
    return row["kind"] if row else None


def list_tokens():
    with connect() as conn:
        return conn.execute("select id, kind, label, created_at, revoked_at from tokens order by id").fetchall()


def revoke_token(token_id):
    with connect() as conn:
        return conn.execute("update tokens set revoked_at = now() where id = %s and revoked_at is null",
                            (token_id,)).rowcount


def set_team(email, team):
    with connect() as conn:
        return conn.execute("update users set team = %s, team_locked = true where email = %s",
                            (team, email.lower())).rowcount


def purge_demo():
    """Delete the fake people created by seed-demo; their rows cascade."""
    with connect() as conn:
        return conn.execute("delete from users where email like %s", ("%@demo.local",)).rowcount


def list_users():
    with connect() as conn:
        return conn.execute("select id, email, name, team, team_locked, last_seen, client_version from users order by email").fetchall()


# ---------- ingest ----------

def ingest(payload):
    u = payload.get("user") or {}
    email = (u.get("email") or "").strip().lower()
    if not email:
        raise ValueError("user.email is required")
    client = payload.get("client") or {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """insert into users (email, name, team, last_seen, client_version) values (%s, %s, %s, now(), %s)
               on conflict (email) do update set
                 name = coalesce(excluded.name, users.name),
                 team = case when users.team_locked then users.team else coalesce(excluded.team, users.team) end,
                 last_seen = now(), client_version = excluded.client_version
               returning id""",
            (email, u.get("name"), u.get("team"), client.get("version")),
        )
        uid = cur.fetchone()["id"]

        sessions = payload.get("sessions") or {}
        cur.executemany(
            """insert into sessions (user_id, session_id, project, title, started_at, ended_at) values (%s, %s, %s, %s, %s, %s)
               on conflict (user_id, session_id) do update set project = excluded.project,
                 title = coalesce(excluded.title, sessions.title),
                 started_at = least(sessions.started_at, excluded.started_at),
                 ended_at = greatest(sessions.ended_at, excluded.ended_at)""",
            [(uid, sid, s.get("project"), s.get("title"), s.get("start"), s.get("end")) for sid, s in sessions.items()],
        )
        calls = [c for c in payload.get("calls") or [] if c.get("id")]
        cur.executemany(
            """insert into calls (user_id, call_id, session_id, ts, model, input, output, cache_read, cache_write,
                                  thinking, agent, category, usd, fast)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (user_id, call_id) do update set category = excluded.category, usd = excluded.usd""",
            [(uid, c["id"], c["s"], c.get("ts"), c.get("m"), c.get("i", 0), c.get("o", 0), c.get("cr", 0),
              c.get("cw", 0), c.get("th", 0), c.get("a"), c.get("c"), c.get("usd"), bool(c.get("fast")))
             for c in calls],
        )
        tools = [t for t in payload.get("tools") or [] if t.get("id")]
        # carried grows as a session continues, so re-sent rows update it
        cur.executemany(
            """insert into tools (user_id, tool_id, session_id, ts, name, server, tool, result_tokens, carried, err,
                                  category, agent, skill)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (user_id, tool_id) do update set result_tokens = excluded.result_tokens,
                 carried = excluded.carried, err = excluded.err, category = excluded.category""",
            [(uid, t["id"], t["s"], t.get("ts"), t["n"], t.get("srv"), t.get("t"), t.get("rt", 0), t.get("cx", 0),
              bool(t.get("err")), t.get("c"), t.get("a"), t.get("skill")) for t in tools],
        )
        agents = [a for a in payload.get("agents") or [] if a.get("id")]
        cur.executemany(
            """insert into agent_runs (user_id, run_id, session_id, ts, type, tokens) values (%s, %s, %s, %s, %s, %s)
               on conflict (user_id, run_id) do update set tokens = excluded.tokens""",
            [(uid, a["id"], a["s"], a.get("ts"), a.get("type"), a.get("tokens") or 0) for a in agents],
        )
        comps = [(uid, sid, ts) for sid, lst in (payload.get("compactions") or {}).items() for ts in lst if ts]
        cur.executemany("insert into compactions values (%s, %s, %s) on conflict do nothing", comps)
    return {"user": email, "sessions": len(sessions), "calls": len(calls), "tools": len(tools), "agents": len(agents)}


# ---------- read models for the dashboard ----------

def _iso(v):
    return v.isoformat().replace("+00:00", "Z") if v is not None else None


def dataset(days, admin):
    """Dashboard dataset. Calls and tools are pre-aggregated per session/day so an
    org's worth of history stays small; `w` carries the row count."""
    since = f"now() - interval '{int(days)} days'" if days else "'-infinity'::timestamptz"
    with connect() as conn:
        users = {r["id"]: r for r in conn.execute("select id, email, name, team from users").fetchall()}
        calls = conn.execute(f"""
            select session_id s, date_trunc('day', ts) d, max(ts) ts, model m, category c, agent a,
                   count(*) w, sum(input) i, sum(output) o, sum(cache_read) cr, sum(cache_write) cw,
                   sum(thinking) th, sum(usd) usd, bool_or(fast) fast,
                   max(case when agent is null then input + cache_read + cache_write end) pk
            from calls where ts >= {since} group by 1, 2, 4, 5, 6""").fetchall()
        tools = conn.execute(f"""
            select session_id s, date_trunc('day', ts) d, max(ts) ts, name n, server srv, tool t, category c,
                   agent a, skill, count(*) w, sum(result_tokens) rt, sum(carried) cx, count(*) filter (where err) err
            from tools where ts >= {since} group by 1, 2, 4, 5, 6, 7, 8, 9""").fetchall()
        agents = conn.execute(f"select session_id s, ts, type, tokens from agent_runs where ts >= {since}").fetchall()
        sids = {r["s"] for r in calls}
        sessions = conn.execute("select user_id, session_id, project, title, started_at, ended_at from sessions "
                                "where session_id = any(%s)", (list(sids),)).fetchall()
        comps = conn.execute("select session_id, ts from compactions where session_id = any(%s)", (list(sids),)).fetchall()

    out_sessions = {}
    for s in sessions:
        u = users.get(s["user_id"]) or {}
        out_sessions[s["session_id"]] = {
            "title": s["title"] or (s["project"] or "session") + " · " + s["session_id"][:6],
            "project": s["project"], "start": _iso(s["started_at"]), "end": _iso(s["ended_at"]),
            "team": u.get("team") or "Unassigned", "user": u.get("email") if admin else None,
        }
    for rows in (calls, tools):
        for r in rows:
            r.pop("d", None)
            r["ts"] = _iso(r["ts"])
            for k in ("i", "o", "cr", "cw", "th", "rt", "cx", "w", "err", "pk"):
                if k in r and r[k] is not None:
                    r[k] = int(r[k])
    for a in agents:
        a["ts"] = _iso(a["ts"])
    compactions = {}
    for c in comps:
        compactions.setdefault(c["session_id"], []).append(_iso(c["ts"]))
    people = {u["email"]: {"name": u["name"], "team": u["team"] or "Unassigned"} for u in users.values()} if admin else {}
    return {"org": True, "role": "admin" if admin else "viewer", "days": days, "demo": False,
            "source": "org server", "files": 0, "sessions": out_sessions, "calls": calls, "tools": tools,
            "agents": agents, "compactions": compactions, "people": people}


def session_calls(session_id):
    with connect() as conn:
        rows = conn.execute("""select ts, input i, output o, cache_read cr, cache_write cw, usd, category c
                               from calls where session_id = %s and agent is null order by ts""",
                            (session_id,)).fetchall()
    for r in rows:
        r["ts"] = _iso(r["ts"])
    return rows
