"""Synthetic dataset shaped like a busy team's month of Claude Code use.

Only for previewing the dashboard; always flagged `demo: True` so the page
labels it. Numbers follow realistic proportions (cache reads dominate volume,
output dominates per-token cost, a few MCP tools return huge payloads).
"""

import random
from datetime import datetime, timedelta, timezone

from .pricing import cost

PROJECTS = ["payments-api", "web-dashboard", "infra-terraform", "mobile-app", "docs-site", "data-pipeline"]
MODELS = [("claude-opus-5-5", 0.55), ("claude-sonnet-5", 0.30), ("claude-fable-5-1", 0.08), ("claude-haiku-4-5", 0.07)]
CATS = [("Development", 0.42), ("Analysis", 0.22), ("Documentation", 0.11), ("Design", 0.08),
        ("Research", 0.07), ("Conversation", 0.10)]
BUILTIN = {  # name: (weight, mean result tokens)
    "Read": (30, 2600), "Edit": (18, 180), "Bash": (22, 1400), "Grep": (12, 900), "Glob": (6, 250),
    "Write": (5, 120), "WebFetch": (3, 5200), "WebSearch": (2, 2400), "TodoWrite": (6, 90),
    "Agent": (3, 1500), "Skill": (2, 3200),
}
MCP = {  # server: {tool: (weight, mean result tokens)}
    "github": {"get_file_contents": (6, 7800), "search_code": (4, 5200), "list_pull_requests": (3, 3900),
               "get_pull_request_diff": (2, 11500), "create_pull_request": (1, 450), "list_issues": (2, 4100)},
    "atlassian": {"getJiraIssue": (4, 3300), "searchJiraIssuesUsingJql": (3, 9800),
                  "getConfluencePage": (2, 12400), "createJiraIssue": (1, 380)},
    "playwright": {"browser_snapshot": (5, 14200), "browser_navigate": (4, 6100), "browser_click": (4, 5400),
                   "browser_take_screenshot": (2, 1600)},
    "postgres": {"query": (5, 2200), "list_tables": (2, 800), "describe_table": (3, 650)},
    "figma": {"get_design_context": (2, 16800), "get_screenshot": (2, 1600), "get_variable_defs": (1, 2100)},
    "context7": {"resolve-library-id": (2, 900), "get-library-docs": (3, 8600)},
}
DEMO_FILES = ["src/app.ts", "src/api/handlers.ts", "README.md", "package.json", "src/db/models.py",
              "package-lock.json", "tests/test_api.py", "dist/bundle.min.js", "poetry.lock", "src/ui/theme.css"]
DEMO_CMDS = ["npm test", "git status", "git diff", "python -m pytest", "ls", "npm run build", "rg", "docker compose"]
AGENTS = [("Explore", 0.45), ("general-purpose", 0.25), ("Plan", 0.15), ("code-reviewer", 0.15)]
SKILLS = ["pdf", "xlsx", "frontend-design", "code-review", "docx", "security-review"]
TITLES = {
    "Development": ["Add retry logic to webhook handler", "Fix flaky checkout test", "Refactor auth middleware",
                    "Implement CSV export", "Migrate to new ORM", "Add rate limiter"],
    "Analysis": ["Why is p99 latency up?", "Explain the billing flow", "Investigate memory leak",
                 "Review PR #412 risk", "Map service dependencies"],
    "Documentation": ["Write README for SDK", "Update API docs", "Draft ADR for queueing", "Changelog for v2.3"],
    "Design": ["Redesign settings page", "Build dashboard layout", "Dark mode theme tokens"],
    "Research": ["Compare vector DBs", "Latest Next.js caching docs", "Evaluate OTel exporters"],
    "Conversation": ["Plan Q4 migration", "Brainstorm naming", "Talk through architecture"],
}
CAT_TOOLS = {
    "Development": ["Read", "Edit", "Bash", "Grep", "Write", "github", "postgres", "context7"],
    "Analysis": ["Read", "Grep", "Glob", "Bash", "github", "atlassian", "postgres", "Agent"],
    "Documentation": ["Read", "Write", "Edit", "atlassian", "github"],
    "Design": ["Read", "Edit", "figma", "playwright", "Skill"],
    "Research": ["WebFetch", "WebSearch", "context7", "playwright", "Agent"],
    "Conversation": ["Read", "TodoWrite"],
}


def _pick(pairs, rnd):
    r, acc = rnd.random(), 0
    for v, w in pairs:
        acc += w
        if r <= acc:
            return v
    return pairs[-1][0]


def _pick_tool(cat, rnd):
    group = rnd.choice(CAT_TOOLS[cat])
    if group in MCP:
        tools = list(MCP[group].items())
        name, (_, mean) = rnd.choices(tools, weights=[w for _, (w, _) in tools])[0]
        return f"mcp__{group}__{name}", group, name, mean
    return group, None, group, BUILTIN[group][1]


def savings():
    """Example measured-savings rows for the demo dashboard."""
    now = datetime.now(timezone.utc)
    return [
        {"id": "demo:1", "title": "Stop reading generated files in web-dashboard", "kind": "large_file_reads",
         "applied_at": (now - timedelta(days=12)).isoformat(), "status": "measured", "expected_usd_30d": 14.2,
         "saved_tokens": 4_620_000, "saved_usd": 11.84, "units_after": 23, "unit": "session",
         "detail": "231,000 → 18,400 tokens per session"},
        {"id": "demo:2", "title": "Trim noisy command output in payments-api", "kind": "noisy_commands",
         "applied_at": (now - timedelta(days=6)).isoformat(), "status": "measured", "expected_usd_30d": 6.1,
         "saved_tokens": 1_150_000, "saved_usd": 2.37, "units_after": 41, "unit": "call",
         "detail": "38,200 → 10,100 tokens per call"},
        {"id": "demo:3", "title": "Remove unused MCP server 'figma' in docs-site", "kind": "unused_mcp_server",
         "applied_at": (now - timedelta(days=2)).isoformat(), "status": "applied (not measurable)", "expected_usd_30d": None,
         "saved_tokens": None, "saved_usd": None, "units_after": 0, "unit": None},
    ]


