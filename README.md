# 🔥 burnlens

**See where your Claude Code tokens burn.** burnlens is a local dashboard that breaks down your usage by token type, work type (development, documentation, analysis, design, research), tool, MCP server, subagent, skill, model and session.

It reads the transcripts Claude Code already writes to `~/.claude/projects`. It has no dependencies and makes no network calls, so nothing leaves your machine. [Org mode](#org-mode) is opt-in: it adds a shared server where a team's usage is pushed automatically.

## Install

### Option A: inside Claude Code (recommended)

```text
/plugin marketplace add abhayjain33/burnlens
/plugin install burnlens@burnlens
```

Needs Python 3.8+ (`python3`, `python` or Windows' `py` launcher; burnlens finds whichever works). Then type **`/burnlens:burnlens`** in any session. It builds the dashboard, opens it in your browser, and Claude summarizes the highlights in chat.

Claude Code names plugin commands `plugin:command`, so all burnlens commands start with `/burnlens:`. Type `/burnlens:` to see all four in autocomplete. The short form (`/burnlens-guard`) gives "Unknown command".

Arguments: `/burnlens:burnlens --redact` hides session titles, `/burnlens:burnlens --demo` shows synthetic data, and `/burnlens:burnlens --no-open` skips opening the browser.

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
- **Recommended fixes and measured savings:** see below.

## Fixes and measured savings

burnlens doesn't stop at charts. It recommends specific changes, applies them with your approval, and then measures what they saved.

```text
/burnlens:burnlens-fix        # see recommended fixes, pick which to apply; Claude makes each change and you approve it
/burnlens:burnlens-savings    # later: measured before → after for each applied fix
```

(From a terminal: `burnlens fixes`, `burnlens fixes applied <id>`, `burnlens fixes dismiss <id>`, `burnlens savings`.)

| Detects | Example | Fix |
|---|---|---|
| Generated files read into context | `package-lock.json` read 16× = 15M tokens incl. re-sends | `permissions.deny` read rules in the project's `.claude/settings.local.json` |
| Commands with huge output | `npm test` averaging 9k tokens per run | A `CLAUDE.md` line telling Claude to keep that output short |
| MCP tools with huge results | `browser_snapshot` averaging 17k tokens per call | A `CLAUDE.md` line preferring narrower calls |
| Oversized `CLAUDE.md` | 3k tokens on every request | Claude proposes a trimmed version as a diff |
| Unused MCP servers | Configured, never called in 30 days | `claude mcp remove …` / disable in project settings |
| Model fit for each role | Explore agents on Opus; a code reviewer on Sonnet | Bulk reading and routine implementation → Sonnet; planning and review → your strongest model (costs more, labelled *quality*) |
| Planning and implementing on Opus | Opus sessions that plan, then write code | `/model opusplan`: Opus in plan mode, Sonnet for implementation |

Model advice follows the role, not just the price. A better plan saves more in implementation than it costs, so planning and review agents are never pushed to a cheaper model. Model switches are measured by **cost per run**, not per token: if a cheaper model needs more turns and ends up costing more, `/burnlens:burnlens-savings` reports that the fix *made it worse*.

Each recommendation shows a projected monthly saving and how far to trust it. **Measured** means cost that already happened and the fix removes. **Estimated** depends on a stated assumption, e.g. "output shrinks 60%". Unused MCP servers are **not quantifiable**. When you apply a fix, burnlens records a baseline in `~/.burnlens/fixes.json`. Once there are a few sessions of new usage, `/burnlens:burnlens-savings` compares against it: tokens per session (or per call) before and after, times the usage since. The dashboard shows both lists too.

File paths and command names used by the detectors stay on your machine; `burnlens sync` strips them before anything goes to an org server.

## Live guardrails

The plugin also steps in *during* a session, before waste happens. The guardrails ask or warn; they never block on their own.

| Guardrail | What it does | Default |
|---|---|---|
| Read guard | Before Claude reads a generated file (lockfile, bundle) or a large file in full, Claude Code asks you first, with the token cost and a suggestion to read a range or use Grep. Can be set to deny or off. | asks you |
| Loop guard | When a command or file read returns the same result 3 times, tells Claude to change approach | on |
| Context alert | When a session passes 150k tokens (and every 50k after), tells you what each turn now costs | on |
| Status line | `🔥 $1.23 · ctx 124k (62%) · cache 95%` | opt-in |

**Turn them all on or off** with `/burnlens:burnlens-guard off` / `/burnlens:burnlens-guard on` (terminal: `burnlens guard off` / `on`), or for a single session with `BURNLENS_GUARD=off`. The status line isn't a guardrail and keeps working either way. Change individual settings with `/burnlens:burnlens-guard`, e.g. `/burnlens:burnlens-guard read guard deny` or `/burnlens:burnlens-guard status line` (terminal: `burnlens guard status`, `burnlens guard set read_guard deny`, `burnlens guard reset`).

Each check takes about 60 ms, and a guardrail that hits an internal error stays silent, so it can't break a session.

**Proof they pay off.** The dashboard's *Guardrails: on vs off* section compares sessions with guardrails on against sessions where they were off (or not yet installed): cost, tokens, context per call, and tool output per session. It also counts what the guardrails actually did: full reads prevented (reads that were questioned and didn't happen, with the tokens avoided), loop nudges and context alerts. Sessions do different work, so the on/off averages are indicative; prevented reads are the direct evidence. The comparison appears once there are 3 sessions each way.

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
commands/           /burnlens:burnlens, /burnlens:burnlens-fix, /burnlens:burnlens-savings, /burnlens:burnlens-guard slash commands
scripts/run.py      runs from a checkout without installing
hooks/hooks.json    guardrail hooks (PreToolUse, PostToolUse, UserPromptSubmit) + SessionEnd sync
src/burnlens/       parser, classifier, pricing, fixes (detectors + savings ledger), demo data, sync client, dashboard.html
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
