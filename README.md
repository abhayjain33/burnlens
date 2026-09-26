# 🔥 burnlens

**See where your Claude Code tokens burn.** burnlens is a local dashboard that breaks down your usage by token type, work type (development, documentation, analysis, design, research), tool, MCP server, subagent, skill, model and session.

It reads the transcripts Claude Code already writes to `~/.claude/projects`. It has no dependencies and makes no network calls, so nothing leaves your machine. [Org mode](#org-mode) is opt-in: it adds a shared server where a team's usage is pushed automatically.

## Install

### Option A: inside Claude Code (recommended)

```text
/plugin marketplace add abhayjain33/burnlens
/plugin install burnlens@burnlens
```

Then type **`/burnlens`** in any session. It builds the dashboard, opens it in your browser, and Claude summarizes the highlights in chat.

Arguments: `/burnlens --redact` hides session titles, `/burnlens --demo` shows synthetic data, and `/burnlens --no-open` skips opening the browser.

### Option B: from the terminal with `uvx`

```bash
uvx --from git+https://github.com/abhayjain33/burnlens burnlens
```

This needs no install; [uv](https://docs.astral.sh/uv/) fetches and runs it. You can also install it permanently with `pipx install git+https://github.com/abhayjain33/burnlens`, or from a clone with `python3 scripts/run.py`.

Terminal flags: `--root DIR`, `--out PATH` (default `~/.burnlens/dashboard.html`), `--json PATH`, `--redact`, `--demo`, `--no-open`.

## What you get

- **Where the tokens go:** cache reads, cache writes, uncached input and output, shown by volume and by cost.
- **Daily usage:** stacked by token type.
- **Work type:** each prompt classified by what the agent actually did (files edited, tools used).
- **Tools & MCP leaderboard:** tools ranked by calls, result tokens, or *carried* tokens. Carried tokens are a result's size multiplied by the number of later turns that re-sent it. MCP servers expand into their individual tools.
- **Subagents and skills:** the tokens each consumed.
- **Sessions:** context growth per call, with compactions visible as drops.
- **Insights:** cache hit rate, oversized tool results, runaway sessions and similar.

## Org mode

A self-hosted server that collects everyone's usage into one dashboard, with views by team and, for admins, by person.

```
developer machines                                  org server (Docker Compose)
Claude Code session ends                            ┌───────────────────────────┐
  → burnlens SessionEnd hook (background)  ──push──▶│ burnlens-server + Postgres│◀── viewers / admins (browser)
    parses changed transcripts locally              └───────────────────────────┘
```

**What gets sent:** token counts, cost, model, tool and MCP names, result sizes, work type, project folder name, timestamps, and the developer's email. **Never sent:** prompts, responses, file contents, tool arguments, or session titles (unless `BURNLENS_SEND_TITLES=1`).

### Quick start

```bash
git clone https://github.com/abhayjain33/burnlens.git && cd burnlens
cp deploy/.env.example deploy/.env        # set POSTGRES_PASSWORD
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec server burnlens-server create-token admin
```

Then open `http://<host>:8080` and sign in with that token.

**For a real rollout, follow the [org setup guide](docs/org-setup.md).** It covers HTTPS, tokens, rolling the plugin out through managed settings, developer identity, checking data arrives, backups, upgrades and troubleshooting.

## How it's measured

| Metric | Source | Exact? |
|---|---|---|
| Tokens per API call | `message.usage`, de-duplicated by request id | exact |
| API-equivalent cost | tokens × Anthropic list price, with 5-min and 1-hour cache writes priced separately | list-price math |
| Tool result tokens | result text length ÷ 3.6; skills include their injected instructions | estimate |
| Carried tokens | result tokens × later main-thread calls before the next compaction | estimate |
| Work type | heuristics in `classify.py` | heuristic |

Subscription plans (Pro/Max/Team) aren't billed per token, so treat the dollar figures as a way to compare. Bedrock and Vertex prices differ; edit `src/burnlens/pricing.py` to match yours.

**Privacy:** the generated HTML contains your session titles and project names. Use `--redact` before sharing screenshots.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/run.py --demo
```

```
.claude-plugin/     plugin.json + marketplace.json (this repo is its own marketplace)
commands/           /burnlens slash command
scripts/run.py      runs from a checkout without installing
hooks/hooks.json    SessionEnd hook → background `burnlens sync`
src/burnlens/       parser, classifier, pricing, demo data, sync client, dashboard.html
src/burnlens/server FastAPI + Postgres org server (`pip install ".[server]"`)
deploy/             docker-compose.yml; Dockerfile at repo root
tests/              unit tests; test_server.py runs when DATABASE_URL is set
```

## Roadmap

- Org mode: per-developer tokens, SSO (OIDC), budgets and alerts, OpenTelemetry ingest for machines without the plugin.
- MCP schema tax: measure the tool-definition tokens every request carries.
- Copilot support through a BYOK proxy feeding the same data model.
- Tokens per commit or PR, joined with git history.

MIT licensed.
