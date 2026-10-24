# Daily Digest Bot

A personal Telegram bot that posts a short digest at 07:30 Europe/Stockholm in
a private chat with its owner:

- **Mon–Fri:** a topic digest. The topic is defined entirely in `config.json`
  (prompt, sources, filters); the current one is newly disclosed
  vulnerabilities, exploits and malicious packages in Node.js and major npm
  packages. If nothing qualifies it posts `topic.empty_message` verbatim.
- **Sat–Sun:** a weather report for `weather.location`.

It wraps the Claude Code CLI (`claude -p`) in a hardened Docker container on a
home server (x86). User-facing setup lives in `README.md`.

Sister project of `listings-telegram-bot`: the infrastructure (Dockerfile,
entrypoint, hardened compose, runner, config store, CI) was copied from it, not
shared as a library. Revisit that only if a third bot appears.

## Layout

| File | Role |
|---|---|
| `app/bot.py` | Telegram: allowlist, the two `run_daily` jobs, commands, send-only mode |
| `app/cli.py` | Same runs without Telegram (`digest`, `weather`, `validate`, `--post`) |
| `app/digest.py` | Mon–Fri pipeline: window, collect, filter, dedupe, Claude, record |
| `app/weather.py` | Sat–Sun: SMHI (fallback MET Norway) forecast, then Claude with no tools |
| `app/collectors.py` | Source types (`github_advisories`, `rss`, `hn_algolia`) + their validation |
| `app/filters.py` | `topic.filters` (severity, watchlist, npm weekly downloads) |
| `app/render_config.py` | System prompts and the `/config` summary |
| `app/runner.py` | The `claude -p` subprocess contract |
| `app/config_store.py` | Load + validate `config.json`, keep the last good one |
| `app/state.py` | `/data` state: window, reported IDs, archive, cost log |
| `app/formatting.py` | `Outcome` + splitting for Telegram's 4,096-char limit |
| `config/config.json` | Tracked example config. The real `./config.json` is gitignored |

## Decisions

- **Python service wrapping `claude -p`** as a subprocess. Every run is fresh
  (`--no-session-persistence`, no `--resume`): no follow-ups, no sessions map.
- **Telegram:** `python-telegram-bot[job-queue]==22.8`.
- **Scheduling:** in-process `JobQueue.run_daily` with an explicit
  `ZoneInfo(schedule.timezone)`, so DST and the server clock don't shift it.
  It is not host cron. Two jobs: days `(1..5)` digest and `(6, 0)` weather.
  PTB ≥ 20 numbers days 0 = Sunday. The image installs `tzdata`, which
  `node:22-slim` lacks.
- **Send-only mode** (`telegram.receive_updates: false`), currently on. Only one
  process may long-poll `getUpdates` per token (a second gets `409 Conflict`).
  The owner chose to share the token with the listings bot, which owns
  polling.
  - In this mode the app is built with `updater(None)` and only sends.
  - Commands are unavailable; `cli.py digest|weather --post` replaces
    `/digest` and `/weather`.
  - Never call `getUpdates` with a shared token by hand: it confirms and
    swallows updates meant for the other bot.
  - With its own token, set `receive_updates: true` to get the commands back.
- **Chat:** the private 1:1 chat with the owner. `allowed_chat_ids` holds its
  id, a positive number equal to the owner's Telegram user id. Everything else
  is ignored and logged.
