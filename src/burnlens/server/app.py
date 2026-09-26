"""burnlens org server: receives pushes from `burnlens sync`, serves the org dashboard.

Tokens (created with `burnlens-server create-token`):
  ingest  developers' machines push usage
  viewer  dashboard with team and org totals
  admin   dashboard including per-person rows
"""

import html
import json
import os
from importlib import resources

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import __version__
from . import db

COOKIE = "burnlens_token"
MAX_BODY = 20 * 1024 * 1024
SECURE_COOKIE = os.environ.get("BURNLENS_SECURE_COOKIE", "0") == "1"

app = FastAPI(title="burnlens", version=__version__, docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup():
    db.init()


def _bearer(request):
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _viewer_kind(request):
    kind = db.token_kind(_bearer(request) or request.cookies.get(COOKIE))
    return kind if kind in ("viewer", "admin") else None


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.post("/api/ingest")
async def ingest(request: Request):
    if db.token_kind(_bearer(request)) not in ("ingest", "admin"):
        raise HTTPException(401, "invalid ingest token")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "payload too large")
    try:
        payload = json.loads(body)
        return db.ingest(payload)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"bad payload: {e}")


@app.get("/api/data")
def data(request: Request, days: int = 90):
    kind = _viewer_kind(request)
    if not kind:
        raise HTTPException(401, "sign in required")
    return db.dataset(max(0, days), admin=kind == "admin")


@app.get("/api/session/{session_id}")
def session(session_id: str, request: Request):
    if not _viewer_kind(request):
        raise HTTPException(401, "sign in required")
    return db.session_calls(session_id)


LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>burnlens · sign in</title><style>
:root{{color-scheme:light dark;--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--muted:#52514e;--border:rgba(11,11,11,.12)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;--muted:#c3c2b7;--border:rgba(255,255,255,.12)}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,sans-serif;padding:16px}}
form{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;width:100%;max-width:380px}}
h1{{font-size:20px;margin:0 0 4px}}p{{color:var(--muted);margin:0 0 16px}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:9px 10px;border-radius:8px;border:1px solid var(--border);background:transparent;color:inherit}}
button{{margin-top:12px;width:100%;font:inherit;font-weight:600;padding:9px;border-radius:8px;border:0;background:#2a78d6;color:#fff;cursor:pointer}}
.err{{color:#d03b3b;margin-top:10px}}</style></head><body>
<form method="post" action="/login"><h1>🔥 burnlens</h1><p>Org dashboard. Sign in with a viewer or admin token.</p>
<input name="token" type="password" placeholder="bl_viewer_… or bl_admin_…" autocomplete="current-password" autofocus required>
<button>Sign in</button>{error}</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    msg = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return LOGIN.format(error=msg)


@app.post("/login")
def login(token: str = Form(...)):
    if db.token_kind(token) not in ("viewer", "admin"):
        return RedirectResponse("/login?error=That+token+isn%27t+a+valid+viewer+or+admin+token.", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=SECURE_COOKIE, max_age=30 * 86400)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, days: int = 90):
    kind = _viewer_kind(request)
    if not kind:
        return RedirectResponse("/login", status_code=303)
    data = db.dataset(max(0, days), admin=kind == "admin")
    data["version"] = __version__
    template = resources.files("burnlens").joinpath("dashboard.html").read_text()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return HTMLResponse(template.replace("/*__DATA__*/null", payload),
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@app.exception_handler(HTTPException)
def _errors(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
