"""Push usage to a burnlens org server.

Runs from the plugin's SessionEnd hook (in the background) or by hand with
`burnlens sync`. Only numbers leave the machine: token counts, tool names,
MCP servers, work types, project folder names. Prompt text and session
titles are never sent unless BURNLENS_SEND_TITLES=1.

Configuration, first match wins:
  env   BURNLENS_SERVER, BURNLENS_TOKEN, BURNLENS_USER, BURNLENS_TEAM
  file  ~/.burnlens/config.json  (written by `burnlens connect`)
Without a server configured, sync is a silent no-op.
"""

import getpass
import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__, parser

HOME = os.path.expanduser("~/.burnlens")
CONFIG = os.path.join(HOME, "config.json")
STATE = os.path.join(HOME, "sync-state.json")
LOCK = os.path.join(HOME, "sync.lock")
CHUNK = 2000
LOCAL_ONLY = ("path", "cmd")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    os.makedirs(HOME, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def config():
    cfg = _read_json(CONFIG)
    for key in ("server", "token", "user", "team"):
        env = os.environ.get("BURNLENS_" + key.upper())
        if env:
            cfg[key] = env
    if cfg.get("server"):
        cfg["server"] = cfg["server"].rstrip("/")
    return cfg


def _git(key):
    try:
        out = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def identity(cfg):
    email = cfg.get("user") or _git("user.email") or f"{getpass.getuser()}@{socket.gethostname()}"
    return {"email": email.lower(), "name": _git("user.name") or getpass.getuser(), "team": cfg.get("team")}


def connect(server, token, team=None, user=None):
    cfg = _read_json(CONFIG)
    cfg.update({k: v for k, v in {"server": server, "token": token, "team": team, "user": user}.items() if v})
    _write_json(CONFIG, cfg)
    os.chmod(CONFIG, 0o600)
    return cfg


def _session_of(path, root):
    """Transcript file -> session id: <sid>.jsonl, or <sid>/subagents/*.jsonl."""
    rel = os.path.relpath(path, root).split(os.sep)
    return rel[-3] if len(rel) >= 3 and rel[-2] == "subagents" else os.path.splitext(rel[-1])[0]


def strip_local(tools, sessions):
    """File paths, commands and working directories stay on this machine."""
    tools = [{k: v for k, v in t.items() if k not in LOCAL_ONLY} for t in tools]
    sessions = {k: {f: v for f, v in s.items() if f != "cwd"} for k, s in sessions.items()}
    return tools, sessions


def _post(cfg, payload):
    req = urllib.request.Request(
        cfg["server"] + "/api/ingest",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["token"],
                 "User-Agent": "burnlens/" + __version__},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _acquire_lock():
    os.makedirs(HOME, exist_ok=True)
    try:
        if time.time() - os.path.getmtime(LOCK) > 600:  # stale lock from a crashed run
            os.remove(LOCK)
    except OSError:
        pass
    try:
        os.close(os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def run(full=False, quiet=False, root=None):
    say = (lambda *a: None) if quiet else print
    cfg = config()
    if not cfg.get("server") or not cfg.get("token"):
        say("burnlens sync: no org server configured (run `burnlens connect --server URL --token TOKEN`).")
        return 0
    if not _acquire_lock():
        say("burnlens sync: another sync is running.")
        return 0
    try:
        root = root or parser.default_root()
        all_state = _read_json(STATE)
        state = {} if full else all_state.get(cfg["server"], {})  # tracked per server
        files = {}
        for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            st = os.stat(p)
            files[p] = [st.st_mtime, st.st_size]
        changed = [p for p, sig in files.items() if state.get(p) != sig]
        if not changed:
            say("burnlens sync: up to date.")
            return 0
        wanted = {_session_of(p, root) for p in changed}

        data = parser.load(root, redact=os.environ.get("BURNLENS_SEND_TITLES") != "1")
        keep = lambda rows: [r for r in rows if r["s"] in wanted]  # noqa: E731
        calls, tools, agents = keep(data["calls"]), keep(data["tools"]), keep(data["agents"])
        tools, sessions = strip_local(tools, {k: v for k, v in data["sessions"].items() if k in wanted})
        if os.environ.get("BURNLENS_SEND_TITLES") != "1":
            for s in sessions.values():
                s["title"] = None
        base = {
            "user": identity(cfg),
            "client": {"version": __version__, "host": socket.gethostname()},
            "sessions": sessions,
            "compactions": {k: v for k, v in data["compactions"].items() if k in wanted},
        }
        n = max(len(calls), len(tools), 1)
        sent = {"calls": 0, "tools": 0}
        for i in range(0, n, CHUNK):
            payload = dict(base, calls=calls[i:i + CHUNK], tools=tools[i:i + CHUNK],
                           agents=agents if i == 0 else [])
            res = _post(cfg, payload)
            sent["calls"] += res.get("calls", 0)
            sent["tools"] += res.get("tools", 0)
        state.update({p: files[p] for p in changed})
        all_state[cfg["server"]] = state
        _write_json(STATE, all_state)
        say(f"burnlens sync: sent {len(sessions)} sessions, {sent['calls']} calls, {sent['tools']} tool calls "
            f"to {cfg['server']} as {base['user']['email']}.")
        return 0
    except urllib.error.HTTPError as e:
        say(f"burnlens sync: server returned {e.code}: {e.read()[:200].decode(errors='replace')}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        say(f"burnlens sync: could not reach server ({e}); will retry next session.")
        return 1
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def run_in_background():
    """Detach so the SessionEnd hook returns immediately."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-c", "import sys; sys.path[:0] = %r; from burnlens import sync; sync.run(quiet=True)"
                      % [p for p in sys.path if p]], **kwargs)
