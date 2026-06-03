#!/usr/bin/env python3
"""Single-feed RSS to Misskey/Sharkey bot."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
from dotenv import load_dotenv

DEFAULT_FEED_URL = ""
DEFAULT_VISIBILITY = "public"
DEFAULT_MAX_POSTS_PER_RUN = 3
DEFAULT_POST_TEMPLATE = "templates/post.txt"
DEFAULT_DATABASE_PATH = "data/bot.sqlite"
REQUEST_TIMEOUT_SECONDS = 20

LOGGER = logging.getLogger("misskey-rss-bot")


@dataclass(frozen=True)
class Config:
    misskey_host: str | None
    misskey_token: str | None
    feed_url: str
    visibility: str
    dry_run: bool
    max_posts_per_run: int
    post_template: Path
    database_path: Path


@dataclass(frozen=True)
class RssItem:
    feed_url: str
    guid: str
    link: str
    normalized_link: str
    title: str
    pub_date: str | None
    description: str | None
    sort_key: datetime


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def str_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    env_path = Path(".env")
    env_loaded = load_dotenv(env_path)
    if not env_loaded:
        LOGGER.warning(".env file was not loaded. Using environment variables and defaults where possible.")

    try:
        max_posts = int(os.getenv("MAX_POSTS_PER_RUN", str(DEFAULT_MAX_POSTS_PER_RUN)))
    except ValueError as exc:
        raise RuntimeError("MAX_POSTS_PER_RUN must be an integer") from exc
    if max_posts < 0:
        raise RuntimeError("MAX_POSTS_PER_RUN must be 0 or greater")

    return Config(
        misskey_host=os.getenv("MISSKEY_HOST", "").strip() or None,
        misskey_token=os.getenv("MISSKEY_TOKEN", "").strip() or None,
        feed_url=os.getenv("FEED_URL", DEFAULT_FEED_URL).strip(),
        visibility=os.getenv("VISIBILITY", DEFAULT_VISIBILITY).strip() or DEFAULT_VISIBILITY,
        dry_run=str_to_bool(os.getenv("DRY_RUN"), default=True),
        max_posts_per_run=max_posts,
        post_template=Path(os.getenv("POST_TEMPLATE", DEFAULT_POST_TEMPLATE).strip() or DEFAULT_POST_TEMPLATE),
        database_path=Path(os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH).strip() or DEFAULT_DATABASE_PATH),
    )


def validate_posting_config(config: Config) -> None:
    if config.dry_run:
        return
    if not config.misskey_host:
        raise RuntimeError("MISSKEY_HOST is required when DRY_RUN=false")
    if not config.misskey_token:
        raise RuntimeError("MISSKEY_TOKEN is required when DRY_RUN=false")


def validate_feed_config(config: Config) -> None:
    if not config.feed_url:
        raise RuntimeError("FEED_URL is required")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_db(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rss_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT NOT NULL,
            guid TEXT,
            link TEXT NOT NULL,
            normalized_link TEXT NOT NULL,
            title TEXT NOT NULL,
            pub_date TEXT,
            description TEXT,
            status TEXT NOT NULL CHECK (status IN ('seen', 'posted', 'failed', 'skipped')),
            misskey_note_id TEXT,
            error_message TEXT,
            discovered_at TEXT NOT NULL,
            posted_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_items_guid_unique
            ON rss_items(guid)
            WHERE guid IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_items_normalized_link_unique
            ON rss_items(normalized_link);

        CREATE INDEX IF NOT EXISTS idx_rss_items_title_pub_date
            ON rss_items(title, pub_date);

        CREATE INDEX IF NOT EXISTS idx_rss_items_status
            ON rss_items(status);

        CREATE TRIGGER IF NOT EXISTS trg_rss_items_updated_at
        AFTER UPDATE ON rss_items
        FOR EACH ROW
        WHEN NEW.updated_at = OLD.updated_at
        BEGIN
            UPDATE rss_items SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
        END;
        """
    )
    conn.commit()
    LOGGER.info("DB ready: %s", database_path)
    return conn


