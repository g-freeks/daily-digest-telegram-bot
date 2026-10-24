# Daily Digest Bot

A personal Telegram bot that sends one short message every morning (07:30
Europe/Stockholm by default):

- **Mon–Fri:** a topic digest. Out of the box it covers newly disclosed
  vulnerabilities, exploits and malicious packages in Node.js and popular npm
  packages, each with the fixed version and the exact upgrade command. When
  there's nothing worth acting on, it sends a fixed one-liner instead.
- **Sat–Sun:** a weather report for a configured location (SMHI, with MET
  Norway as fallback).

Python collects candidates from public feeds and pre-filters them. The
[Claude Code](https://docs.claude.com/en/docs/claude-code) CLI (`claude -p`)
then verifies them and writes the digest. If no candidates survive, Claude
isn't called at all. The topic lives entirely in `config.json`, so pointing
the digest at something else (PyPI, Rust crates, any RSS feed) needs no code
change.

This is a hobby project shared as-is, not a supported product.

## How it works

```
GitHub advisories ─┐
RSS / Atom feeds ──┼─> filter (severity, watchlist, npm downloads)
Hacker News ───────┘     -> dedupe against already-reported items
                           -> none left?  post the empty message
                           -> otherwise   claude -p (WebFetch/WebSearch only) -> Telegram
```

- The window runs from the previous digest to now, so Monday's digest covers
  the weekend.
- Long digests become a headline message plus the full text as a Markdown
  attachment.
- Each run's Claude cost is logged. A digest is typically $0.15–0.30, a
  weather report a few cents.

## Requirements

- Docker with Compose
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Claude subscription token: run `claude setup-token`

## Setup

1. `cp env.example .env` and fill in `TELEGRAM_BOT_TOKEN` and
   `CLAUDE_CODE_OAUTH_TOKEN`. `GITHUB_TOKEN` is optional (no scopes needed)
   and only raises the GitHub API rate limit.
2. `cp config/config.json config.json`. The real config is gitignored. Edit:
   - `allowed_chat_ids`: your Telegram user id. In a private chat, the chat
     id is the user id. With `receive_updates: true`, send the bot a message
     and it logs your chat id.
   - `weather.location`
   - `telegram.receive_updates`: see below
3. `docker compose up -d --build`.

### Portainer

Use a "Repository" stack pointing at this repo; it deploys
`docker-compose.yml`, which only pulls the image. Portainer resolves relative
paths against its own clone of the repo, so keep your files in a host
directory and point the stack at it:

1. On the host, create a writable directory, e.g.
   `/mnt/<pool>/apps/daily-digest` on TrueNAS (where `/opt` is read-only),
   and put your `config.json` in it. `data/` and `claude-home/` are created
   on first start.
2. In the stack's **Environment variables**, set `APP_DIR` to that directory,
   `TELEGRAM_BOT_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN`, plus `GITHUB_TOKEN` if
   you have one.

If `config.json` is missing, the stack fails with `bind source path does not
exist` instead of starting with a broken config. On first start the
entrypoint makes these files owned by uid 1000 (the container's user), so
editing `config.json` on the host may then need `sudo`.

### Notes

`docker-compose.yml` pulls the CI-built image from GHCR. The auto-loaded
`docker-compose.override.yml` adds `build: .` for local builds. The entrypoint
fixes ownership of `./data`, `./claude-home` and `config.json`, then drops to
an unprivileged user, so no host-side `chown` is needed.

### Own token or shared token

Telegram allows only one process to poll a bot token for incoming messages.

- **`receive_updates: true`:** the bot has its own token, polls for messages,
  and the commands below work.
- **`receive_updates: false`:** send-only. Use this to reuse a token that
  another bot already polls. Scheduled posts still arrive, but chat commands
  don't. Trigger runs with `cli.py … --post` instead (see below).

## Commands

Commands need `receive_updates: true`.

| Command | What it does |
|---|---|
| `/digest [hours]` | Run the topic digest now. Dry run: no window state, no dedupe, nothing recorded |
| `/weather` | Post the weather report now |
| `/config` | Show the active topic, sources and filters |
| `/reload` | Re-read `config.json`, report errors, reschedule if `schedule` changed |
| `/cost` | Claude cost per kind, last 30 days and all-time |
| `/help` | Command list |

## Running by hand

```bash
uv venv .venv && uv pip install -p .venv -r requirements.txt
.venv/bin/python app/cli.py validate
.venv/bin/python app/cli.py digest --hours 24 --collect-only   # candidates only, no Claude
.venv/bin/python app/cli.py digest --since 2026-10-21T07:30 --until 2026-10-23T07:30
.venv/bin/python app/cli.py weather
```

- CLI runs are dry unless you pass `--scheduled`.
- `--post` also sends the result to `allowed_chat_ids` (needs
  `TELEGRAM_BOT_TOKEN` in the environment).
- Defaults are `./config.json` and `./data`; override them with
  `CONFIG_PATH` / `DATA_DIR`.
- Inside the container: `docker compose exec bot python3 /app/cli.py weather --post`.

## Changing the topic

Edit `topic` in `config.json`: `prompt`, `empty_message`, `sources` and
`filters`. The next run picks it up, or `/reload` if commands are enabled.

| Source `type` | Params |
|---|---|
| `github_advisories` | `ecosystem` (`npm`, `pip`, `rust`, …), `types` (default `["reviewed", "malware"]`), `max_pages` (default 10) |
| `rss` | `url` (RSS 2.0 or Atom) |
| `hn_algolia` | `queries` (list), `min_points` (default 0) |

Filters are all optional, and only apply to items that carry the relevant data:

- `min_severity`: drops advisories below `low`/`medium`/`high`/`critical`.
  Items tagged with an `always_include` tag (`malicious_package`,
  `known_exploited`) bypass it.
- `packages`: keeps an advisory only if a package is on the `watchlist` or has
  at least `min_weekly_downloads` weekly npm downloads.
  - This applies to `always_include` items too, because npm gets dozens of
    malware advisories a day for packages nobody installs.
  - Download counts come from npm only. For other ecosystems the count is
    unknown and the item passes, so rely on `min_severity` and the prompt
    there.

News items from RSS and HN aren't pre-filtered; Claude judges them against the
prompt.

## Security

- Only allowlisted chats are answered. Everything else is ignored and logged.
- Claude runs with only WebFetch and WebSearch (`--tools`). It has no file,
  shell or edit tools, and never resumes a session.
- Feed content is untrusted. It's passed to Claude as escaped data, never as
  instructions.
- The container runs as non-root with a read-only root filesystem,
  `cap_drop: ALL` (plus the four capabilities the entrypoint needs to hand off
  to `node`), a tmpfs `/tmp`, and memory/CPU limits.

## License

MIT, see [LICENSE](LICENSE).
