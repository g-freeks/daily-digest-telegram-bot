"""Generic source collectors for the topic digest. Each source `type` in
config.json maps to one collector here, configured by that source's params.

Every collector returns a list of candidate items (plain dicts) published in
(since, until]. Item fields:

    id, source, kind ("advisory" | "news"), title, url, published (ISO, UTC)
    optional: severity, packages [{name, ecosystem, vulnerable_versions, first_patched}],
              tags (e.g. "malicious_package"), cve, summary, points, discussion_url

Severity/package filters (filters.py) only touch items that carry that data.
Everything here is untrusted input and is handed to Claude as data only.
"""
import email.utils
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Dict, List
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree

logger = logging.getLogger(__name__)

USER_AGENT = "daily-digest-telegram-bot/0.1 (+https://github.com/g-freeks/daily-digest-telegram-bot)"
HTTP_TIMEOUT_SECONDS = 30.0
SUMMARY_MAX_CHARS = 1500

SEVERITIES = ["low", "medium", "high", "critical"]
ALWAYS_INCLUDE_TAGS = ["known_exploited", "malicious_package"]


class CollectorError(Exception):
    pass


def http_client() -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _truncate(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# --- github_advisories -------------------------------------------------------

GITHUB_ADVISORIES_URL = "https://api.github.com/advisories"
GITHUB_ADVISORY_TYPES = ["reviewed", "malware", "unreviewed"]


def validate_github_advisories(source: dict) -> List[str]:
    errors = []
    if not isinstance(source.get("ecosystem"), str) or not source["ecosystem"]:
        errors.append("github_advisories: ecosystem must be a non-empty string")
    types = source.get("types", ["reviewed", "malware"])
    if not isinstance(types, list) or not types or any(t not in GITHUB_ADVISORY_TYPES for t in types):
        errors.append(f"github_advisories: types must be a non-empty list of {GITHUB_ADVISORY_TYPES}")
    if not isinstance(source.get("max_pages", 10), int):
        errors.append("github_advisories: max_pages must be an int")
    return errors


def collect_github_advisories(source: dict, since: datetime, until: datetime) -> List[dict]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    items: List[dict] = []
    with http_client() as client:
        # The API only returns non-malware reviewed advisories unless `type`
        # is given, so each configured type is a separate query.
        for advisory_type in source.get("types", ["reviewed", "malware"]):
            url = GITHUB_ADVISORIES_URL
            params = {
                "ecosystem": source["ecosystem"],
                "type": advisory_type,
                "published": f"{_iso(since)}..{_iso(until)}",
                "sort": "published",
                "direction": "desc",
                "per_page": 100,
            }
            for _ in range(source.get("max_pages", 10)):
                response = client.get(url, params=params, headers=headers)
                if response.status_code != 200:
                    raise CollectorError(
                        f"GitHub advisories ({advisory_type}) returned HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                for advisory in response.json():
                    item = _github_item(advisory, since, until)
                    if item:
                        items.append(item)
                next_url = response.links.get("next", {}).get("url")
                if not next_url:
                    break
                url, params = next_url, None
            else:
                logger.warning("github_advisories (%s): stopped after max_pages", advisory_type)
    return items


def _github_item(advisory: dict, since: datetime, until: datetime):
    if advisory.get("withdrawn_at"):
        return None
    published = _parse_iso(advisory["published_at"])
    if not (since < published <= until):
        return None

    tags = []
    if advisory.get("type") == "malware":
        tags.append("malicious_package")

    packages = []
    for vuln in advisory.get("vulnerabilities") or []:
        package = vuln.get("package") or {}
        packages.append({
            "name": package.get("name"),
            "ecosystem": package.get("ecosystem"),
            "vulnerable_versions": vuln.get("vulnerable_version_range"),
            "first_patched": vuln.get("first_patched_version"),
        })

    return {
        "id": advisory["ghsa_id"],
        "source": "github_advisories",
        "kind": "advisory",
        "title": advisory.get("summary") or advisory["ghsa_id"],
        "url": advisory.get("html_url"),
        "published": _iso(published),
        "severity": (advisory.get("severity") or "").lower() or None,
        "cve": advisory.get("cve_id"),
        "packages": packages,
        "tags": tags,
        "summary": _truncate(advisory.get("description") or ""),
    }


# --- rss ---------------------------------------------------------------------

ATOM_NS = "{http://www.w3.org/2005/Atom}"


def validate_rss(source: dict) -> List[str]:
    if not _is_http_url(source.get("url")):
        return [f"rss: url must be an http(s) URL, got {source.get('url')!r}"]
    return []


def _parse_feed_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = _parse_iso(value)
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def collect_rss(source: dict, since: datetime, until: datetime) -> List[dict]:
    with http_client() as client:
        response = client.get(source["url"])
    if response.status_code != 200:
        raise CollectorError(f"RSS {source['url']} returned HTTP {response.status_code}")
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise CollectorError(f"RSS {source['url']} is not valid XML: {exc}") from exc

    entries = []
    for item in root.iter("item"):  # RSS 2.0
        entries.append({
            "title": item.findtext("title"),
            "url": item.findtext("link"),
            "guid": item.findtext("guid"),
            "date": item.findtext("pubDate"),
            "summary": item.findtext("description"),
        })
    for entry in root.iter(f"{ATOM_NS}entry"):  # Atom
        link = entry.find(f"{ATOM_NS}link")
        entries.append({
            "title": entry.findtext(f"{ATOM_NS}title"),
            "url": link.get("href") if link is not None else None,
            "guid": entry.findtext(f"{ATOM_NS}id"),
            "date": entry.findtext(f"{ATOM_NS}published") or entry.findtext(f"{ATOM_NS}updated"),
            "summary": entry.findtext(f"{ATOM_NS}summary"),
        })

    items = []
    for entry in entries:
        published = _parse_feed_date(entry["date"])
        if published is None or not (since < published <= until):
            continue
        url = (entry["url"] or "").strip()
        items.append({
            "id": "rss:" + ((entry["guid"] or "").strip() or url),
            "source": "rss",
            "kind": "news",
            "title": (entry["title"] or "").strip(),
            "url": url,
            "published": _iso(published),
            "feed": source["url"],
            "summary": _truncate(entry["summary"] or ""),
        })
    return items


# --- hn_algolia --------------------------------------------------------------

HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"


def validate_hn_algolia(source: dict) -> List[str]:
    errors = []
    queries = source.get("queries")
    if not isinstance(queries, list) or not queries or not all(isinstance(q, str) and q for q in queries):
        errors.append("hn_algolia: queries must be a non-empty list of strings")
    if not isinstance(source.get("min_points", 0), int):
        errors.append("hn_algolia: min_points must be an int")
    return errors


def collect_hn_algolia(source: dict, since: datetime, until: datetime) -> List[dict]:
    min_points = source.get("min_points", 0)
    seen = set()
    items = []
    with http_client() as client:
        for query in source["queries"]:
            response = client.get(HN_ALGOLIA_URL, params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{int(since.timestamp())},created_at_i<={int(until.timestamp())}",
                "hitsPerPage": 50,
            })
            if response.status_code != 200:
                raise CollectorError(f"HN Algolia returned HTTP {response.status_code} for {query!r}")
            for hit in response.json().get("hits", []):
                object_id = hit.get("objectID")
                if not object_id or object_id in seen:
                    continue
                if (hit.get("points") or 0) < min_points:
                    continue
                seen.add(object_id)
                discussion_url = f"https://news.ycombinator.com/item?id={object_id}"
                items.append({
                    "id": f"hn:{object_id}",
                    "source": "hn_algolia",
                    "kind": "news",
                    "title": hit.get("title") or "",
                    "url": hit.get("url") or discussion_url,
                    "discussion_url": discussion_url,
                    "published": _iso(datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc)),
                    "points": hit.get("points"),
                    "matched_query": query,
                })
    return items