- **Auth:** subscription token (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`).
- **Config:** `./config.json`, mounted read-write and re-read on every run.
  Validation rejects unknown source types, missing prompts, bad URLs, etc.; the
  bot then keeps the last good config and reports the error in chat. No
  topic-specific wording is hard-coded in Python.
- **No browser** (Playwright/Chromium): the image stays small, 512m is plenty.

## Topic digest pipeline (Mon–Fri)

Python collects candidates first; Claude only filters, verifies and writes.
Zero candidates means Claude isn't called at all. This keeps the empty case
free and reliable, and stops Claude from inventing items.

1. **Window:** from `last_digest_at` in `/data/state.json` to now. With no
   state, it starts at the previous weekday slot, so Monday covers Fri 07:30 to
   Mon 07:30. The window is capped at 7 days.
2. **Collect:** every configured source. A failing source doesn't sink the
   others and is noted under the digest. If every source fails, an error is
   posted instead of the empty message.
   - `github_advisories`: `api.github.com/advisories` with a `published`
     range. GitHub only returns reviewed non-malware advisories unless `type`
     is given, so each of `types` (default reviewed + malware) is its own
     query. Optional `GITHUB_TOKEN` raises the 60/h rate limit.
   - `rss`: RSS 2.0 or Atom, parsed with `defusedxml`. Current feed:
     `nodejs.org/en/feed/vulnerability.xml`.
   - `hn_algolia`: `search_by_date` per query with `created_at_i` bounds and
     an optional `min_points`.
3. **Filter:** apply `topic.filters`, only to items that carry the data.
   - `always_include` tags bypass `min_severity` only, not the package
     filter: npm gets ~35 malware advisories a day, nearly all
     dependency-confusion squats with zero downloads.
   - Download counts come from npm's API: bulk for unscoped names, one call
     per scoped name. An unknown count (failed lookup, non-npm package) fails
     open.
   - No collector sets `known_exploited` yet; that needs a `cisa_kev` source.
4. **Dedupe:** drop IDs/URLs already in `/data/reported.json` (kept 180 days).
5. **Empty:** post `topic.empty_message` and advance the window.
6. **Otherwise:** run `claude -p` with the candidates as JSON inside
   `<candidates>`, with `<` escaped so the data can't close the tag. Claude
   writes a `## Headlines` list, then details. If nothing is relevant it
   outputs exactly the empty message. It ends with `REPORTED: id, id`, a line
   that is stripped before posting; those IDs and their URLs are recorded. If
   the line is missing, all candidate IDs are recorded.
7. **Archive:** save to `/data/reports/YYYY-MM-DD.md`, then advance the
   window. A failed run leaves the window alone, so the next run covers it.

Dry runs (`/digest`, CLI without `--scheduled`) ignore window state and dedupe
and record nothing.

## Weather (Sat–Sun)

- Source: SMHI open data `category/snow1g/version/1` point forecast; the
  older `pmp3g/version/2` returns 404. Fallback: MET Norway
  `locationforecast/2.0/compact`, which needs a descriptive `User-Agent`.
- Python reduces the forecast to today and tomorrow (local time) with
  per-day aggregates. Claude, with no tools, writes it up per
  `weather.prompt`.
- If both sources fail, the bot posts a short error instead of staying silent.

## Runner contract

```bash
claude -p "$PROMPT" --output-format json \
  --append-system-prompt "$RENDERED_PROMPT" \
  --allowedTools "WebFetch WebSearch" --tools "WebFetch WebSearch" \
  --max-turns "$MAX_TURNS" \
  --settings /app/claude_settings.json --permission-prompts none \
  --no-session-persistence
```

- `--tools` makes every other built-in tool unavailable, not just unapproved.
  An adversarial prompt confirmed Claude can't read files or run commands.
- Weather uses `--allowedTools "" --tools ""` and `runtime.weather_max_turns`
  (default 3).
- Hard timeout is `runtime.timeout_seconds`. On failure, scheduled runs retry
  once after 5 minutes, then post a short error. Never fail silently.
- Results over 4,096 chars: the headline list as the message, the full
  Markdown as an attachment.
- `total_cost_usd` is appended to `/data/cost_log.jsonl` (for `/cost`).
  Typical cost: digest ≈ $0.15–0.30, weather ≈ $0.03–0.10.

## Volumes

| Host | Container | Purpose |
|---|---|---|
| `./config.json` | `/app/config/config.json` | real config, gitignored |
| `./data` | `/data` | `state.json`, `reported.json`, `reports/`, `cost_log.jsonl`, `work/` |
| `./claude-home` | `/home/node` (whole $HOME) | `~/.claude/` and `~/.claude.json`, writable under `read_only: true` |
| `.env` | env vars | `TELEGRAM_BOT_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, optional `GITHUB_TOKEN` |

`docker-entrypoint.sh` starts as root, chowns `/data`, `/home/node` and the
config file to `node`, then drops to `node` via `gosu`. Side effect: those
host paths end up owned by uid 1000.

## Security rules (do not relax without asking)

- Allowlist chat IDs; ignore and log everything else.
- No Bash/Write/Edit/Read for Claude (`--tools`); working dir `/data/work`.
- Source content (advisories, RSS, HN) is untrusted. It goes to Claude as data,
  never as instructions, and can't choose tools or anything beyond
  WebFetch/WebSearch.
- `claude_settings.json` denies `Read(//app/config/**)` and `Read(//proc/**)`
  as defence in depth. The double leading slash is required; the single-slash
  form silently fails.
- httpx logging is at WARNING: at INFO it logs Telegram URLs, which contain the
  bot token.
- Container: non-root `node`, read-only root FS, `cap_drop: ALL` plus only
  `CHOWN, DAC_OVERRIDE, SETUID, SETGID` for the entrypoint handoff, `/tmp`
  tmpfs, 512m / 1 CPU.
- Pin the Claude Code version in the Dockerfile.
- `docker-compose.yml` stays pull-only
  (`ghcr.io/g-freeks/daily-digest-telegram-bot`) for Portainer; `build: .`
  lives in `docker-compose.override.yml`.

## Status and open gates

- Done:
  - CLI runs for a real window, the empty case and a topic swap through
    config only
  - hardened container run
  - send-only startup and a `--post` delivery
- Open:
  - both scheduled posts arriving at 07:30 in production
  - surviving a container restart
  - Monday covering the weekend
- Not built: a `cisa_kev` / `osv` collector, and catch-up for a slot missed
  while the container was down.