def normalize_url(url: str) -> str:
    stripped = url.strip()
    parts = urlsplit(stripped)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def parse_datetime(value: str | None, parsed_struct: Any | None = None) -> datetime:
    if parsed_struct:
        try:
            return datetime.fromtimestamp(timegm(parsed_struct), tz=timezone.utc)
        except (OverflowError, TypeError, ValueError):
            pass
    if value:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    return datetime.max.replace(tzinfo=timezone.utc)


def fetch_rss(feed_url: str) -> list[RssItem]:
    LOGGER.info("Fetching RSS: %s", feed_url)
    try:
        response = requests.get(feed_url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"RSS fetch failed: {exc}") from exc

    parsed = feedparser.parse(response.content)
    if parsed.bozo:
        LOGGER.warning("RSS parser warning: %s", parsed.bozo_exception)

    items: list[RssItem] = []
    skipped_missing_link = 0
    for entry in parsed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not link:
            skipped_missing_link += 1
            LOGGER.warning("Skipping RSS item without link: title=%s", title or "(no title)")
            continue

        guid = (
            entry.get("guid")
            or entry.get("id")
            or entry.get("rss_guid")
            or link
        )
        guid = str(guid).strip() or link
        pub_date = (
            entry.get("published")
            or entry.get("updated")
            or entry.get("pubDate")
            or None
        )
        if pub_date is not None:
            pub_date = str(pub_date).strip() or None
        description = entry.get("description") or entry.get("summary") or None
        if description is not None:
            description = str(description)

        items.append(
            RssItem(
                feed_url=feed_url,
                guid=guid,
                link=link,
                normalized_link=normalize_url(link),
                title=title or "(no title)",
                pub_date=pub_date,
                description=description,
                sort_key=parse_datetime(pub_date, entry.get("published_parsed") or entry.get("updated_parsed")),
            )
        )

    LOGGER.info("RSS item count: %d", len(items))
    if skipped_missing_link:
        LOGGER.info("Skipped RSS items without link: %d", skipped_missing_link)
    return items


