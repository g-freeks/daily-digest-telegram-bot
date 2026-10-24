"""Persistent state under /data: the last digest run (window start for the
next one), the IDs/URLs already reported (dedupe across days), the digest
archive and the cost log."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

REPORTED_RETENTION_DAYS = 180


def _load(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


class State:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_path = data_dir / "state.json"
        self.reported_path = data_dir / "reported.json"
        self.reports_dir = data_dir / "reports"
        self.cost_log_path = data_dir / "cost_log.jsonl"
        self.work_dir = data_dir / "work"

    def last_digest_at(self) -> Optional[datetime]:
        value = _load(self.state_path, {}).get("last_digest_at")
        return datetime.fromisoformat(value) if value else None

    def set_last_digest_at(self, when: datetime) -> None:
        state = _load(self.state_path, {})
        state["last_digest_at"] = when.astimezone(timezone.utc).isoformat()
        _save(self.state_path, state)

    def reported_keys(self) -> set:
        return set(_load(self.reported_path, {}))

    def mark_reported(self, keys: Iterable[str], when: datetime) -> None:
        reported = _load(self.reported_path, {})
        stamp = when.astimezone(timezone.utc).isoformat()
        for key in keys:
            if key:
                reported.setdefault(key, stamp)
        cutoff = when - timedelta(days=REPORTED_RETENTION_DAYS)
        reported = {k: v for k, v in reported.items() if datetime.fromisoformat(v) >= cutoff}
        _save(self.reported_path, reported)

    def archive_report(self, name: str, text: str) -> Path:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        path = self.reports_dir / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def log_cost(self, kind: str, result: dict) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "session_id": result.get("session_id"),
            "cost_usd": result.get("total_cost_usd"),
            "num_turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms"),
        }
        self.cost_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cost_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def cost_entries(self) -> list:
        entries = []
        if self.cost_log_path.exists():
            with self.cost_log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries
