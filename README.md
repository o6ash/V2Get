# v2get — Telegram Proxy Subscription Collector & Management Platform

A self-hosted, Dockerized service that continuously collects proxy
configurations from Telegram channels, validates them with lightweight TCP
checks, deduplicates them intelligently, maintains a rotating active pool,
publishes subscription files to GitHub, and ships with a modern web dashboard.

```
┌──────────────┐   every 15 min   ┌───────────────────────────────────────────┐
│  Scheduler   ├─────────────────▶│ collect → parse → dedup → blacklist →       │
└──────────────┘                  │ cooldown filter → TCP validate →            │
        ▲ manual run button       │ cooldown bookkeeping → pool rotation →      │
        │                         │ outputs (active/base64/clash/singbox) →     │
┌──────────────┐                  │ GitHub publish (only on change) → stats     │
│ Web Dashboard│◀───── REST ──────┤                                             │
│  :8080       │                  └───────────────────────────────────────────┘
└──────────────┘                          state persisted in ./data volume
```

## Quick start

```bash
cp .env.example .env        # fill in Telegram + GitHub credentials (optional)
docker compose up -d
```

Open the dashboard at **http://localhost:8080**.

Everything works out of the box. Telegram and GitHub credentials are *optional*:
without them the platform runs, serves the dashboard, and manages any configs
you already have — it simply won't collect from Telegram or publish to GitHub
until configured.

## Configuration

Deployment secrets/paths live in `.env` (read at startup). Everything tunable
lives in the dashboard **Settings** page and persists in the database — changing
the scan interval, pool size, TCP timeout, cooldown duration, GitHub repo/token
or output formats takes effect **without a restart**.

### Telegram credentials

Bots cannot read arbitrary public-channel history, so a logged-in *user*
session is required. Generate a `StringSession` once on your machine:

```bash
pip install telethon
python -m app.tools.gen_session   # prompts for api_id / api_hash / phone / code
```

Put the resulting values in `.env`:

```
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION=<the printed string>
```

### GitHub publishing

```
GITHUB_TOKEN=<PAT with repo / Contents:write>
GITHUB_REPOSITORY=owner/repo
GITHUB_BRANCH=main
GITHUB_TARGET_DIR=          # optional subdirectory
```

Pushes happen only when the published content actually changes.

### Dashboard auth (optional)

Set `DASHBOARD_USER` / `DASHBOARD_PASSWORD` in `.env` to require HTTP Basic auth.

## Supported protocols

VMess · VLESS · Trojan · Shadowsocks (SS) · ShadowsocksR (SSR) · Hysteria2 · TUIC

Deduplication uses connection-identity fingerprints (server+port+secret), so the
same endpoint is collapsed regardless of name, remarks, tags or query params.

## Dashboard pages

| Page | What it does |
|------|--------------|
| Overview | Collector status, last/next run, active/archive/cooldown counts, GitHub status, protocol mix, history chart, recent runs |
| Channels | Add / remove / enable-disable channels, **per-channel scan limit** (messages scanned per run), per-channel stats |
| Active Configs | v2get-branded view with a per-row index and the originating `@channel`; search, filter, delete, export the rotating pool |
| Archive | Search, filter, export, cleanup every unique config ever seen |
| Cooldown | View entries, remaining time, reset fail count, remove cooldown |
| Blacklist | Edit domains / IPs / keywords, import/export, hot-reloaded |
| NPVT | Toggle/observe the isolated `.npvt → bot → V2Ray links` pipeline, tune it, retry files |
| Logs | Live structured logs, search, download |
| Settings | All tunables + output-file preview, download and raw GitHub URLs |

## Output files

Generated under `./data/output/` and published to GitHub:

- `active.txt` — current active subscription (raw links)
- `subscription_base64.txt` — base64 of `active.txt`
- `clash.txt`, `singbox.txt` — optional (toggle in Settings)
- `archive.txt` — every unique config ever discovered (not published)

## Persistence

All state lives under the mounted `./data` volume:

```
data/
├── v2get.db            # configs, archive, channels, message IDs, cooldown,
│                       # settings, stats, run logs, GitHub state
├── output/             # active.txt, subscription_base64.txt, archive.txt, …
├── blacklist/          # blacklist_domains.txt, blacklist_ips.txt, blacklist_keywords.txt
└── logs/               # collector.log (rotated)
```

Container restarts never lose data. The database runs in **WAL mode**
(`journal_mode=WAL`), so dashboard reads stay responsive while the collector
run and the npvt worker write concurrently (you'll see `v2get.db-wal` /
`v2get.db-shm` sidecar files — both are normal and live on the same volume).

Each proxy config is tied to the channel that **first** discovered it; that
origin is never overwritten when the same config reappears elsewhere, so the
source `@channel` shown in the dashboard is stable.