def generate(days=30, seed=7):
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    sessions, calls, tools, agents, compactions = {}, [], [], [], {}
    n = 0
    for day in range(days, -1, -1):
        date = now - timedelta(days=day)
        weekend = date.weekday() >= 5
        for _ in range(rnd.randint(0, 2) if weekend else rnd.randint(3, 8)):
            n += 1
            sid = f"demo-{n:04d}"
            cat = _pick(CATS, rnd)
            model = _pick(MODELS, rnd)
            t = date.replace(hour=rnd.randint(8, 19), minute=rnd.randint(0, 59))
            project = rnd.choice(PROJECTS)
            sessions[sid] = {"title": rnd.choice(TITLES[cat]), "project": project, "cwd": "/home/dev/" + project,
                             "start": t.isoformat(), "end": None}
            base_ctx = rnd.randint(18000, 32000)  # system prompt + tool schemas + CLAUDE.md
            ctx = base_ctx
            turns = max(2, int(rnd.lognormvariate(3.0, 0.7)))
            for k in range(turns):
                t += timedelta(seconds=rnd.randint(8, 90))
                turn_cat = cat if rnd.random() < 0.8 else _pick(CATS, rnd)
                out = int(rnd.lognormvariate(6.3, 0.9))
                fresh = rnd.randint(40, 600)
                miss = rnd.random() < (0.03 if k else 1.0)
                cw = ctx if miss else fresh + (tools[-1]["rt"] if tools and tools[-1]["s"] == sid else 0)
                cr = 0 if miss else ctx
                cw1h = cw if rnd.random() < 0.5 else 0
                rec = {"ts": t.isoformat(), "s": sid, "m": model, "i": rnd.randint(1, 12), "o": out,
                       "cr": cr, "cw": cw, "th": int(out * rnd.uniform(0.2, 0.6)), "a": None, "fast": False,
                       "c": turn_cat}
                rec["usd"] = cost(model, rec["i"], out, cr, cw - cw1h, cw1h)
                calls.append(rec)
                ctx += cw if not miss else 0
                ctx += out
                if rnd.random() < 0.8:
                    name, srv, short, mean = _pick_tool(turn_cat, rnd)
                    rt = max(20, int(rnd.lognormvariate(0, 0.6) * mean))
                    tr = {"ts": t.isoformat(), "s": sid, "n": name, "srv": srv, "t": short, "rt": rt,
                          "err": rnd.random() < 0.04, "c": turn_cat, "a": None,
                          "cx": rt * max(0, turns - k - 1)}
                    if name == "Skill":
                        tr["skill"] = rnd.choice(SKILLS)
                    if name in ("Read", "Edit", "Write"):
                        tr["path"] = f"/home/dev/{project}/" + rnd.choice(DEMO_FILES)
                        if name == "Read" and tr["path"].endswith(("lock.json", ".lock", ".min.js")):
                            tr["rt"] = int(tr["rt"] * 9)  # generated files are huge
                            tr["cx"] = tr["rt"] * max(0, turns - k - 1)
                    if name == "Bash":
                        tr["cmd"] = rnd.choice(DEMO_CMDS)
                        if tr["cmd"] in ("npm test", "python -m pytest"):
                            tr["rt"] = int(tr["rt"] * 4)  # verbose test runners
                            tr["cx"] = tr["rt"] * max(0, turns - k - 1)
                    tools.append(tr)
                    if name == "Agent":
                        atype = _pick(AGENTS, rnd)
                        sub = rnd.randint(4, 14)
                        total = 0
                        sub_ctx = rnd.randint(9000, 16000)
                        for _ in range(sub):
                            so = int(rnd.lognormvariate(5.6, 0.7))
                            scw = rnd.randint(800, 6000)
                            sm = "claude-haiku-4-5" if atype == "Explore" else model
                            srec = {"ts": t.isoformat(), "s": sid, "m": sm, "i": 3, "o": so, "cr": sub_ctx,
                                    "cw": scw, "th": 0, "a": atype, "fast": False,
                                    "c": "Analysis" if atype in ("Explore", "Plan") else turn_cat}
                            srec["usd"] = cost(sm, 3, so, sub_ctx, scw, 0)
                            calls.append(srec)
                            total += 3 + so + sub_ctx + scw
                            sub_ctx += scw + so
                            sname, ssrv, sshort, smean = _pick_tool("Analysis", rnd)
                            if sname != "Agent":
                                tools.append({"ts": t.isoformat(), "s": sid, "n": sname, "srv": ssrv, "t": sshort,
                                              "rt": int(rnd.lognormvariate(0, 0.5) * smean), "err": False,
                                              "c": srec["c"], "a": atype, "cx": 0})
                        agents.append({"s": sid, "ts": t.isoformat(), "type": atype, "desc": None,
                                       "tokens": total, "toolCount": sub})
                    ctx += rt
                if ctx > 160000 and rnd.random() < 0.5:
                    compactions.setdefault(sid, []).append(t.isoformat())
                    ctx = base_ctx + rnd.randint(6000, 14000)
            sessions[sid]["end"] = t.isoformat()
    return {"source": "synthetic demo data", "files": 0, "demo": True, "sessions": sessions,
            "calls": calls, "tools": tools, "agents": agents, "compactions": compactions}
