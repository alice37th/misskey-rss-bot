# AGENTS.md

## Project
Misskey RSS Bot for a single RSS feed.

## Runtime
- Python 3.10.12
- Ubuntu server
- SQLite
- systemd timer

## Safety Requirements
- Never hard-code secrets.
- Read secrets from .env.
- DRY_RUN=true must never post to Misskey.
- --init-seen must only register RSS items as seen and must not post.
- Do not mark an item as posted before Misskey API succeeds.
- Avoid duplicate posts by checking guid, normalized_link, and title + pub_date.

## Scope
- Single feed only.
- No web UI.
- No multi-account support.
- No Docker unless explicitly requested.

## Definition of Done
- bot.py compiles.
- README includes setup and operation steps.
- .env.example exists.
- systemd service/timer examples exist.
- SQLite schema is created automatically.
- Manual dry-run can be executed.
