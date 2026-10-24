"""Telegram bot: allowlisted private chat, 07:30 scheduled jobs (Mon–Fri topic
digest, Sat–Sun weather), manual runs, oversized-result attachments.
See CLAUDE.md for the full contract.

With `telegram.receive_updates: false` the bot never calls getUpdates and only
sends the scheduled posts. That lets it share a token with another bot process
(only one poller per token is allowed); commands are then unavailable and
`cli.py ... --post` replaces /digest and /weather.
"""
import asyncio
import logging
import os
import signal
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import digest
import formatting
import render_config
import weather
from config_store import ConfigStore
from state import State

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config/config.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

# python-telegram-bot >= 20: 0 = Sunday ... 6 = Saturday.
DIGEST_DAYS = (1, 2, 3, 4, 5)
WEATHER_DAYS = (6, 0)
DIGEST_JOB = "digest"
WEATHER_JOB = "weather"

HELP_TEXT = (
    "I post a digest at the scheduled time: the topic digest Mon–Fri, the weather Sat–Sun.\n\n"
    "Commands:\n"
    "/digest [hours] — run the topic digest now (dry run: ignores the window state and dedupe; "
    "default window is since the previous weekday slot)\n"
    "/weather — post the weather report now\n"
    "/config — show the active topic, sources and filters\n"
    "/reload — re-read config.json, report errors, reschedule if the time changed\n"
    "/cost — show Claude cost so far\n"
    "/help — show this message"
)