# --- registry ----------------------------------------------------------------

COLLECTORS: Dict[str, Callable[[dict, datetime, datetime], List[dict]]] = {
    "github_advisories": collect_github_advisories,
    "rss": collect_rss,
    "hn_algolia": collect_hn_algolia,
}

VALIDATORS: Dict[str, Callable[[dict], List[str]]] = {
    "github_advisories": validate_github_advisories,
    "rss": validate_rss,
    "hn_algolia": validate_hn_algolia,
}


def validate_source(source: object) -> List[str]:
    if not isinstance(source, dict):
        return ["each source must be an object"]
    source_type = source.get("type")
    if source_type not in COLLECTORS:
        return [f"unknown source type {source_type!r} (known: {', '.join(COLLECTORS)})"]
    return VALIDATORS[source_type](source)


def describe_source(source: dict) -> str:
    params = ", ".join(f"{k}={v}" for k, v in source.items() if k != "type")
    return f"{source['type']}({params})"


def collect_all(sources: List[dict], since: datetime, until: datetime):
    """Runs every configured collector. One failing source doesn't sink the
    others; returns (items, errors) where errors are human-readable strings."""
    items: List[dict] = []
    errors: List[str] = []
    for source in sources:
        try:
            found = COLLECTORS[source["type"]](source, since, until)
            logger.info("%s: %d item(s)", describe_source(source), len(found))
            items.extend(found)
        except (CollectorError, httpx.HTTPError, ValueError, KeyError) as exc:
            logger.exception("collector failed: %s", describe_source(source))
            errors.append(f"{describe_source(source)}: {exc}")
    return items, errors
