# Misskey RSS Bot

## 1. Overview

A Python bot that periodically checks a single RSS feed and posts new articles to Misskey / Sharkey.

- Target RSS feed: set `FEED_URL` in `.env`
- Misskey / Sharkey host: `MISSKEY_HOST` in `.env`
- Misskey API token: `MISSKEY_TOKEN` in `.env`
- Posted/seen item tracking: SQLite
- Scheduled execution: systemd timer

For safety, run `python bot.py --init-seen` first to register existing RSS items as seen. This prevents old articles from being posted on the first normal run.

Japanese documentation is available in [`README.ja.md`](README.ja.md).

## 2. Requirements

- Ubuntu Server
- Python 3.10.12
- SQLite (uses Python standard library `sqlite3`)
- systemd
- Git

## 3. Setup

```bash
git clone <YOUR_REPOSITORY_URL> misskey-rss-bot
cd misskey-rss-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
vi .env
```

Main `.env` settings:

```dotenv
MISSKEY_HOST=
MISSKEY_TOKEN=
FEED_URL=
VISIBILITY=public
DRY_RUN=true
MAX_POSTS_PER_RUN=3
POST_TEMPLATE=templates/post.txt
DATABASE_PATH=data/bot.sqlite
```

> `MISSKEY_TOKEN` is a secret. Do not commit `.env` to Git.

## 4. Initial seen registration

Before the first normal run, register all current RSS items as `seen` so old articles are not posted.

```bash
python bot.py --init-seen
```

This mode never posts to Misskey / Sharkey. Already registered items are skipped.

## 5. Dry run

Run the bot with `DRY_RUN=true` in `.env`.

```bash
python bot.py
```

When `DRY_RUN=true`:

- The Misskey API is not called
- The title and body that would be posted are logged
- No item is registered as posted in the database

## 6. Production posting test

Edit `.env`, set `MISSKEY_TOKEN`, and set `DRY_RUN=false`.

```bash
python bot.py
```

Only when `DRY_RUN=false`, the bot posts to `{MISSKEY_HOST}/api/notes/create`. An item is saved with `status=posted` only after a successful API response. On failure, the item is saved with `status=failed` and `error_message`.

## 7. systemd timer setup

The sample unit files assume the repository is deployed to `/opt/misskey-rss-bot` and the virtual environment is located at `/opt/misskey-rss-bot/.venv`.

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo cp systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now misskey-rss-bot.timer
```

The timer runs the oneshot `misskey-rss-bot.service` every 15 minutes.

## 8. Check logs

```bash
journalctl -u misskey-rss-bot.service -n 100
```

## 9. Check timer status

```bash
systemctl list-timers | grep misskey-rss-bot
```

## 10. Check SQLite data

```bash
sqlite3 data/bot.sqlite "select id, status, title, pub_date, posted_at from rss_items order by id desc limit 10;"
```

The database and tables are created automatically on first run.

## 11. Change the post template

The default template is `templates/post.txt`.

```text
📝 新着記事

{{ title }}

{{ link }}
```

Editing `templates/post.txt` changes the generated post body. You can also specify another template file with `POST_TEMPLATE` in `.env`.

Available variables:

- `{{ title }}`
- `{{ link }}`
- `{{ guid }}`
- `{{ pub_date }}`
- `{{ description }}`

If the template file does not exist, the bot exits with a clear error.

## 12. Troubleshooting

### `.env` does not exist

Run `cp .env.example .env` and edit the required values. If `.env` is missing, the bot uses environment variables and defaults where possible, but runtime requires `FEED_URL`, and production posting requires `MISSKEY_HOST` and `MISSKEY_TOKEN`.

### `MISSKEY_TOKEN` is not set

When running with `DRY_RUN=false`, set `MISSKEY_TOKEN` in `.env`. If it is missing, the bot exits without posting.

### `FEED_URL` is not set

Set `FEED_URL` in `.env`. The bot does not include a default feed URL.

### RSS fetch failed

Check that `FEED_URL` is correct and that the server can access external HTTP/HTTPS URLs.

### Misskey posting failed

- Check that `MISSKEY_HOST` is correct
- Check the permissions of `MISSKEY_TOKEN`
- Check the `VISIBILITY` value
- Check the error saved in `rss_items.error_message`

### Template file does not exist

Check that `POST_TEMPLATE` in `.env` points to an existing file.

## Duplicate detection

An RSS item is treated as already registered if any of the following match, in this order:

1. `guid`
2. `normalized_link`
3. `title + pub_date`

If `guid` is missing, `link` is used as the fallback. `normalized_link` removes URL fragments and treats trailing slashes as equivalent. Query strings are preserved.

Items previously saved with `status=failed` are also treated as registered, and are not retried automatically.

## Acceptance checklist

- [ ] `python -m py_compile bot.py` succeeds
- [ ] `DRY_RUN=true` does not post to Misskey
- [ ] `python bot.py --init-seen` registers existing RSS items as `seen`
- [ ] Running `--init-seen` twice does not create duplicate rows
- [ ] Normal runs do not post the same RSS item twice
- [ ] `status=posted` is saved only after a successful post
- [ ] On posting failure, `status=failed` and `error_message` are saved
- [ ] Editing `templates/post.txt` changes the post body
- [ ] `.env` and `data/*.sqlite` are not tracked by Git

## Developer verification

```bash
python -m py_compile bot.py
```