def check_allowed(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Tuple[bool, dict, Optional[str]]:
    config, config_error = context.bot_data["config_store"].load()
    return chat_id in set(config.get("allowed_chat_ids", [])), config, config_error


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    """Returns the config if the chat is allowlisted, else logs and returns None."""
    message = update.effective_message
    if message is None:
        return None
    allowed, config, config_error = check_allowed(context, message.chat_id)
    if not allowed:
        user = update.effective_user
        logger.info("ignoring update from non-allowlisted chat_id=%s user=%s text=%r",
                    message.chat_id, user.username if user else None, (message.text or "")[:50])
        return None
    if config_error:
        await message.reply_text(f"Config error, using last good config:\n{config_error[:3500]}")
    return config


async def post_outcome(bot: Bot, chat_id: int, outcome: formatting.Outcome) -> None:
    text, needs_attachment = formatting.split_for_telegram(outcome.message)
    await bot.send_message(chat_id=chat_id, text=text)
    if needs_attachment and outcome.archive_path:
        with outcome.archive_path.open("rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename=outcome.archive_path.name)


async def run_and_post(context: ContextTypes.DEFAULT_TYPE, chat_ids, kind: str, func, *args, **kwargs) -> None:
    # One run at a time: a manual /digest during the scheduled one would
    # otherwise race on state.json / reported.json.
    async with context.bot_data["run_lock"]:
        try:
            outcome = await asyncio.to_thread(func, *args, **kwargs)
        except Exception as exc:
            logger.exception("%s run crashed", kind)
            outcome = formatting.Outcome(f"{kind.capitalize()} run crashed: {exc!s:.500}", failed=True)
    for chat_id in chat_ids:
        try:
            await post_outcome(context.bot, chat_id, outcome)
        except Exception:
            logger.exception("could not post %s to chat %s", kind, chat_id)


async def scheduled_run(context: ContextTypes.DEFAULT_TYPE, kind: str, func) -> None:
    config, config_error = context.bot_data["config_store"].load()
    if not config:
        logger.error("scheduled %s skipped, no usable config: %s", kind, config_error)
        return
    chat_ids = config["allowed_chat_ids"]
    if config_error:
        for chat_id in chat_ids:
            await context.bot.send_message(chat_id, f"Config error, using last good config:\n{config_error[:3500]}")
    await run_and_post(context, chat_ids, kind, func, config, context.bot_data["state"], datetime.now(timezone.utc))


async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    await scheduled_run(context, "digest", digest.run_digest)


async def scheduled_weather(context: ContextTypes.DEFAULT_TYPE) -> None:
    await scheduled_run(context, "weather", weather.run_weather)


def _next_run(job, at: time) -> str:
    # Job.next_t only exists once the scheduler runs, which is after post_init.
    next_fire = job.job.trigger.get_next_fire_time(None, datetime.now(at.tzinfo))
    return f"{next_fire:%a %Y-%m-%d %H:%M %Z}"


def schedule_jobs(application: Application, config: dict) -> str:
    """(Re)creates both daily jobs from config.schedule; returns a description."""
    job_queue = application.job_queue
    for name in (DIGEST_JOB, WEATHER_JOB):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    hour, minute = map(int, config["schedule"]["time"].split(":"))
    # Explicit tz so DST and the server clock don't shift the run time.
    at = time(hour, minute, tzinfo=ZoneInfo(config["schedule"]["timezone"]))
    digest_job = job_queue.run_daily(scheduled_digest, at, days=DIGEST_DAYS, name=DIGEST_JOB)
    weather_job = job_queue.run_daily(scheduled_weather, at, days=WEATHER_DAYS, name=WEATHER_JOB)
    application.bot_data["scheduled_at"] = (config["schedule"]["time"], config["schedule"]["timezone"])

    description = f"digest next at {_next_run(digest_job, at)}, weather next at {_next_run(weather_job, at)}"
    logger.info("scheduled: %s", description)
    return description


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = await guard(update, context)
    if not config:
        return
    message = update.effective_message
    hours = None
    if context.args:
        try:
            hours = float(context.args[0])
            if not 0 < hours <= 24 * 7:
                raise ValueError
        except ValueError:
            await message.reply_text("Usage: /digest [hours], with hours between 0 and 168")
            return
    await message.reply_text("Running the digest (dry run)…")
    await run_and_post(context, [message.chat_id], "digest", digest.run_digest,
                       config, context.bot_data["state"], datetime.now(timezone.utc),
                       dry_run=True, hours=hours, retry_delay_seconds=None)


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = await guard(update, context)
    if not config:
        return
    await update.effective_message.reply_text("Fetching the weather…")
    await run_and_post(context, [update.effective_message.chat_id], "weather", weather.run_weather,
                       config, context.bot_data["state"], datetime.now(timezone.utc), retry_delay_seconds=None)


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = await guard(update, context)
    if not config:
        return
    text = render_config.describe_config(config)
    await update.effective_message.reply_text(formatting.split_for_telegram(text)[0])


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    allowed, config, config_error = check_allowed(context, message.chat_id)
    if not allowed:
        logger.info("ignoring /reload from non-allowlisted chat_id=%s", message.chat_id)
        return
    if config_error:
        await message.reply_text(f"Reload failed, keeping last good config:\n{config_error[:3500]}")
        return

    reply = (f"Config reloaded OK: topic '{config['topic']['name']}', "
             f"{len(config['topic']['sources'])} source(s), "
             f"{len(config['allowed_chat_ids'])} allowed chat(s).")
    if context.bot_data.get("scheduled_at") != (config["schedule"]["time"], config["schedule"]["timezone"]):
        reply += "\nRescheduled: " + schedule_jobs(context.application, config)
    await message.reply_text(reply)


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    totals = {}
    for entry in context.bot_data["state"].cost_entries():
        kind = entry.get("kind", "?")
        cost = entry.get("cost_usd") or 0.0
        recent = datetime.fromisoformat(entry["timestamp"]) >= cutoff
        t = totals.setdefault(kind, [0.0, 0, 0.0, 0])
        t[0] += cost
        t[1] += 1
        if recent:
            t[2] += cost
            t[3] += 1
    if not totals:
        await update.effective_message.reply_text("No Claude runs logged yet.")
        return
    lines = [f"{kind}: ${t[2]:.2f} over {t[3]} run(s) in the last 30 days, ${t[0]:.2f} over {t[1]} all-time"
             for kind, t in sorted(totals.items())]
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def ignore_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # No follow-up questions: everything that isn't a command is ignored, but
    # strangers are still logged (that's also how the owner finds their chat id
    # for allowed_chat_ids: send /start and read the log).
    await guard(update, context)


async def post_init(application: Application) -> None:
    config, config_error = application.bot_data["config_store"].load()
    if not config:
        raise SystemExit(f"cannot start without a valid config: {config_error}")
    schedule_jobs(application, config)


def receives_updates(config: dict) -> bool:
    return config.get("telegram", {}).get("receive_updates", True)


async def run_send_only(application: Application) -> None:
    """Runs the job queue without ever polling Telegram for updates."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with application:
        await post_init(application)
        await application.start()
        logger.info("send-only mode: not polling for updates, commands are disabled")
        await stop.wait()
        await application.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config_store = ConfigStore(CONFIG_PATH)
    config, config_error = config_store.load()
    if not config:
        raise SystemExit(f"cannot start without a valid config: {config_error}")

    builder = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).post_init(post_init)
    if not receives_updates(config):
        builder = builder.updater(None)
    application = builder.build()
    application.bot_data["config_store"] = config_store
    application.bot_data["state"] = State(DATA_DIR)
    application.bot_data["run_lock"] = asyncio.Lock()

    application.add_handler(CommandHandler("digest", cmd_digest))
    application.add_handler(CommandHandler("weather", cmd_weather))
    application.add_handler(CommandHandler("config", cmd_config))
    application.add_handler(CommandHandler("reload", cmd_reload))
    application.add_handler(CommandHandler("cost", cmd_cost))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(MessageHandler(filters.ALL, ignore_other))

    if receives_updates(config):
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    else:
        asyncio.run(run_send_only(application))


if __name__ == "__main__":
    main()
