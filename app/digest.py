"""Topic digest pipeline (Mon–Fri), see CLAUDE.md:

window -> collect -> filter -> dedupe -> (empty: post empty_message, no Claude)
       -> claude -p writes the digest -> record reported IDs + archive.

Blocking code; the bot runs it via asyncio.to_thread.
"""
import json
import logging
import re
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import collectors
import filters
import render_config
import runner
from formatting import Outcome
from state import State

logger = logging.getLogger(__name__)

DIGEST_WEEKDAYS = (0, 1, 2, 3, 4)  # datetime.weekday(): Mon–Fri
MAX_WINDOW = timedelta(days=7)
ALLOWED_TOOLS = "WebFetch WebSearch"

_REPORTED_RE = re.compile(rf"^\s*{re.escape(render_config.REPORTED_MARKER)}(.*)$", re.MULTILINE)


@dataclass
class Prepared:
    since: datetime
    until: datetime
    candidates: List[dict]
    collected: int
    filtered_out: int
    deduped: int
    source_errors: List[str] = field(default_factory=list)


@dataclass
class Written:
    text: str
    empty: bool
    reported_ids: List[str]
    result: dict


def schedule_tz(config: dict) -> ZoneInfo:
    return ZoneInfo(config["schedule"]["timezone"])


def previous_slot(config: dict, before: datetime, weekdays=DIGEST_WEEKDAYS) -> datetime:
    """The latest scheduled run time at or before `before` on one of `weekdays`."""
    tz = schedule_tz(config)
    hour, minute = map(int, config["schedule"]["time"].split(":"))
    local = before.astimezone(tz)
    for days_back in range(8):
        day = (local - timedelta(days=days_back)).date()
        slot = datetime.combine(day, time(hour, minute), tzinfo=tz)
        if day.weekday() in weekdays and slot <= local:
            return slot
    raise AssertionError("no slot in the past week")


def default_since(config: dict, until: datetime) -> datetime:
    # The slot before the current one: Monday 07:30 -> Friday 07:30.
    return previous_slot(config, until - timedelta(hours=12))


def compute_window(config: dict, state: State, now: datetime, *, dry_run: bool, hours: Optional[float] = None,
                   window: Optional[Tuple[datetime, datetime]] = None):
    if window:
        return window
    if hours:
        return now - timedelta(hours=hours), now
    since = None if dry_run else state.last_digest_at()
    if since is None:
        since = default_since(config, now)
    if now - since > MAX_WINDOW:
        logger.warning("window since %s exceeds %s, capping", since, MAX_WINDOW)
        since = now - MAX_WINDOW
    return since, now


def _dedupe_key_hit(item: dict, reported: set) -> bool:
    return item["id"] in reported or (item.get("url") and item["url"] in reported)


def prepare(config: dict, state: State, now: datetime, *, dry_run: bool = False, hours: Optional[float] = None,
            window: Optional[Tuple[datetime, datetime]] = None) -> Prepared:
    topic = config["topic"]
    since, until = compute_window(config, state, now, dry_run=dry_run, hours=hours, window=window)
    logger.info("digest window %s .. %s (dry_run=%s)", since, until, dry_run)

    items, source_errors = collectors.collect_all(topic["sources"], since, until)

    unique, seen = [], set()
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)

    kept, filtered_out = filters.apply_filters(unique, topic.get("filters") or {})

    deduped = 0
    if not dry_run:
        reported = state.reported_keys()
        fresh = [item for item in kept if not _dedupe_key_hit(item, reported)]
        deduped = len(kept) - len(fresh)
        kept = fresh

    logger.info("collected %d, filtered out %d, already reported %d, candidates %d",
                len(unique), filtered_out, deduped, len(kept))
    return Prepared(since, until, kept, len(unique), filtered_out, deduped, source_errors)


def build_prompt(prepared: Prepared, tz) -> str:
    # Escape "<" so candidate text can't close the <candidates> wrapper.
    payload = json.dumps(prepared.candidates, indent=1, ensure_ascii=False).replace("<", "\\u003c")
    return (
        f"Write today's digest from the {len(prepared.candidates)} candidate item(s) below, "
        f"collected between {prepared.since.astimezone(tz):%Y-%m-%d %H:%M} and "
        f"{prepared.until.astimezone(tz):%Y-%m-%d %H:%M %Z}.\n\n"
        f"<candidates>\n{payload}\n</candidates>"
    )