def find_existing_item(conn: sqlite3.Connection, item: RssItem) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
          FROM rss_items
         WHERE (guid IS NOT NULL AND guid = ?)
            OR normalized_link = ?
            OR (title = ? AND ((pub_date IS NULL AND ? IS NULL) OR pub_date = ?))
         ORDER BY
            CASE
                WHEN guid IS NOT NULL AND guid = ? THEN 1
                WHEN normalized_link = ? THEN 2
                ELSE 3
            END,
            id ASC
         LIMIT 1
        """,
        (
            item.guid,
            item.normalized_link,
            item.title,
            item.pub_date,
            item.pub_date,
            item.guid,
            item.normalized_link,
        ),
    ).fetchone()


def insert_item(
    conn: sqlite3.Connection,
    item: RssItem,
    status: str,
    misskey_note_id: str | None = None,
    error_message: str | None = None,
    posted_at: str | None = None,
) -> bool:
    discovered_at = utc_now_iso()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO rss_items (
                    feed_url,
                    guid,
                    link,
                    normalized_link,
                    title,
                    pub_date,
                    description,
                    status,
                    misskey_note_id,
                    error_message,
                    discovered_at,
                    posted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.feed_url,
                    item.guid,
                    item.link,
                    item.normalized_link,
                    item.title,
                    item.pub_date,
                    item.description,
                    status,
                    misskey_note_id,
                    error_message,
                    discovered_at,
                    posted_at,
                ),
            )
        LOGGER.info("DB registered: status=%s title=%s", status, item.title)
        return True
    except sqlite3.IntegrityError as exc:
        LOGGER.warning("DB registration skipped because item already exists: title=%s error=%s", item.title, exc)
        return False


def select_new_items(conn: sqlite3.Connection, items: Iterable[RssItem]) -> tuple[list[RssItem], int]:
    candidates: list[RssItem] = []
    skipped = 0
    for item in items:
        existing = find_existing_item(conn, item)
        if existing:
            skipped += 1
            LOGGER.info(
                "Skipping already registered item: status=%s title=%s",
                existing["status"],
                item.title,
            )
            continue
        candidates.append(item)
    return candidates, skipped


def load_template(template_path: Path) -> str:
    if not template_path.exists():
        raise RuntimeError(f"Post template file not found: {template_path}")
    if not template_path.is_file():
        raise RuntimeError(f"Post template path is not a file: {template_path}")
    return template_path.read_text(encoding="utf-8")


def render_post(template: str, item: RssItem) -> str:
    replacements = {
        "title": item.title,
        "link": item.link,
        "guid": item.guid,
        "pub_date": item.pub_date or "",
        "description": item.description or "",
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{{ " + key + " }}", value)
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered.strip()


def extract_note_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "noteId"):
        value = payload.get(key)
        if value:
            return str(value)
    for parent_key in ("createdNote", "note"):
        child = payload.get(parent_key)
        if isinstance(child, dict) and child.get("id"):
            return str(child["id"])
    return None


def post_to_misskey(config: Config, text: str) -> str | None:
    validate_posting_config(config)
    assert config.misskey_host is not None
    assert config.misskey_token is not None

    endpoint = config.misskey_host.rstrip("/") + "/api/notes/create"
    body = {
        "i": config.misskey_token,
        "text": text,
        "visibility": config.visibility,
    }
    try:
        response = requests.post(endpoint, json=body, timeout=REQUEST_TIMEOUT_SECONDS)
        if not response.ok:
            raise RuntimeError(f"Misskey API returned HTTP {response.status_code}: {response.text[:500]}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return extract_note_id(payload)
    except requests.RequestException as exc:
        raise RuntimeError(f"Misskey post failed: {exc}") from exc


def run_init_seen(config: Config, conn: sqlite3.Connection) -> int:
    LOGGER.info("Mode: init-seen")
    LOGGER.info("DRY_RUN: %s (init-seen never posts to Misskey)", config.dry_run)
    validate_feed_config(config)
    items = fetch_rss(config.feed_url)

    registered = 0
    skipped = 0
    for item in items:
        if find_existing_item(conn, item):
            skipped += 1
            continue
        if insert_item(conn, item, status="seen"):
            registered += 1
        else:
            skipped += 1

    LOGGER.info("init-seen result: registered=%d skipped=%d", registered, skipped)
    return 0


def run_normal(config: Config, conn: sqlite3.Connection) -> int:
    LOGGER.info("Mode: normal")
    LOGGER.info("DRY_RUN: %s", config.dry_run)
    validate_feed_config(config)
    validate_posting_config(config)

    items = fetch_rss(config.feed_url)
    candidates, skipped = select_new_items(conn, items)
    candidates.sort(key=lambda item: (item.sort_key, item.title, item.link))
    to_process = candidates[: config.max_posts_per_run]

    LOGGER.info("New candidate count: %d", len(candidates))
    LOGGER.info("Skipped count: %d", skipped)
    LOGGER.info("Max posts per run: %d", config.max_posts_per_run)
    LOGGER.info("Processing count: %d", len(to_process))

    if not to_process:
        LOGGER.info("No new items to post.")
        return 0

    template = load_template(config.post_template)
    if config.dry_run:
        for item in to_process:
            text = render_post(template, item)
            LOGGER.info("DRY_RUN post candidate title: %s", item.title)
            LOGGER.info("DRY_RUN post body:\n%s", text)
        LOGGER.info("DRY_RUN=true, so no Misskey API calls were made and no DB rows were registered.")
        return 0

    for item in to_process:
        text = render_post(template, item)
        LOGGER.info("Posting title: %s", item.title)
        try:
            note_id = post_to_misskey(config, text)
            insert_item(
                conn,
                item,
                status="posted",
                misskey_note_id=note_id,
                posted_at=utc_now_iso(),
            )
            LOGGER.info("Post succeeded: note_id=%s title=%s", note_id or "(not returned)", item.title)
        except Exception as exc:  # noqa: BLE001 - keep failure reason in DB for manual handling.
            error_message = str(exc)
            insert_item(conn, item, status="failed", error_message=error_message)
            LOGGER.error("Post failed: title=%s error=%s", item.title, error_message)

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post new RSS feed items to Misskey/Sharkey.")
    parser.add_argument(
        "--init-seen",
        action="store_true",
        help="Register all current RSS items as seen without posting to Misskey.",
    )
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()
    try:
        config = load_config()
        conn = init_db(config.database_path)
        with conn:
            if args.init_seen:
                return run_init_seen(config, conn)
            return run_normal(config, conn)
    except Exception as exc:  # noqa: BLE001 - top-level CLI error reporting.
        LOGGER.error("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
