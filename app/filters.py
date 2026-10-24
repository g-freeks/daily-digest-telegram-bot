"""Applies `topic.filters` to collected items, before Claude sees them.

- `min_severity` applies only to items that carry a severity (advisories).
  Items tagged with one of `always_include` bypass it.
- `packages` (watchlist OR min_weekly_downloads) applies only to items that
  carry packages, and applies to `always_include` items too: npm gets dozens
  of malware advisories a day for typo-squats nobody installs, and the ones
  worth reporting are those that hit a widely used package. Download counts
  come from npm only; an unknown count (lookup failed, or a non-npm package)
  fails open and the item passes.
- Items without that data (RSS, HN) pass through; Claude judges relevance.
"""
import logging
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import httpx

from collectors import SEVERITIES, http_client

logger = logging.getLogger(__name__)

NPM_DOWNLOADS_URL = "https://api.npmjs.org/downloads/point/last-week/"
NPM_BULK_LIMIT = 128  # bulk lookups take up to 128 unscoped names


class NpmDownloads:
    """Weekly npm download counts, cached for the lifetime of one run.
    None means unknown (lookup failed); a package npm doesn't know is 0."""

    def __init__(self):
        self._cache: Dict[str, Optional[int]] = {}

    def prefetch(self, names: Iterable[str]) -> None:
        missing = sorted({n for n in names if n and n not in self._cache})
        if not missing:
            return
        unscoped = [n for n in missing if not n.startswith("@")]
        scoped = [n for n in missing if n.startswith("@")]
        with http_client() as client:
            for i in range(0, len(unscoped), NPM_BULK_LIMIT):
                self._fetch_bulk(client, unscoped[i:i + NPM_BULK_LIMIT])
            # npm doesn't support scoped packages in bulk lookups.
            for name in scoped:
                self._fetch_one(client, name)

    def get(self, name: str) -> Optional[int]:
        if name not in self._cache:
            self.prefetch([name])
        return self._cache.get(name)

    def cached(self, name: str) -> Optional[int]:
        """Like get() but never triggers a lookup."""
        return self._cache.get(name)

    def _fetch_one(self, client: httpx.Client, name: str) -> None:
        try:
            response = client.get(NPM_DOWNLOADS_URL + quote(name, safe="@"))
            if response.status_code == 404:
                self._cache[name] = 0
                return
            response.raise_for_status()
            self._cache[name] = int(response.json().get("downloads", 0))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("npm downloads lookup failed for %s: %s", name, exc)
            self._cache[name] = None

    def _fetch_bulk(self, client: httpx.Client, names: List[str]) -> None:
        if len(names) == 1:
            self._fetch_one(client, names[0])
            return
        try:
            response = client.get(NPM_DOWNLOADS_URL + ",".join(names))
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("npm bulk downloads lookup failed: %s", exc)
            for name in names:
                self._cache[name] = None
            return
        for name in names:
            entry = data.get(name)
            # Unknown packages come back as null in bulk responses.
            self._cache[name] = int(entry.get("downloads", 0)) if isinstance(entry, dict) else 0


def _severity_rank(severity: Optional[str]) -> int:
    try:
        return SEVERITIES.index((severity or "").lower())
    except ValueError:
        return -1


def _passes_severity(item: dict, filters: dict) -> bool:
    min_severity = filters.get("min_severity")
    if not min_severity or not item.get("severity"):
        return True
    if set(item.get("tags", [])) & set(filters.get("always_include", [])):
        return True
    return _severity_rank(item["severity"]) >= _severity_rank(min_severity)


def _is_npm(package: dict) -> bool:
    return package.get("ecosystem") == "npm"


def _passes_packages(item: dict, package_filter: dict, downloads: NpmDownloads) -> bool:
    names = [p["name"] for p in item.get("packages") or [] if p.get("name")]
    if not names or not package_filter:
        return True
    watchlist = set(package_filter.get("watchlist", []))
    if watchlist & set(names):
        return True
    min_downloads = package_filter.get("min_weekly_downloads")
    if min_downloads is None:
        return False
    for package in item["packages"]:
        if not package.get("name"):
            continue
        count = downloads.get(package["name"]) if _is_npm(package) else None
        # Fail open: an unknown count shouldn't silently hide a real issue.
        if count is None or count >= min_downloads:
            return True
    return False


def apply_filters(items: List[dict], filters: dict, downloads: Optional[NpmDownloads] = None) -> Tuple[List[dict], int]:
    """Returns (kept_items, dropped_count)."""
    if not filters:
        return items, 0
    downloads = downloads or NpmDownloads()
    package_filter = filters.get("packages") or {}

    after_severity = [item for item in items if _passes_severity(item, filters)]

    if package_filter.get("min_weekly_downloads") is not None:
        downloads.prefetch(
            p["name"] for item in after_severity for p in item.get("packages") or []
            if p.get("name") and _is_npm(p)
        )
    kept = [item for item in after_severity if _passes_packages(item, package_filter, downloads)]

    # Let Claude see the numbers it was filtered on.
    for item in kept:
        for package in item.get("packages") or []:
            count = downloads.cached(package.get("name")) if _is_npm(package) else None
            if count is not None:
                package["weekly_downloads"] = count

    return kept, len(items) - len(kept)