def write(config: dict, state: State, prepared: Prepared) -> Written:
    """Runs claude -p over the candidates. Raises runner.RunnerError."""
    topic = config["topic"]
    runtime = config["runtime"]
    tz = schedule_tz(config)

    result = runner.run_claude(
        build_prompt(prepared, tz),
        render_config.render_digest_system_prompt(topic, prepared.since, prepared.until, tz),
        state.work_dir,
        allowed_tools=ALLOWED_TOOLS,
        max_turns=runtime["max_turns"],
        timeout_seconds=runtime["timeout_seconds"],
    )
    state.log_cost("digest", result)
    logger.info("digest cost $%s, %s turn(s)", result.get("total_cost_usd"), result.get("num_turns"))

    raw = (result.get("result") or "").strip()
    reported_ids: List[str] = []
    match = None
    for match in _REPORTED_RE.finditer(raw):
        pass
    if match:
        reported_ids = [i.strip().strip("`") for i in match.group(1).split(",") if i.strip()]
        raw = (raw[:match.start()] + raw[match.end():]).strip()

    empty_message = topic["empty_message"]
    if raw.strip("`").strip() == empty_message:
        return Written(empty_message, True, [], result)

    candidate_ids = {c["id"] for c in prepared.candidates}
    reported_ids = [i for i in reported_ids if i in candidate_ids]
    if not reported_ids:
        # No usable marker: assume everything Claude saw was covered.
        reported_ids = sorted(candidate_ids)
    return Written(raw, False, reported_ids, result)


def record(state: State, prepared: Prepared, written: Optional[Written], now: datetime) -> None:
    """Moves the window forward and remembers what was reported. Only for
    scheduled (non-dry) runs that actually posted something."""
    if written and not written.empty:
        by_id = {c["id"]: c for c in prepared.candidates}
        keys = []
        for item_id in written.reported_ids:
            keys.append(item_id)
            if by_id.get(item_id, {}).get("url"):
                keys.append(by_id[item_id]["url"])
        state.mark_reported(keys, now)
    state.set_last_digest_at(prepared.until)


def source_error_note(prepared: Prepared) -> str:
    if not prepared.source_errors:
        return ""
    return "\n\n⚠️ Some sources failed:\n" + "\n".join(f"- {e[:300]}" for e in prepared.source_errors)


def stats_line(prepared: Prepared) -> str:
    return (f"{prepared.collected} collected, {prepared.filtered_out} filtered out, "
            f"{prepared.deduped} already reported, {len(prepared.candidates)} candidate(s)")


def run_digest(config: dict, state: State, now: datetime, *, dry_run: bool = False,
               hours: Optional[float] = None, window: Optional[Tuple[datetime, datetime]] = None,
               retry_delay_seconds: Optional[float] = 300) -> Outcome:
    """The whole Mon–Fri flow. Never raises for expected failures; returns
    what to post. `retry_delay_seconds=None` disables the retry."""
    topic = config["topic"]
    prepared = prepare(config, state, now, dry_run=dry_run, hours=hours, window=window)

    if len(prepared.source_errors) == len(topic["sources"]):
        # Every source failed: "nothing to report" would be a silent failure.
        return Outcome("Digest failed, no source could be read:\n"
                       + "\n".join(f"- {e[:300]}" for e in prepared.source_errors), failed=True)

    stamp = now.astimezone(schedule_tz(config)).strftime("%Y-%m-%d")
    name = stamp if not dry_run else f"{stamp}-dryrun-{now.astimezone(schedule_tz(config)):%H%M}"

    if not prepared.candidates:
        if not dry_run:
            record(state, prepared, None, now)
        return Outcome(topic["empty_message"] + source_error_note(prepared))

    written = None
    for attempt in (1, 2):
        try:
            written = write(config, state, prepared)
            break
        except runner.RunnerError as exc:
            logger.exception("digest attempt %d failed", attempt)
            if attempt == 2 or retry_delay_seconds is None:
                return Outcome(f"Digest failed ({len(prepared.candidates)} candidate(s) pending, "
                               f"they'll be retried in the next window): {str(exc)[:500]}", failed=True)
            time_module.sleep(retry_delay_seconds)

    archive_path = state.archive_report(name, written.text)
    if not dry_run:
        record(state, prepared, written, now)
    return Outcome(written.text + source_error_note(prepared), archive_path)