## Local development (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=./data
uvicorn app.main:app --reload --port 8080
```

## Project layout

```
app/
├── main.py                  # FastAPI app, lifespan, dashboard routes, auth
├── config.py                # env-sourced deployment config + paths
├── database.py              # async SQLAlchemy engine/session
├── models.py                # ORM models (persistent state)
├── schemas.py               # Pydantic request models
├── api/routes.py            # REST API
├── core/
│   ├── telegram_client.py   # Telethon collection
│   ├── parser.py            # robust multi-protocol link extraction
│   ├── fingerprint.py       # dedup fingerprints
│   ├── tcp_checker.py       # async TCP liveness checks
│   ├── cooldown_manager.py  # fail counting + cooldown windows
│   ├── pool_manager.py      # rotating active pool (random eviction)
│   ├── subscription.py      # output file generation
│   ├── github_sync.py       # change-aware GitHub publishing
│   ├── statistics.py        # aggregates + history snapshots
│   ├── blacklist.py         # hot-reloaded file-backed blacklists
│   ├── logbook.py           # structured logging + ring buffer
│   ├── settings_manager.py  # DB-backed hot-reloadable settings
│   ├── scheduler.py         # non-overlapping 15-min scheduler
│   └── collector.py         # orchestrates a full run
├── npvt/                    # ISOLATED .npvt pipeline (nothing in core/ is touched)
│   ├── models.py            # private ORM tables (own declarative base)
│   ├── config.py            # private DB-backed settings + 3 feature toggles
│   ├── source.py            # detect/download .npvt attachments from channels
│   ├── bot_relay.py         # Telethon: send file → click button → collect links
│   ├── ingest.py            # inject links via the reused core building blocks
│   ├── worker.py            # async queue + discovery/relay workers (retry/rate-limit)
│   ├── service.py           # facade singleton (start/stop/state/settings)
│   └── api.py               # /api/npvt/* router
└── dashboard/               # Jinja template + vanilla JS/CSS dashboard
```

## NPVT pipeline (`.npvt` → bot relay → V2Ray links)

A **fully isolated** add-on that turns proprietary `.npvt` files posted in your
monitored channels into V2Ray links and feeds them into the *existing* pipeline.
Nothing under `app/core/` is modified — the module reuses the parser, fingerprint,
blacklist, cooldown, pool, subscription and GitHub-publish building blocks and
keeps its own settings, tables and background worker. Any failure here is
contained and **never affects the core collector**.

```
.npvt in channel ─▶ download ─▶ @DickiriptorBot ─▶ auto-click button ─▶
   collect every V2Ray link (across batches) ─▶ existing pipeline (dedup/TCP/pool/publish)
```

**Bot interaction.** The file is sent over the *same* user session used for
collection (no second login). The bot replies with an inline keyboard; the
module clicks the button whose text matches a configurable pattern
(e.g. *"Get V2Ray link"* / *«لینک ویتوریشو بده»*, with Persian-diacritic-aware
matching), falling back to the **second** button only when no text matches. It
then collects **all** returned links — across however many message batches the
bot sends, including links inside attached `.txt` documents — until a quiet
period or the collection window elapses.

**Three independent on/off toggles** (NPVT dashboard page, applied live):

| Toggle | Controls |
|--------|----------|
| NPVT collection | detecting & downloading `.npvt` files from channels |
| Bot relay | forwarding files to the bot and driving its buttons |
| Link collection | injecting the bot's links into the active pipeline |

**Robustness:** content/file/link de-duplication, per-job retry with backoff,
per-relay and overall timeouts, a jittered send rate-limiter, configurable relay
concurrency, text-based (not index-based) button selection, and restart-safe
resume (files are re-fetched by id). All knobs live on the NPVT page and persist
in the DB. **No extra deployment config** — it reuses the existing Telegram
session, so `.env` is unchanged.

**Queue cleanup before relay.** The queue is treated as *storage, not a forced
execution list*: right before a `.npvt` file is sent to the bot it is
re-screened and dropped if it is already processed, a duplicate (by content
hash), or its source channel was removed/disabled — so unwanted files never
reach the bot even if they sit in the queue (surfaced as the "Filtered" stat).

**CAPTCHA back-off.** The bot eventually challenges automation with a numeric
image CAPTCHA. The module **does not solve it** — solving an anti-abuse control
invites escalation/bans. Instead it *detects* the challenge (configurable text
patterns, Persian-aware), keeps any links already collected, and trips a
**circuit breaker**: the relay pauses with an escalating cooldown
(`base · 2ⁿ`, capped), and after `captcha_max_consecutive` consecutive hits it
**auto-disables Bot relay** and flags it on the NPVT page. A clean relay resets
the streak. The first line of defense is the jittered pacing
(`relay_min_interval_seconds` + `relay_jitter_seconds`) — spacing sends out
keeps you under the bot's threshold so the CAPTCHA rarely fires. When it pauses,
let the bot cool down and re-enable relay from the dashboard.

REST surface (under the dashboard auth guard): `GET /api/npvt/state`,
`GET|PUT /api/npvt/settings`, `POST /api/npvt/scan`, `GET /api/npvt/files`,
`POST /api/npvt/files/{id}/retry`.

## Deploying to a VPS

The same `docker compose up -d` works on any Linux VPS. Put it behind a reverse
proxy (Caddy/Nginx) for TLS, and enable `DASHBOARD_USER`/`DASHBOARD_PASSWORD`.
No architectural changes are needed between Docker Desktop and the VPS.
