# Setting up burnlens for an organization

This guide takes you from nothing to a running org dashboard with developers' usage flowing in. Expect about 30 minutes, most of it on HTTPS and the settings rollout.

**How it fits together:** each developer's Claude Code has the burnlens plugin. When a session ends, the plugin parses that machine's transcripts and pushes the numbers (never prompt text) to your burnlens server. You and your team leads read the dashboard in a browser.

```
developer laptops                               your server (Docker Compose)
Claude Code + burnlens plugin  ──HTTPS push──▶  burnlens-server ── Postgres
                                                     ▲
                        admins / team leads ─────────┘ browser, sign in with a token
```

## What you need

| | |
|---|---|
| A host | Any Linux VM, cloud instance or internal server that developers' laptops can reach. For a pilot of up to ~50 developers, 1 vCPU and 1 GB RAM is enough. |
| Docker | Docker Engine with the Compose plugin (`docker compose version` works). |
| A DNS name and TLS | e.g. `burnlens.internal.example.com`, served over HTTPS (step 3). |
| GitHub access | The repo `abhayjain33/burnlens` is **private**. The server host needs access to clone it. **Every developer's machine** also needs git access to install the plugin: grant the developers read access, or put the repo in your GitHub organization and give a team read access. |
| Python 3.9+ on laptops | The plugin runs `python3`; macOS and most Linux systems already have it. |

## 1. Install the server

```bash
git clone https://github.com/abhayjain33/burnlens.git
cd burnlens
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env` and set `POSTGRES_PASSWORD` to a long random value (for example the output of `openssl rand -hex 24`). Set it **before the first start**: the database keeps the password it was created with.

```bash
docker compose -f deploy/docker-compose.yml up -d --build
curl http://localhost:8080/healthz          # → {"ok":true,"version":"…"}
```

For the rest of this guide, this alias runs admin commands inside the container:

```bash
alias bls='docker compose -f deploy/docker-compose.yml exec server burnlens-server'
```

## 2. Create tokens

```bash
bls create-token admin  --label "your-name"        # dashboard incl. per-person data
bls create-token viewer --label "team leads"       # dashboard with team and org totals only
bls create-token ingest --label "all developers"   # goes on developer machines
```

Each token is printed **once**. Store them in your password manager. The dashboard is at `http://<host>:8080`: sign in with the admin token to check it loads. It will be empty until data arrives.

**Optional: try it with fake data.** `bls seed-demo` adds 8 fake people across 3 teams so you can explore the dashboard. Remove them before going live with `bls purge-demo`.

## 3. Put it behind HTTPS

Tokens travel in request headers and cookies, so don't expose plain HTTP beyond a trusted network.

