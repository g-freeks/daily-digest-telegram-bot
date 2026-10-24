#!/usr/bin/env python3
"""Run the digest or weather report by hand, without Telegram (Phase 1 spike,
and handy for debugging a config change).

    python3 app/cli.py digest --since 2026-10-20T07:30 --until 2026-10-23T07:30
    python3 app/cli.py digest --hours 24 --collect-only
    python3 app/cli.py digest --scheduled        # like the 07:30 job: uses and updates /data state
    python3 app/cli.py weather
    python3 app/cli.py weather --post            # also send it to allowed_chat_ids
    python3 app/cli.py validate

Env: CONFIG_PATH (default ./config.json), DATA_DIR (default ./data),
TELEGRAM_BOT_TOKEN (only for --post). --post is how to trigger a run on demand
when the bot is in send-only mode (telegram.receive_updates: false).
Without --scheduled, runs are dry: no window state, no dedupe, nothing recorded.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import digest
import weather
from config_store import ConfigStore
from formatting import Outcome
from state import State


def parse_local(value: str, tz) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


async def post(config: dict, outcome: Outcome) -> None:
    from telegram import Bot

    import bot

    async with Bot(os.environ["TELEGRAM_BOT_TOKEN"]) as telegram_bot:
        for chat_id in config["allowed_chat_ids"]:
            await bot.post_outcome(telegram_bot, chat_id, outcome)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_digest = sub.add_parser("digest")
    p_digest.add_argument("--since", help="window start, ISO (schedule timezone if no offset)")
    p_digest.add_argument("--until", help="window end, ISO (default: now)")
    p_digest.add_argument("--hours", type=float, help="window = the last N hours")
    p_digest.add_argument("--collect-only", action="store_true", help="print the candidates, don't call Claude")
    p_digest.add_argument("--scheduled", action="store_true", help="behave like the scheduled job (state + dedupe)")
    p_digest.add_argument("--post", action="store_true", help="also send the result to allowed_chat_ids")
    p_weather = sub.add_parser("weather")
    p_weather.add_argument("--post", action="store_true", help="also send the result to allowed_chat_ids")
    sub.add_parser("validate")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config_path = Path(os.environ.get("CONFIG_PATH", "config.json"))
    state = State(Path(os.environ.get("DATA_DIR", "data")))
    config, error = ConfigStore(config_path).load()
    if error:
        print(error, file=sys.stderr)
        return 1
    if args.command == "validate":
        print(f"{config_path} OK")
        return 0

    now = datetime.now(timezone.utc)
    tz = digest.schedule_tz(config)

    if args.command == "weather":
        outcome = weather.run_weather(config, state, now, retry_delay_seconds=None)
    else:
        window = None
        if args.since:
            until = parse_local(args.until, tz) if args.until else now
            window = (parse_local(args.since, tz), until)
        dry_run = not args.scheduled
        if args.collect_only:
            prepared = digest.prepare(config, state, now, dry_run=dry_run, hours=args.hours, window=window)
            print(json.dumps(prepared.candidates, indent=1, ensure_ascii=False))
            print(digest.stats_line(prepared) + digest.source_error_note(prepared), file=sys.stderr)
            return 0
        outcome = digest.run_digest(config, state, now, dry_run=dry_run, hours=args.hours, window=window,
                                    retry_delay_seconds=None)

    print(outcome.message)
    if args.post:
        asyncio.run(post(config, outcome))
    if outcome.archive_path:
        print(f"\n(archived: {outcome.archive_path})", file=sys.stderr)
    return 1 if outcome.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