**If the host has no web server yet**, [Caddy](https://caddyserver.com/) gets a certificate automatically. Install it on the host and use this `Caddyfile`:

```
burnlens.internal.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

**If you already run nginx or a load balancer**, proxy `https://burnlens.internal.example.com` → `http://<host>:8080`. Allow request bodies up to 20 MB, since the first sync of a heavy user is large.

Then update `deploy/.env`:

```bash
BURNLENS_BIND=127.0.0.1        # only the proxy on this host can reach port 8080
BURNLENS_SECURE_COOKIE=1       # login cookie only sent over HTTPS
```

and apply it with `docker compose -f deploy/docker-compose.yml up -d`. Check that `https://burnlens.internal.example.com/healthz` responds from a laptop.

## 4. Connect developers

### Option A: managed settings (recommended)

Claude Code reads an admin-controlled **managed settings** file that users can't override. Put the following in it, with your server URL and the **ingest** token. It installs the plugin, enables it, and tells it where to push:

```json
{
  "extraKnownMarketplaces": {
    "burnlens": {
      "source": { "source": "git", "url": "https://github.com/abhayjain33/burnlens.git" }
    }
  },
  "enabledPlugins": { "burnlens@burnlens": true },
  "env": {
    "BURNLENS_SERVER": "https://burnlens.internal.example.com",
    "BURNLENS_TOKEN": "bl_ingest_…"
  }
}
```

Where the file goes (distribute it with your MDM, e.g. Jamf, Intune or Kandji):

| OS | Path |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux / WSL | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

These are the paths at the time of writing; check the [Claude Code settings docs](https://docs.claude.com/en/docs/claude-code/settings) for your version. If your developers authenticate to GitHub over SSH, use `"url": "git@github.com:abhayjain33/burnlens.git"`.

Optionally add `"BURNLENS_TEAM": "Payments"` to `env` when a file only goes to one team; otherwise set teams from the server (step 6).

### Option B: self-serve

Send developers the server URL and the ingest token. Each one runs, inside Claude Code:

```text
/plugin marketplace add abhayjain33/burnlens
/plugin install burnlens@burnlens
/burnlens connect --server https://burnlens.internal.example.com --token bl_ingest_… --team Payments
```

`connect` saves the settings to `~/.burnlens/config.json` and immediately sends their existing history.

### Identity: ask developers to set their git email

Each developer appears on the dashboard under their **global git email**. If it isn't set, they show up as `username@hostname` instead. Ask developers to check with:

```bash
git config --global user.email          # should print their work email
```

A developer can also report a specific identity with `/burnlens connect … --user name@example.com`.

## 5. Check it works

On a developer machine, inside Claude Code:

```text
/burnlens sync
```

It should reply with something like `sent 12 sessions, 830 calls … as name@example.com`. On the server:

```bash
bls list-users        # the developer appears with a recent "last sync"
```

After that, syncing is automatic: every time a Claude Code session ends, the plugin pushes in the background. Laptops that were offline catch up at their next session end.

## 6. Day-to-day admin

```bash
bls list-users                                  # everyone who has synced: team, last sync, plugin version
bls set-team priya@example.com Payments         # pins a team; overrides whatever the laptop reports
bls list-tokens
bls revoke-token 3
```

**Give team leads access:** send them the viewer token. They see team and org totals, no individuals.

**Rotate the ingest token:** create a new one, update the managed settings (or have developers re-run `connect`), and after a few days `bls revoke-token <old id>`. Pushes with a revoked token are rejected, and the plugin retries at the next session end, so no data is lost in the switch.

**Upgrade:**

```bash
git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

The database schema updates itself on start. Developers get new plugin versions with `/plugin update burnlens`.

**Back up the database** (e.g. nightly from cron):

```bash
docker compose -f deploy/docker-compose.yml exec -T db pg_dump --clean --if-exists -U burnlens burnlens | gzip > burnlens-$(date +%F).sql.gz
```

To restore, into the same server or a fresh install (the dump replaces whatever tables are there):

```bash
gunzip -c burnlens-2026-01-31.sql.gz | docker compose -f deploy/docker-compose.yml exec -T db psql -v ON_ERROR_STOP=1 -U burnlens burnlens
```

**Logs:** `docker compose -f deploy/docker-compose.yml logs -f server`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `/burnlens sync` says "no org server configured" | The managed settings `env` hasn't reached this machine, or `connect` wasn't run. Restart Claude Code after the settings file is installed. |
| `server returned 401` | Wrong or revoked ingest token. |
| `could not reach server` | DNS, VPN or firewall; check `curl https://<server>/healthz` from the laptop. |
| Plugin won't install ("repository not found") | That developer's git has no access to the private repo. |
| A developer appears as `user@hostname` | Their global git email isn't set (see "Identity" above). |
| Someone is missing from the dashboard | `bls list-users`: if they're absent, they've never synced; if present, check the date range filter. |
| Server won't start: "set POSTGRES_PASSWORD" | `deploy/.env` is missing; copy it from `.env.example`. |
| Dashboard is slow to load | You have a lot of history; see the limits below. |

## What's collected, and limits of this version

**Sent to the server:** token counts, API-equivalent cost, model, tool and MCP server names, tool result sizes, work type, project **folder name**, timestamps, and the developer's email and team. **Never sent:** prompts, responses, file contents, tool arguments, or session titles (unless `BURNLENS_SEND_TITLES=1` is set on the laptop).

**Limits:**
- All developers share one ingest token, so the server trusts the email each laptop reports. Treat this as a pilot for a trusted network; per-developer tokens are on the roadmap.
- The dashboard loads 90 days at a time (the "All" range loads everything), pre-aggregated per session and day. That works well for tens of developers; hundreds will need server-side aggregation, which is also on the roadmap.
- There's no SSO yet: people sign in with viewer or admin tokens.
